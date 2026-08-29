#!/usr/bin/env python3
"""Behavioral tests for per-task delegate_task model overrides.

No live network. Most cases stub model resolution at the /model chain
(switch_model) and child construction at AIAgent. Integration cases
exercise the real switch_model / picker-inventory chain against the
temp HERMES_HOME from conftest, with catalog fetches forced offline.
"""

import json
import os
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _list_available_models_for_error,
    _strip_model_hidden_task_fields,
    delegate_task,
)


def _make_mock_parent(depth=0):
    """Create a mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "sk-parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.client = object()
    parent.reasoning_config = {"enabled": True, "effort": "high"}
    parent._fallback_chain = [{"model": "fallback-model"}]
    parent._credential_pool = MagicMock()
    parent._credential_pool.current.return_value = "cred-parent-1"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._interrupt_requested = False
    return parent


def _parent_runtime_state(parent):
    """Snapshot parent fields that a model override must not mutate (I1)."""
    pool = getattr(parent, "_credential_pool", None)
    cursor = None
    if pool is not None:
        current = getattr(pool, "current", None)
        if callable(current):
            cursor = current()
    return {
        "model": parent.model,
        "provider": parent.provider,
        "api_mode": parent.api_mode,
        "base_url": parent.base_url,
        "client": getattr(parent, "client", None),
        "reasoning_config": getattr(parent, "reasoning_config", None),
        "fallback_chain": getattr(parent, "_fallback_chain", None),
        "pool_cursor": cursor,
        "api_key": parent.api_key,
    }


def _switch_ok(model, provider, changed, api_key="sk-resolved"):
    result = MagicMock()
    result.success = True
    result.new_model = model
    result.target_provider = provider
    result.provider_changed = changed
    result.api_key = api_key
    result.error_message = ""
    return result


def _switch_fail(message="not in catalog"):
    result = MagicMock()
    result.success = False
    result.new_model = ""
    result.target_provider = ""
    result.provider_changed = False
    result.api_key = ""
    result.error_message = message
    return result


def _mock_child(*, model=None):
    child = MagicMock()
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "api_calls": 1,
    }
    child._delegate_role = "leaf"
    if isinstance(model, str):
        child.model = model
    return child


def _aiagent_copies_model(*_args, **kwargs):
    """AIAgent test double that stamps constructor ``model`` onto the child."""
    return _mock_child(model=kwargs.get("model"))


_LONG_GOAL = "Analyze the repository structure thoroughly"


class TestDelegateModelOverrideSchema(unittest.TestCase):
    def test_schema_advertises_per_task_model_only(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        task_props = props["tasks"]["items"]["properties"]
        self.assertIn("model", task_props)
        self.assertEqual(task_props["model"]["type"], "string")
        self.assertNotIn("model", props)
        for forbidden in ("provider", "base_url", "api_key", "api_mode"):
            self.assertNotIn(forbidden, props)
            self.assertNotIn(forbidden, task_props)

    def test_model_description_is_static_and_states_fail_closed(self):
        from tools.delegate_tool import _build_dynamic_schema_overrides

        desc = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"][
            "properties"
        ]["model"]["description"]
        self.assertIsInstance(desc, str)
        for keyword in ("optional", "available", "omit", "rejected"):
            self.assertIn(keyword, desc.lower(), f"missing keyword {keyword!r}")
        with patch(
            "tools.delegate_tool._get_max_concurrent_children", return_value=3
        ):
            rebuilt_a = _build_dynamic_schema_overrides()
        with patch(
            "tools.delegate_tool._get_max_concurrent_children", return_value=9
        ):
            rebuilt_b = _build_dynamic_schema_overrides()
        desc_a = rebuilt_a["parameters"]["properties"]["tasks"]["items"][
            "properties"
        ]["model"]["description"]
        desc_b = rebuilt_b["parameters"]["properties"]["tasks"]["items"][
            "properties"
        ]["model"]["description"]
        self.assertEqual(desc_a, desc)
        self.assertEqual(desc_b, desc)
        self.assertNotIn("up to 9", desc_a)
        self.assertNotIn("up to 3", desc_a)

    def test_strip_hidden_fields_preserves_model(self):
        tasks = [
            {
                "goal": _LONG_GOAL,
                "model": "google/gemini-flash",
                "acp_command": "should-strip",
            }
        ]
        stripped = _strip_model_hidden_task_fields(tasks)
        self.assertEqual(stripped[0]["model"], "google/gemini-flash")
        self.assertNotIn("acp_command", stripped[0])


class TestDelegateModelOverrideBehavior(unittest.TestCase):
    def setUp(self):
        # * Stubbed /model successes still have to pass picker membership.
        # * These cases pin that gate open so they keep testing credential
        # * overlay, kill-switch, and spawn behavior rather than inventory.
        patcher = patch(
            "tools.delegate_tool._resolved_model_in_picker_inventory",
            return_value=True,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_same_provider_override_inherits_parent_credentials(self):
        parent = _make_mock_parent()
        before = _parent_runtime_state(parent)
        switch = MagicMock(
            return_value=_switch_ok(
                "google/gemini-flash", "openrouter", changed=False
            )
        )
        with (
            patch("hermes_cli.model_switch.switch_model", switch),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                goal=_LONG_GOAL,
                model="gemini-flash",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], "google/gemini-flash")
        self.assertEqual(kwargs["provider"], "openrouter")
        self.assertEqual(kwargs["api_key"], "sk-parent-key")
        self.assertEqual(kwargs["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(_parent_runtime_state(parent), before)
        switch.assert_called()
        self.assertFalse(switch.call_args.kwargs.get("is_global", True))

    def test_cross_provider_resolves_credentials(self):
        parent = _make_mock_parent()
        before = _parent_runtime_state(parent)
        runtime = {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-test",
            "api_mode": "anthropic_messages",
        }
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "claude-opus-4", "anthropic", changed=True, api_key="sk-ant-test"
                ),
            ),
            patch(
                "hermes_cli.model_switch.get_authenticated_provider_slugs",
                return_value=["openrouter", "anthropic"],
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value=runtime,
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                tasks=[{"goal": _LONG_GOAL, "model": "opus"}],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], "claude-opus-4")
        self.assertEqual(kwargs["provider"], "anthropic")
        self.assertEqual(kwargs["api_key"], "sk-ant-test")
        self.assertEqual(kwargs["base_url"], "https://api.anthropic.com")
        self.assertEqual(kwargs["api_mode"], "anthropic_messages")
        self.assertEqual(_parent_runtime_state(parent), before)

    def test_unknown_model_errors_without_spawn_and_lists_available(self):
        parent = _make_mock_parent()
        before = _parent_runtime_state(parent)
        available = ["alpha-model", "beta-model", "gamma-model"]
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_fail("not in catalog"),
            ),
            patch(
                "tools.delegate_tool._list_available_models_for_error",
                return_value=available,
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                goal=_LONG_GOAL,
                model="definitely-not-a-real-model",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        err = parsed["error"]
        self.assertIn("definitely-not-a-real-model", err)
        for name in available:
            self.assertIn(name, err)
        mock_agent.assert_not_called()
        self.assertEqual(_parent_runtime_state(parent), before)

    def test_present_empty_or_whitespace_model_is_rejected_without_spawn(self):
        parent = _make_mock_parent()
        cases = [
            {"tasks": [{"goal": _LONG_GOAL, "model": ""}]},
            {"tasks": [{"goal": _LONG_GOAL, "model": "   \t"}]},
            {"goal": _LONG_GOAL, "model": ""},
            {"goal": _LONG_GOAL, "model": "  "},
            {"tasks": [{"goal": _LONG_GOAL, "model": 123}]},
            {"goal": _LONG_GOAL, "model": ["flash"]},
        ]
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with (
                    patch("hermes_cli.model_switch.switch_model") as switch,
                    patch("run_agent.AIAgent") as mock_agent,
                ):
                    out = delegate_task(parent_agent=parent, **kwargs)
                parsed = json.loads(out)
                self.assertIn("error", parsed)
                self.assertIn("must be", parsed["error"].lower())
                mock_agent.assert_not_called()
                switch.assert_not_called()

    def test_empty_model_on_second_task_spawns_zero_children(self):
        parent = _make_mock_parent()
        with (
            patch("hermes_cli.model_switch.switch_model") as switch,
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "good-model"},
                    {"goal": "Write a thorough summary of findings", "model": "  "},
                ],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn("Task 1", parsed["error"])
        mock_agent.assert_not_called()
        switch.assert_not_called()

    def test_omitted_model_still_inherits(self):
        parent = _make_mock_parent()
        with (
            patch("tools.delegate_tool._load_config", return_value={"max_iterations": 45}),
            patch("hermes_cli.model_switch.switch_model") as switch,
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                tasks=[{"goal": _LONG_GOAL}],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        switch.assert_not_called()
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], parent.model)

    def test_invalid_second_task_spawns_zero_children(self):
        parent = _make_mock_parent()

        def fake_switch(raw_input, **kwargs):
            if "good-model" in raw_input:
                return _switch_ok("good-model", "openrouter", changed=False)
            return _switch_fail("unknown")

        with (
            patch("hermes_cli.model_switch.switch_model", side_effect=fake_switch),
            patch(
                "tools.delegate_tool._list_available_models_for_error",
                return_value=["good-model"],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "good-model"},
                    {"goal": "Write a thorough summary of findings", "model": "bad-model"},
                ],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn("bad-model", parsed["error"])
        mock_agent.assert_not_called()

    def test_kill_switch_rejects_model_without_changing_schema(self):
        parent = _make_mock_parent()
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"][
            "properties"
        ]
        self.assertIn("model", props)
        with (
            patch("tools.delegate_tool._get_allow_model_override", return_value=False),
            patch("hermes_cli.model_switch.switch_model") as switch,
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                goal=_LONG_GOAL,
                model="gemini-flash",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn("allow_model_override", parsed["error"])
        mock_agent.assert_not_called()
        switch.assert_not_called()

    def test_kill_switch_still_allows_inherit_when_model_omitted(self):
        parent = _make_mock_parent()
        with (
            patch("tools.delegate_tool._get_allow_model_override", return_value=False),
            patch("tools.delegate_tool._load_config", return_value={"max_iterations": 45}),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(goal=_LONG_GOAL, parent_agent=parent)
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        mock_agent.assert_called_once()
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], parent.model)

    def test_per_task_model_beats_delegation_config_model(self):
        parent = _make_mock_parent()
        with (
            patch(
                "tools.delegate_tool._load_config",
                return_value={
                    "max_iterations": 45,
                    "model": "delegation-cheap-model",
                    "provider": "",
                    "allow_model_override": True,
                },
            ),
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "per-task-model", "openrouter", changed=False
                ),
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                tasks=[{"goal": _LONG_GOAL, "model": "per-task-model"}],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], "per-task-model")
        self.assertNotEqual(kwargs["model"], "delegation-cheap-model")

    def test_per_task_model_beats_top_level_model(self):
        parent = _make_mock_parent()

        def fake_switch(raw_input, **kwargs):
            return _switch_ok(raw_input, "openrouter", changed=False)

        with (
            patch("hermes_cli.model_switch.switch_model", side_effect=fake_switch),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                tasks=[{"goal": _LONG_GOAL, "model": "per-task-model"}],
                model="top-level-model",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], "per-task-model")
        self.assertNotEqual(kwargs["model"], "top-level-model")

    def test_top_level_model_beats_delegation_config_model(self):
        parent = _make_mock_parent()
        with (
            patch(
                "tools.delegate_tool._load_config",
                return_value={
                    "max_iterations": 45,
                    "model": "delegation-cheap-model",
                    "provider": "",
                    "allow_model_override": True,
                },
            ),
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "top-level-model", "openrouter", changed=False
                ),
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                goal=_LONG_GOAL,
                model="top-level-model",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], "top-level-model")
        self.assertNotEqual(kwargs["model"], "delegation-cheap-model")

    def test_timeout_path_does_not_mutate_parent_runtime_state(self):
        parent = _make_mock_parent()
        before = _parent_runtime_state(parent)
        timeout_result = {
            "task_index": 0,
            "status": "timeout",
            "summary": None,
            "error": "Subagent timed out after 1.0s",
            "exit_reason": "timeout",
            "api_calls": 0,
            "duration_seconds": 1.0,
            "timeout_seconds": 1.0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        }
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "google/gemini-flash", "openrouter", changed=False
                ),
            ),
            patch("run_agent.AIAgent") as mock_agent,
            patch(
                "tools.delegate_tool._run_single_child",
                return_value=timeout_result,
            ),
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                goal=_LONG_GOAL,
                model="gemini-flash",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        self.assertEqual(parsed["results"][0]["status"], "timeout")
        mock_agent.assert_called_once()
        self.assertEqual(_parent_runtime_state(parent), before)

    def test_mixed_batch_gives_each_child_its_own_credentials(self):
        parent = _make_mock_parent()
        runtime = {
            "provider": "anthropic",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-test",
            "api_mode": "anthropic_messages",
        }

        def fake_switch(raw_input, **kwargs):
            if "flash" in raw_input:
                return _switch_ok("google/gemini-flash", "openrouter", changed=False)
            return _switch_ok(
                "claude-opus-4", "anthropic", changed=True, api_key="sk-ant-test"
            )

        with (
            patch("hermes_cli.model_switch.switch_model", side_effect=fake_switch),
            patch(
                "hermes_cli.model_switch.get_authenticated_provider_slugs",
                return_value=["openrouter", "anthropic"],
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value=runtime,
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "gemini-flash"},
                    {
                        "goal": "Write a thorough summary of findings",
                        "model": "opus",
                    },
                ],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        self.assertEqual(mock_agent.call_count, 2)
        first = mock_agent.call_args_list[0].kwargs
        second = mock_agent.call_args_list[1].kwargs
        self.assertEqual(first["model"], "google/gemini-flash")
        self.assertEqual(first["provider"], "openrouter")
        self.assertEqual(first["api_key"], "sk-parent-key")
        self.assertEqual(second["model"], "claude-opus-4")
        self.assertEqual(second["provider"], "anthropic")
        self.assertEqual(second["api_key"], "sk-ant-test")

    def test_review_credentials_cfg_is_not_overlaid_by_model(self):
        parent = _make_mock_parent()
        seen = {}

        def fake_resolve(cfg, parent_agent):
            seen["cfg"] = cfg
            return {
                "model": cfg.get("model"),
                "provider": cfg.get("provider"),
                "base_url": None,
                "api_key": None,
                "api_mode": None,
                "command": None,
                "args": None,
            }

        override = {"provider": "openrouter", "model": "review-model-x"}
        with (
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                side_effect=fake_resolve,
            ),
            patch("hermes_cli.model_switch.switch_model") as switch,
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.return_value = _mock_child()
            out = delegate_task(
                goal=_LONG_GOAL,
                model="should-be-ignored",
                parent_agent=parent,
                credentials_cfg=override,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        self.assertEqual(seen["cfg"], override)
        switch.assert_not_called()

    def test_available_models_error_list_is_capped(self):
        rows = [{"models": [f"m{i}" for i in range(80)]}]
        with (
            patch("hermes_cli.inventory.load_picker_context"),
            patch(
                "hermes_cli.inventory.build_models_payload",
                return_value={"providers": rows},
            ),
        ):
            ids = _list_available_models_for_error()
        self.assertEqual(len(ids), 50)
        self.assertEqual(ids[0], "m0")
        self.assertEqual(ids[-1], "m49")

    def test_unauthenticated_cross_provider_fails_closed(self):
        parent = _make_mock_parent()
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "claude-opus-4", "anthropic", changed=True, api_key=""
                ),
            ),
            patch(
                "hermes_cli.model_switch.get_authenticated_provider_slugs",
                return_value=["openrouter"],
            ),
            patch(
                "tools.delegate_tool._list_available_models_for_error",
                return_value=["openrouter/foo"],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                goal=_LONG_GOAL,
                model="opus",
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn("no credentials", parsed["error"].lower())
        self.assertIn("openrouter/foo", parsed["error"])
        mock_agent.assert_not_called()


class TestDelegateModelOverrideInventory(unittest.TestCase):
    def test_unknown_model_rejected_on_soft_accept_providers(self):
        cases = (
            ("anthropic", "not-a-real-claude-id-xyz"),
            ("minimax", "not-a-real-minimax-id-xyz"),
            ("custom:local", "not-a-real-custom-id-xyz"),
        )
        listed = ["listed-model-alpha", "listed-model-beta"]
        for provider, fake in cases:
            with self.subTest(provider=provider):
                parent = _make_mock_parent()
                parent.provider = provider if provider != "custom:local" else "openrouter"
                with (
                    patch(
                        "hermes_cli.model_switch.switch_model",
                        return_value=_switch_ok(
                            fake, provider, changed=(provider != parent.provider)
                        ),
                    ),
                    patch(
                        "hermes_cli.model_switch.get_authenticated_provider_slugs",
                        return_value=["openrouter", provider],
                    ),
                    patch(
                        "tools.delegate_tool._picker_inventory_rows",
                        return_value=[{"slug": provider, "models": listed}],
                    ),
                    patch("run_agent.AIAgent") as mock_agent,
                ):
                    out = delegate_task(
                        goal=_LONG_GOAL,
                        model=fake,
                        parent_agent=parent,
                    )
                parsed = json.loads(out)
                self.assertIn("error", parsed)
                self.assertIn(fake, parsed["error"])
                self.assertIn("listed-model-alpha", parsed["error"])
                mock_agent.assert_not_called()

    def test_membership_uses_full_inventory_not_error_cap(self):
        parent = _make_mock_parent()
        full = [f"catalog-model-{i}" for i in range(80)]
        beyond_cap = full[79]
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    beyond_cap, "openrouter", changed=False
                ),
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[{"slug": "openrouter", "models": full}],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                goal=_LONG_GOAL,
                model=beyond_cap,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], beyond_cap)

    def test_aggregator_slug_in_inventory_is_allowed(self):
        parent = _make_mock_parent()
        slug = "google/gemini-2.5-flash"
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(slug, "openrouter", changed=False),
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[{"slug": "openrouter", "models": [slug]}],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                goal=_LONG_GOAL,
                model=slug,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], slug)

    def test_configured_provider_model_is_allowed(self):
        parent = _make_mock_parent()
        configured = "local-llama-3"
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    configured, "my-ollama", changed=True, api_key="sk-local"
                ),
            ),
            patch(
                "hermes_cli.model_switch.get_authenticated_provider_slugs",
                return_value=["openrouter", "my-ollama"],
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[
                    {"slug": "my-ollama", "models": [configured, "other-local"]},
                ],
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "provider": "custom",
                    "base_url": "http://127.0.0.1:11434/v1",
                    "api_key": "sk-local",
                    "api_mode": "chat_completions",
                },
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                goal=_LONG_GOAL,
                model=configured,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], configured)
        self.assertEqual(kwargs["provider"], "my-ollama")

    def test_openrouter_routing_variant_accepted_when_base_in_inventory(self):
        parent = _make_mock_parent()
        variant = "anthropic/claude-sonnet-4:nitro"
        base = "anthropic/claude-sonnet-4"
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(variant, "openrouter", changed=False),
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[{"slug": "openrouter", "models": [base]}],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                goal=_LONG_GOAL,
                model=variant,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed, parsed)
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], variant)

    def test_openrouter_routing_variant_rejected_when_base_unknown(self):
        parent = _make_mock_parent()
        variant = "unknown-model:nitro"
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(variant, "openrouter", changed=False),
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[
                    {
                        "slug": "openrouter",
                        "models": ["anthropic/claude-sonnet-4"],
                    }
                ],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                goal=_LONG_GOAL,
                model=variant,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn(variant, parsed["error"])
        mock_agent.assert_not_called()

    def test_non_openrouter_provider_does_not_strip_routing_variant(self):
        parent = _make_mock_parent()
        parent.provider = "anthropic"
        parent.model = "claude-opus-4"
        variant = "claude-opus-4:nitro"
        with (
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(variant, "anthropic", changed=False),
            ),
            patch(
                "tools.delegate_tool._picker_inventory_rows",
                return_value=[{"slug": "anthropic", "models": ["claude-opus-4"]}],
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                goal=_LONG_GOAL,
                model=variant,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn(variant, parsed["error"])
        mock_agent.assert_not_called()


class TestDelegateModelOverrideLazySharedCreds(unittest.TestCase):
    def setUp(self):
        patcher = patch(
            "tools.delegate_tool._resolved_model_in_picker_inventory",
            return_value=True,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def test_broken_pin_all_overrides_succeeds(self):
        parent = _make_mock_parent()
        broken = {
            "max_iterations": 45,
            "provider": "definitely-not-a-provider",
            "model": "ignored-pin-model",
            "allow_model_override": True,
        }
        with (
            patch("tools.delegate_tool._load_config", return_value=broken),
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "google/gemini-flash", "openrouter", changed=False
                ),
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "gemini-flash"},
                    {
                        "goal": "Write a thorough summary of findings",
                        "model": "gemini-flash",
                    },
                ],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed)
        self.assertEqual(mock_agent.call_count, 2)
        for call in mock_agent.call_args_list:
            self.assertEqual(call.kwargs["model"], "google/gemini-flash")

    def test_broken_pin_mixed_batch_errors_without_spawn(self):
        parent = _make_mock_parent()
        broken = {
            "max_iterations": 45,
            "provider": "definitely-not-a-provider",
            "model": "ignored-pin-model",
            "allow_model_override": True,
        }
        with (
            patch("tools.delegate_tool._load_config", return_value=broken),
            patch(
                "hermes_cli.model_switch.switch_model",
                return_value=_switch_ok(
                    "google/gemini-flash", "openrouter", changed=False
                ),
            ),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "gemini-flash"},
                    {"goal": "Write a thorough summary of findings"},
                ],
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn("definitely-not-a-provider", parsed["error"])
        mock_agent.assert_not_called()

    def test_broken_pin_without_override_still_errors(self):
        parent = _make_mock_parent()
        broken = {
            "max_iterations": 45,
            "provider": "definitely-not-a-provider",
            "allow_model_override": True,
        }
        with (
            patch("tools.delegate_tool._load_config", return_value=broken),
            patch("run_agent.AIAgent") as mock_agent,
        ):
            out = delegate_task(goal=_LONG_GOAL, parent_agent=parent)
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        mock_agent.assert_not_called()


class TestDelegateModelOverrideRealResolver(unittest.TestCase):
    def setUp(self):
        from hermes_cli.models import _PROVIDER_MODELS

        catalog = list(_PROVIDER_MODELS.get("anthropic") or [])
        self.assertGreaterEqual(len(catalog), 2)
        self.catalog_model = catalog[0]
        self.parent_model = catalog[1]
        patches = (
            patch("hermes_cli.models.cached_provider_model_ids", return_value=[]),
            patch("hermes_cli.models._fetch_anthropic_models", return_value=None),
            patch("hermes_cli.models.fetch_api_models", return_value=[]),
            patch("hermes_cli.models.get_curated_nous_model_ids", return_value=[]),
            patch("hermes_cli.models.fetch_ollama_cloud_models", return_value=[]),
            patch("hermes_cli.models.fetch_lmstudio_models", return_value=[]),
            patch("agent.models_dev.fetch_models_dev", return_value={}),
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-test-fixture"}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _anthropic_parent(self):
        parent = _make_mock_parent()
        parent.provider = "anthropic"
        parent.model = self.parent_model
        parent.api_key = "sk-ant-test-fixture"
        parent.base_url = "https://api.anthropic.com"
        parent.api_mode = "anthropic_messages"
        return parent

    def test_real_switch_model_builds_child_from_static_catalog(self):
        parent = self._anthropic_parent()
        with patch("run_agent.AIAgent") as mock_agent:
            mock_agent.side_effect = _aiagent_copies_model
            out = delegate_task(
                goal=_LONG_GOAL,
                model=self.catalog_model,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertNotIn("error", parsed, parsed)
        mock_agent.assert_called_once()
        _, kwargs = mock_agent.call_args
        self.assertEqual(kwargs["model"], self.catalog_model)
        self.assertEqual(kwargs["api_key"], "sk-ant-test-fixture")

    def test_real_resolver_rejects_unknown_model_without_spawn(self):
        parent = self._anthropic_parent()
        unknown = "definitely-not-in-anthropic-catalog-zzzz"
        with patch("run_agent.AIAgent") as mock_agent:
            out = delegate_task(
                goal=_LONG_GOAL,
                model=unknown,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertIn("error", parsed)
        self.assertIn(unknown, parsed["error"])
        mock_agent.assert_not_called()


class TestDelegateModelOverrideAsyncPerChildModel(unittest.TestCase):
    def setUp(self):
        from tools import async_delegation as ad
        from tools.process_registry import process_registry

        self._ad = ad
        self._registry = process_registry
        ad._reset_for_tests()
        while not process_registry.completion_queue.empty():
            process_registry.completion_queue.get_nowait()
        patcher = patch(
            "tools.delegate_tool._resolved_model_in_picker_inventory",
            return_value=True,
        )
        self.addCleanup(patcher.stop)
        patcher.start()

    def tearDown(self):
        deadline = time.monotonic() + 2.0
        while self._ad.active_count() and time.monotonic() < deadline:
            time.sleep(0.02)
        self._ad._reset_for_tests()
        while not self._registry.completion_queue.empty():
            self._registry.completion_queue.get_nowait()

    def test_async_mixed_batch_completion_carries_per_child_model(self):
        from tools.process_registry import format_process_notification

        parent = _make_mock_parent()
        parent.session_id = "sess-async-models"

        def fake_switch(raw_input, **kwargs):
            if "flash" in raw_input:
                return _switch_ok("google/gemini-flash", "openrouter", changed=False)
            return _switch_ok("google/gemini-pro", "openrouter", changed=False)

        def fake_run(task_index, goal, child, parent_agent, **kw):
            return {
                "task_index": task_index,
                "status": "completed",
                "summary": f"done:{goal[:12]}",
                "api_calls": 1,
                "duration_seconds": 0.01,
                "exit_reason": "completed",
            }

        with (
            patch("hermes_cli.model_switch.switch_model", side_effect=fake_switch),
            patch("run_agent.AIAgent", side_effect=_aiagent_copies_model),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            out = delegate_task(
                tasks=[
                    {"goal": _LONG_GOAL, "model": "gemini-flash"},
                    {
                        "goal": "Write a thorough summary of findings",
                        "model": "gemini-pro",
                    },
                ],
                background=True,
                parent_agent=parent,
            )
        parsed = json.loads(out)
        self.assertEqual(parsed.get("status"), "dispatched")
        deleg_id = parsed["delegation_id"]
        deadline = time.monotonic() + 5.0
        evt = None
        while time.monotonic() < deadline:
            if not self._registry.completion_queue.empty():
                candidate = self._registry.completion_queue.get_nowait()
                if candidate.get("delegation_id") == deleg_id:
                    evt = candidate
                    break
            time.sleep(0.02)
        self.assertIsNotNone(evt)
        results = sorted(evt["results"], key=lambda r: r.get("task_index", 0))
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["model"], "google/gemini-flash")
        self.assertEqual(results[1]["model"], "google/gemini-pro")
        self.assertEqual(
            evt.get("models"),
            ["google/gemini-flash", "google/gemini-pro"],
        )
        text = format_process_notification(evt)
        self.assertIn("google/gemini-flash", text)
        self.assertIn("google/gemini-pro", text)


class TestDelegateModelOverrideCrashRecovery(unittest.TestCase):
    def _recover_event(self, record):
        import tempfile
        from pathlib import Path

        from tools import async_delegation as ad

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = Path(tmp) / "state.db"
            with patch.object(ad, "_db_path", return_value=db):
                ad._persist_dispatch(record)
                with patch("gateway.status._pid_exists", return_value=False):
                    recovered = ad.recover_abandoned_delegations()
                self.assertEqual(recovered, 1)
                with ad._DB_LOCK, ad._transaction() as conn:
                    row = conn.execute(
                        "SELECT event_json FROM async_delegations "
                        "WHERE delegation_id=?",
                        (record["delegation_id"],),
                    ).fetchone()
                payload = row[0] if row else None
        self.assertIsNotNone(payload)
        return json.loads(payload)

    def test_recover_abandoned_preserves_per_child_models(self):
        from tools.process_registry import format_process_notification

        models = ["google/gemini-flash", "google/gemini-pro"]
        evt = self._recover_event(
            {
                "delegation_id": "d-recover-models",
                "goal": "2 parallel subagents",
                "goals": [_LONG_GOAL, "Write a thorough summary of findings"],
                "model": models[0],
                "models": models,
                "is_batch": True,
                "session_key": "s-recover",
                "origin_ui_session_id": "",
                "parent_session_id": "s-recover",
                "dispatched_at": time.time() - 5,
            }
        )
        self.assertEqual(evt.get("models"), models)
        text = format_process_notification(evt)
        self.assertIsInstance(text, str)

    def test_recover_abandoned_legacy_without_models_does_not_crash(self):
        from tools.process_registry import format_process_notification

        evt = self._recover_event(
            {
                "delegation_id": "d-recover-legacy",
                "goal": _LONG_GOAL,
                "model": "google/gemini-flash",
                "is_batch": True,
                "goals": [_LONG_GOAL],
                "session_key": "s-legacy",
                "origin_ui_session_id": "",
                "parent_session_id": "s-legacy",
                "dispatched_at": time.time() - 5,
            }
        )
        self.assertTrue(evt.get("models") in (None, []))
        text = format_process_notification(evt)
        self.assertIsInstance(text, str)
        with_results = dict(evt)
        with_results["error"] = None
        with_results["status"] = "completed"
        with_results["results"] = [
            {"task_index": 0, "status": "completed", "summary": "ok"}
        ]
        text = format_process_notification(with_results)
        self.assertIsInstance(text, str)
        self.assertIn("ok", text)


if __name__ == "__main__":
    unittest.main()
