"""Offline unit tests for the reconstructed MerchantBench Hermes adapter."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import requests

from merchantbench_adapter.__main__ import _build_parser
from merchantbench_adapter.bridge import (
    Bridge,
    _flush_pending_auxiliary_usage,
    _token_usage,
    _usage_delta,
    _usage_snapshot,
)
from merchantbench_adapter.history import (
    _sanitize_merchantbench_agent_history,
    _sanitize_merchantbench_history,
)
from merchantbench_adapter.runtime import (
    MERCHANTBENCH_CAPABILITY_GUIDANCE,
    MerchantBenchHermesRuntime,
    _load_run_local_agent_kwargs,
)
from run_agent import AIAgent


_OVERRIDE_ENV = (
    "MERCHANTBENCH_REASONING_EFFORT",
    "HERMES_REASONING_EFFORT",
    "MERCHANTBENCH_OPENROUTER_PROVIDERS",
)

_SPAWN_ARGS = [
    "--run-id",
    "run_abc",
    "--base-url",
    "http://127.0.0.1:5050",
    "--agent-id",
    "agent_0",
    "--max-hops-per-step",
    "30",
    "--quiet",
]


def _make_tool_defs(*names: str) -> list:
    """Build minimal tool definition list accepted by AIAgent.__init__."""
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _clear_routing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _OVERRIDE_ENV:
        monkeypatch.delenv(name, raising=False)


def _isolate_run_local_config_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Point Hermes config resolution at ``tmp_path`` and drop load caches.

    ``load_config()`` is keyed on ``str(get_config_path())`` plus file
    signature; leftover cache entries from other tests must not leak in.
    """
    import hermes_cli.config as config_mod

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(
        config_mod, "get_config_path", lambda: tmp_path / "config.yaml"
    )
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()


def _clear_cli_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MERCHANTBENCH_RUN_ID",
        "MERCHANTBENCH_BASE_URL",
        "MERCHANTBENCH_AGENT_ID",
        "MERCHANTBENCH_MAX_HOPS",
        "MODEL_NAME",
        "OPENAI_BASE_URL",
        "OPENAI_API_KEY",
        "HERMES_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


class TestMerchantBenchAdapterCli:
    def test_spawn_contract_and_new_flags(self, monkeypatch):
        _clear_cli_env(monkeypatch)
        parser = _build_parser()
        args = parser.parse_args(
            [
                *_SPAWN_ARGS,
                "--model",
                "deepseek/deepseek-v4-flash-0731",
                "--max-steps",
                "168",
                "--max-observations",
                "10",
                "--timeout",
                "90",
                "--observation-timeout",
                "45",
                "--retry-sleep",
                "2.5",
                "--provider",
                "openrouter",
                "--openai-base-url",
                "https://openrouter.ai/api/v1",
                "--openai-api-key",
                "sk-test",
            ]
        )
        assert args.run_id == "run_abc"
        assert args.base_url == "http://127.0.0.1:5050"
        assert args.agent_id == "agent_0"
        assert args.max_hops_per_step == 30
        assert args.quiet is True
        assert args.model == "deepseek/deepseek-v4-flash-0731"
        assert args.max_steps == 168
        assert args.max_observations == 10
        assert args.timeout == 90.0
        assert args.observation_timeout == 45.0
        assert args.retry_sleep == 2.5
        assert args.provider == "openrouter"
        assert args.openai_base_url == "https://openrouter.ai/api/v1"
        assert args.openai_api_key == "sk-test"

    def test_parser_defaults(self, monkeypatch):
        _clear_cli_env(monkeypatch)
        parser = _build_parser()
        args = parser.parse_args(["--run-id", "run_abc"])
        assert args.base_url == "http://127.0.0.1:5050"
        assert args.agent_id == "agent_0"
        assert args.max_hops_per_step == 30
        assert args.max_steps is None
        assert args.max_observations is None
        assert args.timeout == 60.0
        assert args.observation_timeout == 30.0
        assert args.retry_sleep == 1.0
        assert args.provider is None
        assert args.quiet is False


class TestMerchantBenchAdapterConfigWiring:
    def test_config_yaml_reasoning_and_provider_routing(
        self, monkeypatch, tmp_path
    ):
        _clear_routing_env(monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "agent:\n"
            "  reasoning_effort: max\n"
            "provider_routing:\n"
            "  only:\n"
            "    - coreweave/fp8\n"
            "  require_parameters: true\n",
            encoding="utf-8",
        )
        _isolate_run_local_config_home(monkeypatch, tmp_path)
        kwargs = _load_run_local_agent_kwargs()
        assert kwargs["reasoning_config"] == {"enabled": True, "effort": "max"}
        assert kwargs["providers_allowed"] == ["coreweave/fp8"]
        assert kwargs["provider_require_parameters"] is True

    def test_absent_config_uses_defaults(self, monkeypatch, tmp_path):
        _clear_routing_env(monkeypatch)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {})
        kwargs = _load_run_local_agent_kwargs()
        assert kwargs["reasoning_config"] is None
        assert kwargs["providers_allowed"] is None
        assert kwargs["provider_require_parameters"] is False

    def test_missing_config_does_not_raise(self, monkeypatch, tmp_path):
        _clear_routing_env(monkeypatch)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        def _boom():
            raise OSError("config missing")

        monkeypatch.setattr("hermes_cli.config.load_config", _boom)
        kwargs = _load_run_local_agent_kwargs()
        assert kwargs["reasoning_config"] is None
        assert kwargs["providers_allowed"] is None
        assert kwargs["provider_require_parameters"] is False

    def test_env_overrides_beat_config(self, monkeypatch, tmp_path):
        _clear_routing_env(monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "agent:\n"
            "  reasoning_effort: max\n"
            "provider_routing:\n"
            "  only:\n"
            "    - coreweave/fp8\n"
            "  require_parameters: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "agent": {"reasoning_effort": "max"},
                "provider_routing": {
                    "only": ["coreweave/fp8"],
                    "require_parameters": True,
                },
            },
        )
        monkeypatch.setenv("MERCHANTBENCH_REASONING_EFFORT", "low")
        monkeypatch.setenv(
            "MERCHANTBENCH_OPENROUTER_PROVIDERS", "togethercomputer/fp8"
        )
        kwargs = _load_run_local_agent_kwargs()
        assert kwargs["reasoning_config"] == {"enabled": True, "effort": "low"}
        assert kwargs["providers_allowed"] == ["togethercomputer/fp8"]
        assert kwargs["provider_require_parameters"] is True

    def test_hermes_reasoning_effort_does_not_override_config(
        self, monkeypatch, tmp_path
    ):
        _clear_routing_env(monkeypatch)
        (tmp_path / "config.yaml").write_text(
            "agent:\n"
            "  reasoning_effort: max\n",
            encoding="utf-8",
        )
        _isolate_run_local_config_home(monkeypatch, tmp_path)
        monkeypatch.setenv("HERMES_REASONING_EFFORT", "low")
        kwargs = _load_run_local_agent_kwargs()
        assert kwargs["reasoning_config"] == {"enabled": True, "effort": "max"}


class TestSupportsReasoningExtraBodyGate:
    @pytest.mark.parametrize(
        "model",
        ["stealth/ox-alpha", "google/gemini-3.7-flash"],
    )
    def test_static_fallback_prefixes(self, model):
        with (
            patch(
                "run_agent.get_tool_definitions",
                return_value=_make_tool_defs("web_search"),
            ),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
            patch(
                "hermes_cli.models.openrouter_model_reasoning_capabilities",
                return_value=None,
            ),
            patch("hermes_cli.models.warm_openrouter_reasoning_caps_async"),
        ):
            agent = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                model=model,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
            assert agent._supports_reasoning_extra_body() is True


def _fake_session_agent(**buckets) -> SimpleNamespace:
    """Build a namespace with AIAgent-shaped session token counters."""
    return SimpleNamespace(
        session_input_tokens=buckets.get("input", 0),
        session_output_tokens=buckets.get("output", 0),
        session_cache_read_tokens=buckets.get("cache_read", 0),
        session_cache_write_tokens=buckets.get("cache_write", 0),
        session_reasoning_tokens=buckets.get("reasoning", 0),
        session_total_tokens=buckets.get("total", 0),
        interrupt=lambda *args, **kwargs: None,
    )


def _apply_session_tokens(agent: SimpleNamespace, **buckets) -> None:
    agent.session_input_tokens = buckets.get("input", 0)
    agent.session_output_tokens = buckets.get("output", 0)
    agent.session_cache_read_tokens = buckets.get("cache_read", 0)
    agent.session_cache_write_tokens = buckets.get("cache_write", 0)
    agent.session_reasoning_tokens = buckets.get("reasoning", 0)
    agent.session_total_tokens = buckets.get("total", 0)


def _env_calls_from_act(assistant_message, messages):
    """Return (tool_call_id, name) pairs for merchantbench_env calls in an /act."""
    payload = list(messages) if isinstance(messages, list) else []
    if not payload and isinstance(assistant_message, dict):
        payload = [assistant_message]
    calls = []
    for msg in payload:
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        for tool_call in msg.get("tool_calls") or []:
            if not isinstance(tool_call, dict):
                continue
            origin = str(
                tool_call.get("tool_origin")
                or msg.get("tool_origin")
                or "merchantbench_env"
            )
            if origin != "merchantbench_env":
                continue
            function = tool_call.get("function") or {}
            calls.append(
                (tool_call.get("id"), function.get("name") or "unknown")
            )
    return calls


class _FakeActClient:
    """In-memory MerchantBench client for offline /act usage tests."""

    def __init__(self) -> None:
        self.act_calls: list[dict] = []
        self.usage_calls: list[tuple] = []
        self._latest_env_t = 0

    def latest_env_t(self):
        return self._latest_env_t

    def record_usage(self, token_usage, **kwargs):
        self.usage_calls.append((dict(token_usage), dict(kwargs)))
        return {"ok": True, "recorded": True}

    def act(
        self,
        assistant_message=None,
        token_usage=None,
        *,
        messages=None,
        context=None,
    ):
        self.act_calls.append(
            {
                "assistant_message": assistant_message,
                "token_usage": token_usage,
                "messages": messages,
                "context": context,
            }
        )
        env_calls = _env_calls_from_act(assistant_message, messages)
        if not env_calls and isinstance(assistant_message, dict):
            calls = assistant_message.get("tool_calls") or []
            if calls:
                function = calls[0].get("function") or {}
                env_calls = [
                    (calls[0].get("id"), function.get("name") or "unknown")
                ]
        tool_results = [
            {
                "tool_call_id": call_id,
                "name": tool_name,
                "content": "ok",
                "tool_origin": "merchantbench_env",
            }
            for call_id, tool_name in env_calls
        ]
        step_done = any(name == "end_of_step" for _call_id, name in env_calls)
        return {
            "ok": True,
            "turn_idx": len(self.act_calls),
            "tool_results": tool_results,
            "step_done": step_done,
            "hook_released": step_done,
        }


class _HttpErrorOnCallActClient(_FakeActClient):
    """Raise HTTPError on one /act call without recording a successful post."""

    def __init__(self, *, fail_on_call: int, status: int) -> None:
        super().__init__()
        self.fail_on_call = fail_on_call
        self.fail_status = status
        self._call_idx = 0

    def act(
        self,
        assistant_message=None,
        token_usage=None,
        *,
        messages=None,
        context=None,
    ):
        self._call_idx += 1
        if self._call_idx == self.fail_on_call:
            raise _http_error(self.fail_status)
        return super().act(
            assistant_message=assistant_message,
            token_usage=token_usage,
            messages=messages,
            context=context,
        )


class _NamedResultActClient(_FakeActClient):
    """Like ``_FakeActClient``, but each env tool gets distinct result content."""

    def act(
        self,
        assistant_message=None,
        token_usage=None,
        *,
        messages=None,
        context=None,
    ):
        response = super().act(
            assistant_message=assistant_message,
            token_usage=token_usage,
            messages=messages,
            context=context,
        )
        for item in response.get("tool_results") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or "unknown"
            item["content"] = f"result-for-{name}"
        return response


class TestMerchantBenchAdapterUsage:
    def test_usage_delta_includes_cache_and_reasoning_buckets(self):
        agent = _fake_session_agent(
            input=120,
            output=45,
            cache_read=30,
            cache_write=5,
            reasoning=9,
            total=200,
        )
        before = {
            "input": 100,
            "output": 40,
            "cache_read": 10,
            "cache_write": 0,
            "reasoning": 4,
            "total": 160,
        }
        delta = _usage_delta(before, agent)
        assert delta == {
            "input": 20,
            "output": 5,
            "cache_read": 20,
            "cache_write": 5,
            "reasoning": 5,
            "total": 40,
        }
        assert set(delta) >= {
            "reasoning",
            "cache_read",
            "cache_write",
        }
        assert _usage_delta(_usage_snapshot(agent), agent) is None

    def test_token_usage_normalizes_openai_shaped_dict(self):
        assistant_message = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 300},
                "completion_tokens_details": {"reasoning_tokens": 25},
            }
        }
        assert _token_usage(
            assistant_message,
            provider="openai",
            api_mode="chat_completions",
        ) == {
            "input": 700,
            "output": 100,
            "cache_read": 300,
            "cache_write": 0,
            "reasoning": 25,
            "total": 1100,
        }

    def test_aux_ledger_retry_reuses_usage_id_and_frozen_step(self):
        record = {
            "token_usage": {"input": 80, "output": 10, "total": 90},
            "model": "summary-model",
        }

        class UsageClient:
            def __init__(self):
                self.usage_calls = []
                self.current_step = 12
                self.fail = True

            def latest_env_t(self):
                return self.current_step

            def record_usage(self, token_usage, **kwargs):
                self.usage_calls.append((dict(token_usage), dict(kwargs)))
                if self.fail:
                    self.current_step = 24
                    raise ConnectionError("response lost")
                return {"ok": True, "recorded": True}

        records = []
        client = UsageClient()
        bridge = Bridge(client)
        bridge.bind_agent(
            SimpleNamespace(
                context_compressor=SimpleNamespace(
                    summary_usage_records_snapshot=lambda: [dict(r) for r in records],
                ),
            )
        )
        records.append(record)

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        client.fail = False
        _flush_pending_auxiliary_usage(bridge, client, attempts=1)

        assert len(client.usage_calls) == 2
        first_kwargs = client.usage_calls[0][1]
        retry_kwargs = client.usage_calls[1][1]
        assert first_kwargs["usage_id"] == retry_kwargs["usage_id"]
        assert first_kwargs["usage_id"].startswith("compression-0-")
        assert first_kwargs["step"] == 12
        assert retry_kwargs["step"] == 12
        assert first_kwargs["source"] == "context_compression"
        assert client.current_step == 24

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert len(client.usage_calls) == 2

    def test_consecutive_acts_post_deltas_not_cumulative(self):
        client = _FakeActClient()
        agent = _fake_session_agent()
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        first = {
            "input": 20,
            "output": 5,
            "cache_read": 10,
            "cache_write": 2,
            "reasoning": 3,
            "total": 37,
        }
        second = {
            "input": 25,
            "output": 8,
            "cache_read": 14,
            "cache_write": 2,
            "reasoning": 7,
            "total": 49,
        }
        third = {
            "input": 40,
            "output": 12,
            "cache_read": 14,
            "cache_write": 4,
            "reasoning": 9,
            "total": 70,
        }

        _apply_session_tokens(agent, **first)
        bridge.call_environment_tool("search_products", {"q": "a"})
        _apply_session_tokens(agent, **second)
        bridge.call_environment_tool("search_products", {"q": "b"})
        _apply_session_tokens(agent, **third)
        bridge.force_end_of_step("no end_of_step")

        posted = [call["token_usage"] for call in client.act_calls]
        assert len(posted) == 3
        assert posted[0] == first
        assert posted[1] == {
            "input": 5,
            "output": 3,
            "cache_read": 4,
            "cache_write": 0,
            "reasoning": 4,
            "total": 12,
        }
        assert posted[2] == {
            "input": 15,
            "output": 4,
            "cache_read": 0,
            "cache_write": 2,
            "reasoning": 2,
            "total": 21,
        }
        summed = {
            key: posted[0][key] + posted[1][key] + posted[2][key]
            for key in first
        }
        assert summed == third
        assert posted[1] != second
        assert posted[2] != third

    def test_non_425_act_http_error_reclaims_usage_on_next_act(self):
        client = _HttpErrorOnCallActClient(fail_on_call=2, status=429)
        agent = _fake_session_agent()
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        first = {
            "input": 20,
            "output": 5,
            "cache_read": 10,
            "cache_write": 2,
            "reasoning": 3,
            "total": 37,
        }
        after_failed_turn = {
            "input": 25,
            "output": 8,
            "cache_read": 14,
            "cache_write": 2,
            "reasoning": 7,
            "total": 49,
        }
        after_retry_turn = {
            "input": 40,
            "output": 12,
            "cache_read": 14,
            "cache_write": 4,
            "reasoning": 9,
            "total": 70,
        }

        _apply_session_tokens(agent, **first)
        bridge.call_environment_tool("search_products", {"q": "a"})
        _apply_session_tokens(agent, **after_failed_turn)
        failed = bridge.call_environment_tool("search_products", {"q": "b"})
        assert json.loads(failed)["error"].startswith("/act failed")
        _apply_session_tokens(agent, **after_retry_turn)
        bridge.call_environment_tool("search_products", {"q": "c"})

        posted = [call["token_usage"] for call in client.act_calls]
        assert len(posted) == 2
        assert posted[0] == first
        combined = {
            key: after_retry_turn[key] - first[key] for key in first
        }
        assert posted[1] == combined
        summed = {key: posted[0][key] + posted[1][key] for key in first}
        assert summed == after_retry_turn

    def test_act_http_500_does_not_reclaim_usage_on_next_act(self):
        client = _HttpErrorOnCallActClient(fail_on_call=2, status=500)
        agent = _fake_session_agent()
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        first = {
            "input": 20,
            "output": 5,
            "cache_read": 10,
            "cache_write": 2,
            "reasoning": 3,
            "total": 37,
        }
        after_failed_turn = {
            "input": 25,
            "output": 8,
            "cache_read": 14,
            "cache_write": 2,
            "reasoning": 7,
            "total": 49,
        }
        after_retry_turn = {
            "input": 40,
            "output": 12,
            "cache_read": 14,
            "cache_write": 4,
            "reasoning": 9,
            "total": 70,
        }

        _apply_session_tokens(agent, **first)
        bridge.call_environment_tool("search_products", {"q": "a"})
        _apply_session_tokens(agent, **after_failed_turn)
        failed = bridge.call_environment_tool("search_products", {"q": "b"})
        assert json.loads(failed)["error"].startswith("/act failed")
        _apply_session_tokens(agent, **after_retry_turn)
        bridge.call_environment_tool("search_products", {"q": "c"})

        posted = [call["token_usage"] for call in client.act_calls]
        assert len(posted) == 2
        assert posted[0] == first
        assert posted[1] == {
            "input": 15,
            "output": 4,
            "cache_read": 0,
            "cache_write": 2,
            "reasoning": 2,
            "total": 21,
        }


def _http_error(status: int) -> requests.HTTPError:
    """Build an HTTPError whose ``response.status_code`` is ``status``."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(response=response)


def _openai_env_tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} env tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _obs(text: str, step: int, brief=None) -> dict:
    payload = {
        "text": text,
        "tick": {"day": 1, "hour": step, "step": step},
    }
    if brief is not None:
        payload["brief"] = brief
    return payload


class _LoopClient:
    """In-memory env client for offline observation-loop tests."""

    def __init__(
        self,
        *,
        observations=None,
        observation_errors=None,
        tools_by_call=None,
        act_error_status=None,
    ):
        self.observations = list(observations or [])
        self.observation_errors = list(observation_errors or [])
        self.tools_by_call = tools_by_call
        self.act_error_status = act_error_status
        self.tools_calls = 0
        self.observation_calls = 0
        self.act_calls = []
        self.default_tools = []

    def register(self, **kwargs):
        return {"ok": True}

    def refresh_schema(self):
        return None

    def tools(self):
        self.tools_calls += 1
        if self.tools_by_call is None:
            return list(self.default_tools)
        idx = min(self.tools_calls - 1, len(self.tools_by_call) - 1)
        return list(self.tools_by_call[idx])

    def observation(self):
        self.observation_calls += 1
        if self.observation_errors:
            raise self.observation_errors.pop(0)
        if not self.observations:
            raise _http_error(410)
        return self.observations.pop(0)

    def act(self, assistant_message=None, token_usage=None, **kwargs):
        if self.act_error_status:
            raise _http_error(self.act_error_status)
        messages = kwargs.get("messages")
        context = kwargs.get("context")
        self.act_calls.append(
            {
                "assistant_message": assistant_message,
                "token_usage": token_usage,
                "messages": messages,
                "context": context,
            }
        )
        env_calls = _env_calls_from_act(assistant_message, messages)
        tool_name = "end_of_step"
        call_id = None
        tool_results = []
        if env_calls:
            call_id, tool_name = env_calls[0]
            tool_results = [
                {
                    "tool_call_id": result_id,
                    "name": result_name,
                    "content": "ok",
                    "tool_origin": "merchantbench_env",
                }
                for result_id, result_name in env_calls
            ]
        elif isinstance(assistant_message, dict):
            calls = assistant_message.get("tool_calls") or []
            if calls:
                call_id = calls[0].get("id")
                function = calls[0].get("function") or {}
                tool_name = function.get("name") or tool_name
                tool_results = [
                    {
                        "tool_call_id": call_id,
                        "name": tool_name,
                        "content": "ok",
                    }
                ]
        if not tool_results:
            tool_results = [
                {
                    "tool_call_id": call_id,
                    "name": tool_name,
                    "content": "ok",
                }
            ]
        step_done = any(
            result.get("name") == "end_of_step" for result in tool_results
        )
        return {
            "ok": True,
            "tool_results": tool_results,
            "step_done": step_done,
            "hook_released": step_done,
        }

    def latest_env_t(self):
        return 0

    def record_usage(self, token_usage, **kwargs):
        return {"ok": True, "recorded": True}


class _CachingSchemaLoopClient(_LoopClient):
    """SDK-shaped client: ``tools()`` stays cached until ``refresh_schema()``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._env_tools = [_openai_env_tool("mb_test_alpha")]
        self._cached_tools = [dict(tool) for tool in self._env_tools]
        self.refresh_calls = 0

    def refresh_schema(self):
        self.refresh_calls += 1
        self._cached_tools = [dict(tool) for tool in self._env_tools]

    def tools(self):
        self.tools_calls += 1
        return [dict(tool) for tool in self._cached_tools]

    def observation(self):
        obs = super().observation()
        # Env-side set grows when the second hook is delivered; the cache
        # stays stale until the per-observation refresh_schema() call.
        if self.observation_calls == 2:
            self._env_tools = [
                _openai_env_tool("mb_test_alpha"),
                _openai_env_tool("mb_test_beta"),
            ]
        return obs


class _StubAgent:
    """Minimal AIAgent stand-in used by the observation-loop tests."""

    def __init__(self):
        self.ephemeral_system_prompt = None
        self.tools = []
        self.valid_tool_names = set()
        self.enabled_toolsets = []
        self.conversation_calls = []
        self._session_messages = []

    def run_conversation(
        self,
        user_message=None,
        conversation_history=None,
        system_message=None,
        **kwargs,
    ):
        tool_names = []
        for tool in self.tools or []:
            if not isinstance(tool, dict):
                continue
            function = tool.get("function") or {}
            name = function.get("name")
            if name:
                tool_names.append(str(name))
        self.conversation_calls.append(
            {
                "user_message": user_message,
                "system_message": system_message,
                "conversation_history": list(conversation_history or []),
                "tool_names": tool_names,
            }
        )
        history = list(conversation_history or [])
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": "ok"})
        self._session_messages = history
        return {"messages": history}

    def interrupt(self, *args, **kwargs):
        return None

    def clear_interrupt(self):
        return None


def _stub_runtime(monkeypatch, client, **runtime_kwargs):
    """Construct a runtime that talks to ``client`` without real credentials."""
    monkeypatch.setattr(
        "merchantbench_adapter.runtime._resolve_credentials",
        lambda *args, **kwargs: {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "model": "test-model",
            "provider": "openrouter",
        },
    )
    monkeypatch.setattr(
        "merchantbench_adapter.runtime._load_client_class",
        lambda: (lambda *args, **kwargs: client),
    )
    kwargs = {
        "base_url": "http://127.0.0.1:5050",
        "run_id": "run_test",
        "quiet": True,
    }
    kwargs.update(runtime_kwargs)
    runtime = MerchantBenchHermesRuntime(**kwargs)
    agent = _StubAgent()
    runtime.agent = agent
    runtime.bridge.bind_agent(agent)
    return runtime, agent


class TestMerchantBenchAdapterParity:
    @pytest.fixture(autouse=True)
    def _cleanup_merchantbench_registry(self):
        from toolsets import TOOLSETS
        from tools.registry import registry

        before_names = set(registry.get_tool_names_for_toolset("merchantbench"))
        before_toolset = TOOLSETS.get("merchantbench")
        yield
        after_names = set(registry.get_tool_names_for_toolset("merchantbench"))
        for name in after_names - before_names:
            registry.deregister(name)
        if before_toolset is None:
            TOOLSETS.pop("merchantbench", None)
        else:
            TOOLSETS["merchantbench"] = before_toolset

    def test_brief_dict_sets_system_prompt_once_string_ignored(self, monkeypatch):
        client = _LoopClient(
            observations=[
                _obs("t1", 0, brief="plain string brief"),
                _obs(
                    "t2",
                    1,
                    brief={"system_prompt": "You are a merchant."},
                ),
                _obs("t3", 2),
            ]
        )
        runtime, agent = _stub_runtime(monkeypatch, client)
        assert runtime.run() == 0
        assert len(agent.conversation_calls) == 3
        assert agent.conversation_calls[0]["system_message"] is None
        expected = (
            "You are a merchant.\n\n" + MERCHANTBENCH_CAPABILITY_GUIDANCE
        )
        assert agent.conversation_calls[1]["system_message"] == expected
        assert agent.conversation_calls[2]["system_message"] == expected
        assert agent.conversation_calls[0]["system_message"] != "plain string brief"

    def test_sanitize_drops_end_of_step_orphans_and_fallback_ack(self):
        eos_id = "eos-1"
        biz_id = "biz-1"
        history = [
            {"role": "user", "content": "first observation"},
            {
                "role": "assistant",
                "content": "checking stock",
                "tool_calls": [
                    {
                        "id": biz_id,
                        "type": "function",
                        "function": {
                            "name": "search_products",
                            "arguments": "{}",
                        },
                    },
                    {
                        "id": eos_id,
                        "type": "function",
                        "function": {"name": "end_of_step", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": biz_id,
                "name": "search_products",
                "content": "found",
            },
            {
                "role": "tool",
                "tool_call_id": eos_id,
                "name": "end_of_step",
                "content": "released",
            },
            {
                "role": "tool",
                "tool_call_id": "orphan-9",
                "name": "search_products",
                "content": "left behind by compaction",
            },
        ]
        sanitized = _sanitize_merchantbench_history(history)
        kept_call_names = [
            (call.get("function") or {}).get("name")
            for item in sanitized
            if item.get("role") == "assistant"
            for call in (item.get("tool_calls") or [])
        ]
        assert kept_call_names == ["search_products"]
        assert not any(
            item.get("role") == "tool" and item.get("tool_call_id") == eos_id
            for item in sanitized
        )
        assert not any(
            item.get("role") == "tool" and item.get("tool_call_id") == "orphan-9"
            for item in sanitized
        )
        assert sanitized[0]["content"] == "first observation"
        assert sanitized[-1]["content"] == "found"

        aliased = _sanitize_merchantbench_history(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "alias-eos",
                            "function": {"name": "merchantbench__end_of_step"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "alias-eos",
                    "content": "ok",
                },
            ]
        )
        assert aliased == []

        fallback_history = [
            {"role": "user", "content": "wakeup"},
            {
                "role": "assistant",
                "content": "[fallback] Hermes did not call end_of_step",
                "tool_calls": [
                    {
                        "id": "fb-eos",
                        "function": {"name": "end_of_step"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "fb-eos", "content": "ok"},
            {"role": "user", "content": "next wakeup"},
        ]
        repaired = _sanitize_merchantbench_history(fallback_history)
        assert repaired == [{"role": "user", "content": "next wakeup"}]

        surviving = fallback_history[-1]
        dropped_user = fallback_history[0]
        flushed = {id(surviving), id(dropped_user), 12345}
        agent = SimpleNamespace(
            _flushed_db_message_ids=flushed,
            _last_flushed_db_idx=99,
        )
        out = _sanitize_merchantbench_agent_history(agent, fallback_history)
        assert out == [{"role": "user", "content": "next wakeup"}]
        assert out[0] is surviving
        assert agent._last_flushed_db_idx == 1
        assert flushed == {id(surviving)}

    def test_observation_410_flushes_aux_usage_and_exits_0(self, monkeypatch):
        client = _LoopClient()
        runtime, _agent = _stub_runtime(monkeypatch, client)
        flushes = []
        runtime._flush_auxiliary_usage = lambda *, attempts=3: flushes.append(
            attempts
        )
        assert runtime.run() == 0
        assert flushes == [3]
        assert client.observation_calls == 1

    def test_non_410_observation_error_sleeps_then_retries(self, monkeypatch):
        client = _LoopClient(observation_errors=[_http_error(503)])
        runtime, _agent = _stub_runtime(monkeypatch, client, retry_sleep=2.5)
        sleeps = []
        monkeypatch.setattr(
            "merchantbench_adapter.runtime.time.sleep",
            lambda seconds: sleeps.append(seconds),
        )
        flushes = []
        runtime._flush_auxiliary_usage = lambda *, attempts=3: flushes.append(
            attempts
        )
        assert runtime.run() == 0
        assert sleeps == [2.5]
        assert client.observation_calls == 2
        assert flushes == [3]

    def test_stale_step_425_discards_turn_then_exits_on_410(self, monkeypatch):
        client = _LoopClient(
            observations=[_obs("voided", 0)],
            act_error_status=425,
        )
        runtime, agent = _stub_runtime(monkeypatch, client)
        prior = [
            {"role": "user", "content": "prior"},
            {"role": "assistant", "content": "kept"},
        ]
        runtime.history = list(prior)
        turns = []

        def _run_conversation(
            user_message=None,
            conversation_history=None,
            system_message=None,
            **kwargs,
        ):
            turns.append(user_message)
            history = list(conversation_history or [])
            history.append({"role": "user", "content": user_message})
            history.append({"role": "assistant", "content": "voided turn"})
            runtime.bridge.call_environment_tool("search_products", {"q": "x"})
            return {"messages": history}

        agent.run_conversation = _run_conversation
        assert runtime.run() == 0
        assert turns == ["voided"]
        assert runtime.history == prior

    def test_max_observations_exits_after_one_completed_step(self, monkeypatch):
        client = _LoopClient(observations=[_obs("only", 0), _obs("two", 1)])
        runtime, agent = _stub_runtime(monkeypatch, client, max_observations=1)
        flushes = []
        runtime._flush_auxiliary_usage = lambda *, attempts=3: flushes.append(
            attempts
        )
        assert runtime.run() == 0
        assert len(agent.conversation_calls) == 1
        assert client.observation_calls == 1
        assert flushes == [3]

    def test_refresh_tools_updates_agent_schema_between_observations(
        self, monkeypatch
    ):
        client = _LoopClient(
            observations=[_obs("first", 0), _obs("second", 1)],
            tools_by_call=[
                [_openai_env_tool("mb_test_alpha")],
                [_openai_env_tool("mb_test_alpha")],
                [
                    _openai_env_tool("mb_test_alpha"),
                    _openai_env_tool("mb_test_beta"),
                ],
            ],
        )
        runtime, agent = _stub_runtime(monkeypatch, client)
        assert runtime.run() == 0
        assert len(agent.conversation_calls) == 2
        assert agent.conversation_calls[0]["tool_names"] == ["mb_test_alpha"]
        assert agent.conversation_calls[1]["tool_names"] == [
            "mb_test_alpha",
            "mb_test_beta",
        ]
        assert "mb_test_beta" in agent.valid_tool_names
        assert runtime.bridge.registered_tools == [
            "mb_test_alpha",
            "mb_test_beta",
        ]

    def test_refresh_schema_exposes_new_env_tool_on_next_observation(
        self, monkeypatch
    ):
        client = _CachingSchemaLoopClient(
            observations=[_obs("first", 0), _obs("second", 1)]
        )
        runtime, agent = _stub_runtime(monkeypatch, client)
        assert runtime.run() == 0
        assert len(agent.conversation_calls) == 2
        assert agent.conversation_calls[0]["tool_names"] == ["mb_test_alpha"]
        assert agent.conversation_calls[1]["tool_names"] == [
            "mb_test_alpha",
            "mb_test_beta",
        ]
        assert "mb_test_beta" in agent.valid_tool_names
        assert client.refresh_calls >= 3
        assert runtime.bridge.registered_tools == [
            "mb_test_alpha",
            "mb_test_beta",
        ]

    def test_observation_connection_error_sleeps_then_retries(self, monkeypatch):
        client = _LoopClient(
            observations=[_obs("ok", 0)],
            observation_errors=[
                requests.ConnectionError("blip 1"),
                requests.ConnectionError("blip 2"),
            ],
        )
        runtime, agent = _stub_runtime(monkeypatch, client, retry_sleep=2.5)
        sleeps = []
        monkeypatch.setattr(
            "merchantbench_adapter.runtime.time.sleep",
            lambda seconds: sleeps.append(seconds),
        )
        flushes = []
        runtime._flush_auxiliary_usage = lambda *, attempts=3: flushes.append(
            attempts
        )
        assert runtime.run() == 0
        assert sleeps == [2.5, 2.5]
        assert len(agent.conversation_calls) == 1
        assert client.observation_calls == 4
        assert flushes == [3]


class _StaleOnceActClient(_FakeActClient):
    """Record every /act; raise HTTP 425 on the first call only."""

    def __init__(self) -> None:
        super().__init__()
        self._raise_stale = True

    def act(
        self,
        assistant_message=None,
        token_usage=None,
        *,
        messages=None,
        context=None,
    ):
        if self._raise_stale:
            self.act_calls.append(
                {
                    "assistant_message": assistant_message,
                    "token_usage": token_usage,
                    "messages": messages,
                    "context": context,
                }
            )
            self._raise_stale = False
            raise _http_error(425)
        return super().act(
            assistant_message=assistant_message,
            token_usage=token_usage,
            messages=messages,
            context=context,
        )


class TestMerchantBenchStaleStepGuard:
    def test_stale_step_blocks_subsequent_env_tool_without_act(self):
        client = _StaleOnceActClient()
        agent = _fake_session_agent(input=10, output=2, total=12)
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        first = json.loads(
            bridge.call_environment_tool("search_products", {"q": "x"})
        )
        assert first["error_type"] == "stale_step"
        assert "simulation advanced" in first["error"]
        assert len(client.act_calls) == 1
        snapshot_after_425 = dict(bridge._last_posted_usage_snapshot or {})

        _apply_session_tokens(agent, input=20, output=4, total=24)
        second = json.loads(
            bridge.call_environment_tool("search_products", {"q": "y"})
        )
        assert second["error_type"] == "stale_step"
        assert second["error"] == (
            "stale_step: simulation advanced; discard this decision "
            "window and re-observe."
        )
        assert len(client.act_calls) == 1
        assert bridge._last_posted_usage_snapshot == snapshot_after_425

    def test_force_end_of_step_is_noop_while_stale(self):
        client = _StaleOnceActClient()
        agent = _fake_session_agent()
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        json.loads(bridge.call_environment_tool("search_products", {"q": "x"}))
        assert bridge.stale_step is True
        assert len(client.act_calls) == 1

        bridge.force_end_of_step("voided window")
        assert len(client.act_calls) == 1
        assert bridge.stale_step is True
        assert bridge.step_released is False

    def test_reset_step_flags_clears_stale_guard(self):
        client = _StaleOnceActClient()
        agent = _fake_session_agent()
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        blocked = json.loads(
            bridge.call_environment_tool("search_products", {"q": "x"})
        )
        assert blocked["error_type"] == "stale_step"
        assert len(client.act_calls) == 1

        bridge.reset_step_flags()
        result = bridge.call_environment_tool("search_products", {"q": "z"})
        assert result == "ok"
        assert len(client.act_calls) == 2


def _assistant_tool_call(call_id, name, arguments="{}"):
    """Build one OpenAI-format tool_call dict."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def _assistant_with_calls(call_id_name_pairs, content=""):
    """Build an assistant message carrying the given tool calls."""
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            _assistant_tool_call(call_id, name)
            for call_id, name in call_id_name_pairs
        ],
    }


def _tool_result_message(call_id, name, content="ok"):
    """Build a role=tool message paired with ``call_id``."""
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": content,
    }


class _StaleOnceLoopClient(_LoopClient):
    """Record every /act; raise HTTP 425 on the first call only."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._raise_stale = True

    def act(self, assistant_message=None, token_usage=None, **kwargs):
        if self._raise_stale:
            self.act_calls.append(
                {
                    "assistant_message": assistant_message,
                    "token_usage": token_usage,
                    "messages": kwargs.get("messages"),
                    "context": kwargs.get("context"),
                }
            )
            self._raise_stale = False
            raise _http_error(425)
        return super().act(
            assistant_message=assistant_message,
            token_usage=token_usage,
            **kwargs,
        )


class TestMerchantBenchAdapterTraceBatch:
    @pytest.fixture(autouse=True)
    def _cleanup_merchantbench_registry(self):
        from toolsets import TOOLSETS
        from tools.registry import registry

        before_names = set(registry.get_tool_names_for_toolset("merchantbench"))
        before_toolset = TOOLSETS.get("merchantbench")
        yield
        after_names = set(registry.get_tool_names_for_toolset("merchantbench"))
        for name in after_names - before_names:
            registry.deregister(name)
        if before_toolset is None:
            TOOLSETS.pop("merchantbench", None)
        else:
            TOOLSETS["merchantbench"] = before_toolset

    def test_env_act_includes_native_trace_without_observation(self):
        client = _FakeActClient()
        agent = _fake_session_agent()
        observation = "OBSERVATION_TEXT_UNIQUE"
        prefix = [{"role": "user", "content": "prior"}]
        native_asst = _assistant_with_calls(
            [("native-1", "web_search")], content="searching"
        )
        native_tool = _tool_result_message("native-1", "web_search", "hits")
        env_asst = _assistant_with_calls([("env-1", "search_products")])
        agent._session_messages = [
            *prefix,
            {"role": "user", "content": observation},
            native_asst,
            native_tool,
            env_asst,
        ]
        bridge = Bridge(client)
        bridge.bind_agent(agent)
        bridge.registered_tools = ["search_products", "end_of_step"]
        bridge.begin_step(observation_text=observation, history=prefix)

        result = bridge.call_environment_tool(
            "search_products", {"q": "x"}, tool_call_id="env-1"
        )
        assert result == "ok"
        batch = client.act_calls[0]["messages"]
        assert batch is not None
        assert not any(item.get("role") == "user" for item in batch)
        assert observation not in json.dumps(batch)
        assert [item.get("tool_origin") for item in batch] == [
            "hermes_native",
            "hermes_native",
            "merchantbench_env",
        ]
        assert batch[0]["role"] == "assistant"
        assert batch[1]["role"] == "tool"
        assert batch[1]["tool_call_id"] == "native-1"
        assert batch[2]["role"] == "assistant"
        env_names = [
            (call.get("function") or {}).get("name")
            for call in batch[2].get("tool_calls") or []
        ]
        assert env_names == ["search_products"]
        assert (batch[2].get("tool_calls") or [])[0]["tool_origin"] == (
            "merchantbench_env"
        )

    def test_context_tokens_and_compacted_once(self):
        client = _FakeActClient()
        agent = _fake_session_agent()
        agent.context_compressor = SimpleNamespace(
            compression_count=1,
            last_prompt_tokens=2048,
            summary_usage_records_snapshot=lambda: [],
        )
        bridge = Bridge(client)
        bridge.bind_agent(agent)
        bridge.registered_tools = ["search_products", "end_of_step"]
        agent.context_compressor.compression_count = 2

        bridge.call_environment_tool("search_products", {"q": "a"})
        first_context = client.act_calls[0]["context"]
        assert first_context == {"tokens": 2048, "compacted": True}

        bridge.call_environment_tool("search_products", {"q": "b"})
        second_context = client.act_calls[1]["context"]
        assert second_context == {"tokens": 2048}
        assert "compacted" not in second_context

    def test_end_of_step_is_last_among_env_calls(self):
        client = _FakeActClient()
        agent = _fake_session_agent()
        env_asst = _assistant_with_calls(
            [("eos-1", "end_of_step"), ("biz-1", "search_products")]
        )
        agent._session_messages = [env_asst]
        bridge = Bridge(client)
        bridge.bind_agent(agent)
        bridge.registered_tools = ["search_products", "end_of_step"]
        bridge.begin_step(observation_text="obs", history=[])

        result = bridge.call_environment_tool(
            "search_products", {}, tool_call_id="biz-1"
        )
        assert result == "ok"
        batch = client.act_calls[0]["messages"]
        assistant = next(item for item in batch if item.get("role") == "assistant")
        env_names = [
            (call.get("function") or {}).get("name")
            for call in assistant.get("tool_calls") or []
            if call.get("tool_origin") == "merchantbench_env"
        ]
        assert env_names == ["search_products", "end_of_step"]
        assert env_names[-1] == "end_of_step"

    def test_sibling_env_handlers_share_one_combined_act(self):
        client = _NamedResultActClient()
        agent = _fake_session_agent()
        env_asst = _assistant_with_calls(
            [("eos-1", "end_of_step"), ("biz-1", "search_products")]
        )
        agent._session_messages = [env_asst]
        bridge = Bridge(client)
        bridge.bind_agent(agent)
        bridge.registered_tools = ["search_products", "end_of_step"]
        bridge.begin_step(observation_text="obs", history=[])

        first = bridge.call_environment_tool(
            "search_products", {}, tool_call_id="biz-1"
        )
        assert first == "result-for-search_products"
        assert len(client.act_calls) == 1
        batch = client.act_calls[0]["messages"]
        assistant = next(
            item for item in batch if item.get("role") == "assistant"
        )
        env_ids_and_names = [
            (call.get("id"), (call.get("function") or {}).get("name"))
            for call in assistant.get("tool_calls") or []
            if call.get("tool_origin") == "merchantbench_env"
        ]
        assert env_ids_and_names == [
            ("biz-1", "search_products"),
            ("eos-1", "end_of_step"),
        ]

        second = bridge.call_environment_tool(
            "end_of_step", {}, tool_call_id="eos-1"
        )
        assert second == "result-for-end_of_step"
        assert len(client.act_calls) == 1
        assert bridge._cached_env_results == {}

    def test_normal_end_of_step_leaves_no_unposted_tail(self, monkeypatch):
        client = _LoopClient(observations=[_obs("obs-a", 0)])
        runtime, agent = _stub_runtime(monkeypatch, client)

        def _run_with_eos(
            user_message=None,
            conversation_history=None,
            system_message=None,
            **kwargs,
        ):
            history = list(conversation_history or [])
            native_asst = _assistant_with_calls(
                [("native-1", "web_search")], content="native"
            )
            native_tool = _tool_result_message("native-1", "web_search", "hits")
            eos_asst = _assistant_with_calls([("eos-1", "end_of_step")])
            live = history + [
                {"role": "user", "content": user_message},
                native_asst,
                native_tool,
                eos_asst,
            ]
            agent._session_messages = live
            runtime.bridge.registered_tools = ["search_products", "end_of_step"]
            runtime.bridge.call_environment_tool(
                "end_of_step", {}, tool_call_id="eos-1"
            )
            return {"messages": live}

        agent.run_conversation = _run_with_eos
        assert runtime.run() == 0
        assert len(client.act_calls) == 1
        batch = client.act_calls[0]["messages"]
        origins = [item.get("tool_origin") for item in batch]
        assert "hermes_native" in origins
        assert "merchantbench_env" in origins
        env_names = [
            (call.get("function") or {}).get("name")
            for item in batch
            if item.get("role") == "assistant"
            for call in item.get("tool_calls") or []
            if call.get("tool_origin") == "merchantbench_env"
        ]
        assert env_names == ["end_of_step"]

    def test_force_end_of_step_posts_pending_native_tail(self, monkeypatch):
        client = _LoopClient(observations=[_obs("obs-b", 0)])
        runtime, agent = _stub_runtime(monkeypatch, client)

        def _run_native_only(
            user_message=None,
            conversation_history=None,
            system_message=None,
            **kwargs,
        ):
            history = list(conversation_history or [])
            native_asst = _assistant_with_calls(
                [("native-2", "web_search")], content="native-tail"
            )
            native_tool = _tool_result_message("native-2", "web_search", "hits")
            live = history + [
                {"role": "user", "content": user_message},
                native_asst,
                native_tool,
            ]
            agent._session_messages = live
            return {"messages": live}

        agent.run_conversation = _run_native_only
        assert runtime.run() == 0
        assert len(client.act_calls) == 1
        batch = client.act_calls[0]["messages"]
        origins = [item.get("tool_origin") for item in batch]
        assert origins.count("hermes_native") >= 2
        env_names = [
            (call.get("function") or {}).get("name")
            for item in batch
            if item.get("role") == "assistant"
            for call in item.get("tool_calls") or []
            if call.get("tool_origin") == "merchantbench_env"
        ]
        assert env_names[-1] == "end_of_step"
        assert "native-tail" in json.dumps(batch)

    def test_cursor_rebase_after_sanitize_has_no_duplicates_or_gaps(self):
        client = _FakeActClient()
        agent = _fake_session_agent()
        prior = [
            {"role": "user", "content": "first observation"},
            {
                "role": "assistant",
                "content": "checking stock",
                "tool_calls": [
                    _assistant_tool_call("biz-1", "search_products"),
                    _assistant_tool_call("eos-1", "end_of_step"),
                ],
            },
            _tool_result_message("biz-1", "search_products", "found"),
            _tool_result_message("eos-1", "end_of_step", "released"),
        ]
        sanitized = _sanitize_merchantbench_history(prior)
        observation = "second observation"
        native_asst = _assistant_with_calls(
            [("n1", "web_search")], content="native-next"
        )
        native_tool = _tool_result_message("n1", "web_search", "n-ok")
        env_asst = _assistant_with_calls(
            [("e1", "search_products")], content="env-next"
        )
        agent._session_messages = list(sanitized) + [
            {"role": "user", "content": observation},
            native_asst,
            native_tool,
            env_asst,
        ]
        bridge = Bridge(client)
        bridge.bind_agent(agent)
        bridge.registered_tools = ["search_products", "end_of_step"]
        bridge.note_history_rewritten(sanitized, assume_all_posted=True)
        bridge.begin_step(observation_text=observation, history=sanitized)

        bridge.call_environment_tool(
            "search_products", {}, tool_call_id="e1"
        )
        batch = client.act_calls[0]["messages"]
        identity = []
        for item in batch:
            if item.get("role") == "assistant":
                ids = tuple(
                    call.get("id") for call in item.get("tool_calls") or []
                )
                identity.append(("assistant", ids, item.get("tool_origin")))
            elif item.get("role") == "tool":
                identity.append(
                    ("tool", item.get("tool_call_id"), item.get("tool_origin"))
                )
        assert identity == [
            ("assistant", ("n1",), "hermes_native"),
            ("tool", "n1", "hermes_native"),
            ("assistant", ("e1",), "merchantbench_env"),
        ]
        blob = json.dumps(batch)
        assert observation not in blob
        assert "first observation" not in blob
        assert "found" not in blob
        assert "checking stock" not in blob
        assert "eos-1" not in blob
        assert "biz-1" not in blob

    def test_stale_425_does_not_repost_voided_messages(self, monkeypatch):
        client = _StaleOnceLoopClient(
            observations=[_obs("voided", 0), _obs("next", 1)]
        )
        runtime, agent = _stub_runtime(monkeypatch, client)
        turns = []

        def _run_conversation(
            user_message=None,
            conversation_history=None,
            system_message=None,
            **kwargs,
        ):
            turns.append(user_message)
            history = list(conversation_history or [])
            if len(turns) == 1:
                voided = _assistant_with_calls(
                    [("void-1", "search_products")],
                    content="VOIDED_MARKER",
                )
                live = history + [
                    {"role": "user", "content": user_message},
                    voided,
                ]
                agent._session_messages = live
                runtime.bridge.registered_tools = [
                    "search_products",
                    "end_of_step",
                ]
                runtime.bridge.call_environment_tool(
                    "search_products", {"q": "x"}, tool_call_id="void-1"
                )
                return {"messages": live}
            next_asst = _assistant_with_calls(
                [("next-1", "search_products")],
                content="NEXT_MARKER",
            )
            live = history + [
                {"role": "user", "content": user_message},
                next_asst,
            ]
            agent._session_messages = live
            runtime.bridge.call_environment_tool(
                "search_products", {"q": "y"}, tool_call_id="next-1"
            )
            return {"messages": live}

        agent.run_conversation = _run_conversation
        assert runtime.run() == 0
        assert turns == ["voided", "next"]
        assert "VOIDED_MARKER" in json.dumps(client.act_calls[0]["messages"])
        later = json.dumps(
            [call.get("messages") for call in client.act_calls[1:]]
        )
        assert "VOIDED_MARKER" not in later
        assert "NEXT_MARKER" in later


_CANONICAL_USAGE_KEYS = {
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "total",
}


class _FailOnceUsageClient:
    """Client that fails the first record_usage, then succeeds."""

    def __init__(self) -> None:
        self.usage_calls: list[tuple] = []
        self._latest_env_t = 7
        self.fail = True

    def latest_env_t(self):
        return self._latest_env_t

    def record_usage(self, token_usage, **kwargs):
        self.usage_calls.append((dict(token_usage), dict(kwargs)))
        if self.fail:
            self.fail = False
            raise ConnectionError("aux ledger unavailable")
        return {"ok": True, "recorded": True}


class _RecordingCompressor:
    """Stub compressor whose summary method calls module-level ``call_llm``."""

    def __init__(self) -> None:
        self.model = "summary-model"
        self.summary_model = "summary-model"
        self.provider = "openrouter"
        self.base_url = ""
        self.api_key = ""

    def _generate_summary(
        self,
        turns_to_summarize,
        focus_topic=None,
        memory_context="",
    ):
        from agent.context_compressor import call_llm

        call_llm(
            task="compression",
            messages=[{"role": "user", "content": "summarize"}],
        )
        return ("KEPT", turns_to_summarize, focus_topic, memory_context)


class TestMerchantBenchAuxiliaryUsageCapture:
    def test_wrapped_summary_call_flushes_canonical_compression_usage(self):
        response = SimpleNamespace(
            usage={
                "prompt_tokens": 80,
                "completion_tokens": 10,
                "total_tokens": 90,
            },
            model="summary-model",
        )
        compressor = _RecordingCompressor()
        agent = SimpleNamespace(context_compressor=compressor)
        client = _FakeActClient()
        client._latest_env_t = 3
        bridge = Bridge(client)
        with patch(
            "agent.context_compressor.call_llm", return_value=response
        ) as mocked:
            bridge.bind_agent(agent)
            result = compressor._generate_summary(
                [{"role": "user", "content": "hi"}],
                focus_topic="stock",
                memory_context="",
            )
            assert result == (
                "KEPT",
                [{"role": "user", "content": "hi"}],
                "stock",
                "",
            )
            assert mocked.call_count == 1

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert len(client.usage_calls) == 1
        token_usage, kwargs = client.usage_calls[0]
        assert set(token_usage) == _CANONICAL_USAGE_KEYS
        assert token_usage["input"] == 80
        assert token_usage["output"] == 10
        assert token_usage["total"] == 90
        assert kwargs["source"] == "context_compression"
        assert kwargs["usage_id"].startswith("compression-0-")
        assert len(kwargs["usage_id"].split("-")[-1]) == 20

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert len(client.usage_calls) == 1

    def test_review_completion_flush_retries_same_usage_id(self):
        import agent.background_review as background_review

        client = _FailOnceUsageClient()
        agent = SimpleNamespace(
            _user_turn_count=4,
            _session_db=None,
            session_id=None,
            context_compressor=SimpleNamespace(
                _generate_summary=lambda *a, **k: "ok",
            ),
            _spawn_background_review=lambda *a, **k: None,
        )
        bridge = Bridge(client)
        bridge.bind_agent(agent)

        original_return = background_review._record_review_usage_to_parent(
            agent,
            {
                "model": "review-model",
                "provider": "openrouter",
                "input_tokens": 11,
                "output_tokens": 7,
                "cache_read_tokens": 1,
                "cache_write_tokens": 0,
                "reasoning_tokens": 2,
                "api_calls": 1,
                "request_index": 1,
                "cost_usd": 0.01,
                "cost_status": "estimated",
                "cost_source": "none",
            },
        )
        assert original_return is None
        assert len(bridge._review_usage_batches) == 1
        queued_id = bridge._review_usage_batches[0]["usage_id"]
        assert re.fullmatch(
            r"checkpoint-review-turn-4-call-1-[0-9a-f]{20}",
            queued_id,
        )

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert len(client.usage_calls) == 1
        assert len(bridge._review_usage_batches) == 1

        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert len(client.usage_calls) == 2
        assert client.usage_calls[0][1]["usage_id"] == queued_id
        assert client.usage_calls[1][1]["usage_id"] == queued_id
        assert client.usage_calls[1][1]["source"] == "checkpoint_review"
        assert set(client.usage_calls[1][0]) == _CANONICAL_USAGE_KEYS
        assert bridge._review_usage_batches == []

    def test_missing_compressor_and_review_internals_are_noop(self):
        client = _FakeActClient()
        bridge = Bridge(client)
        bridge.bind_agent(SimpleNamespace())
        _flush_pending_auxiliary_usage(bridge, client, attempts=1)
        assert client.usage_calls == []
        assert bridge._review_usage_batches == []

    def test_summary_wrap_is_call_through(self):
        compressor = SimpleNamespace()
        compressor._generate_summary = (
            lambda turns, focus_topic=None, memory_context="": (
                "UNCHANGED",
                turns,
                focus_topic,
            )
        )
        agent = SimpleNamespace(context_compressor=compressor)
        bridge = Bridge(_FakeActClient())
        bridge.bind_agent(agent)
        assert compressor._generate_summary(["t"], focus_topic="x") == (
            "UNCHANGED",
            ["t"],
            "x",
        )
        assert getattr(compressor._generate_summary, "_mb_aux_wrapped", False) is True
