"""Outer MerchantBench observation loop driving a persistent Hermes AIAgent."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

import requests

from merchantbench_adapter.bridge import (
    TOOLSET_NAME,
    Bridge,
    _flush_pending_auxiliary_usage,
    refresh_merchantbench_tools,
    set_bridge,
)
from merchantbench_adapter.history import _sanitize_merchantbench_agent_history

logger = logging.getLogger(__name__)

FRAMEWORK = "hermes"
MERCHANTBENCH_CAPABILITY_GUIDANCE = (
    "Use any available tools, write and execute code, persist useful memory, "
    "and improve skills when helpful to maximize final net_assets."
)

# Sentinel: env var absent (distinct from an explicit empty / "any" override).
_UNSET = object()


def _ensure_sdk_on_path() -> None:
    sdk_root = os.environ.get("MERCHANTBENCH_AGENT_SDK_ROOT")
    candidates: list[Path] = []
    if sdk_root:
        candidates.append(Path(sdk_root))
    # Sibling checkouts: .../MerchantBench/agent next to .../Hermes
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parents[2] / "MerchantBench" / "agent",
            here.parents[1].parent / "MerchantBench" / "agent",
            Path(r"G:\GitHubImports\MerchantBench\agent"),
        ]
    )
    for candidate in candidates:
        if (candidate / "sdk" / "merchantbench_tool_client.py").is_file():
            path = str(candidate)
            if path not in sys.path:
                sys.path.insert(0, path)
            return
    raise RuntimeError(
        "MerchantBench SDK not found. Set MERCHANTBENCH_AGENT_SDK_ROOT to the "
        "MerchantBench `agent/` directory containing sdk/merchantbench_tool_client.py."
    )


def _load_client_class():
    _ensure_sdk_on_path()
    from sdk.merchantbench_tool_client import MerchantBenchToolClient

    return MerchantBenchToolClient


def _resolve_credentials(
    model: Optional[str],
    *,
    openai_api_key: Optional[str] = None,
    openai_base_url: Optional[str] = None,
    provider: Optional[str] = None,
) -> dict[str, Optional[str]]:
    api_key = (
        openai_api_key
        or os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    base_url = (
        openai_base_url
        or os.environ.get("OPENAI_BASE_URL")
        or "https://openrouter.ai/api/v1"
    )
    resolved_model = (
        model
        or os.environ.get("MODEL_NAME")
        or "deepseek/deepseek-v4-flash-0731"
    )
    resolved_provider = (
        (provider or "").strip()
        or (os.environ.get("HERMES_PROVIDER") or "").strip()
        or "openrouter"
    )
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY / OPENAI_API_KEY is required for Hermes MerchantBench runs."
        )
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": resolved_model,
        "provider": resolved_provider,
    }


def _default_run_local_agent_kwargs() -> dict[str, Any]:
    """Return kwargs that emit no reasoning override and no provider prefs."""
    return {
        "reasoning_config": None,
        "providers_allowed": None,
        "providers_ignored": None,
        "providers_order": None,
        "provider_sort": None,
        "provider_require_parameters": False,
        "provider_data_collection": None,
    }


def _parse_openrouter_providers_env() -> Any:
    """Parse MERCHANTBENCH_OPENROUTER_PROVIDERS.

    Returns ``_UNSET`` when the variable is absent so config can win.
    Empty / ``*`` / ``any`` / ``all`` is an explicit override to ``None``.
    """
    if "MERCHANTBENCH_OPENROUTER_PROVIDERS" not in os.environ:
        return _UNSET
    raw = (os.environ.get("MERCHANTBENCH_OPENROUTER_PROVIDERS") or "").strip()
    if not raw or raw.lower() in {"*", "any", "all"}:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _parse_reasoning_effort_value(value: Any) -> Optional[dict[str, Any]]:
    """Parse a reasoning effort via ``hermes_constants.parse_reasoning_effort``.

    Non-string non-None values are coerced with ``str()`` so YAML numbers/bools
    do not skip the validator.
    """
    from hermes_constants import parse_reasoning_effort

    if value is None:
        return None
    if isinstance(value, str):
        return parse_reasoning_effort(value)
    return parse_reasoning_effort(str(value))


def _reasoning_config_from_env() -> Any:
    """Return a parsed env override, or ``_UNSET`` when no env source wins."""
    from hermes_constants import parse_reasoning_effort

    if "MERCHANTBENCH_REASONING_EFFORT" not in os.environ:
        return _UNSET
    parsed = parse_reasoning_effort(os.environ.get("MERCHANTBENCH_REASONING_EFFORT"))
    if parsed is not None:
        return parsed
    return _UNSET


def _load_run_local_agent_kwargs() -> dict[str, Any]:
    """Resolve reasoning and OpenRouter routing kwargs for the MerchantBench agent.

    Precedence for ``reasoning_config``:
      ``MERCHANTBENCH_REASONING_EFFORT`` >
      ``agent.reasoning_effort`` in ``$HERMES_HOME/config.yaml`` > ``None``.

    Precedence for ``providers_allowed``:
      ``MERCHANTBENCH_OPENROUTER_PROVIDERS`` (empty/``*``/``any``/``all`` →
      ``None``) > ``provider_routing.only`` > ``None``.

    Never raises: missing or malformed config logs a warning and falls back
    to Hermes defaults (no reasoning override, no provider prefs). Env
    overrides are applied even when config loading fails.
    """
    kwargs = _default_run_local_agent_kwargs()
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        logger.warning(
            "Failed to load run-local Hermes config; using default reasoning "
            "and provider routing",
            exc_info=True,
        )
        cfg = None

    if isinstance(cfg, dict):
        try:
            agent_cfg = cfg.get("agent")
            if isinstance(agent_cfg, dict):
                effort = agent_cfg.get("reasoning_effort")
                if effort is not None:
                    kwargs["reasoning_config"] = _parse_reasoning_effort_value(
                        effort
                    )

            provider_routing = cfg.get("provider_routing")
            if isinstance(provider_routing, dict):
                kwargs["providers_allowed"] = provider_routing.get("only")
                kwargs["providers_ignored"] = provider_routing.get("ignore")
                kwargs["providers_order"] = provider_routing.get("order")
                kwargs["provider_sort"] = provider_routing.get("sort")
                kwargs["provider_require_parameters"] = bool(
                    provider_routing.get("require_parameters", False)
                )
                kwargs["provider_data_collection"] = provider_routing.get(
                    "data_collection"
                )
        except Exception:
            logger.warning(
                "Failed to parse run-local Hermes reasoning/provider routing; "
                "using defaults",
                exc_info=True,
            )
            kwargs = _default_run_local_agent_kwargs()

    try:
        env_reasoning = _reasoning_config_from_env()
        if env_reasoning is not _UNSET:
            kwargs["reasoning_config"] = env_reasoning
        env_providers = _parse_openrouter_providers_env()
        if env_providers is not _UNSET:
            kwargs["providers_allowed"] = env_providers
    except Exception:
        logger.warning(
            "Failed to apply MerchantBench env overrides for reasoning/"
            "provider routing; keeping config values",
            exc_info=True,
        )
    return kwargs


def _obs_user_message(obs: dict[str, Any]) -> str:
    text = obs.get("text")
    if isinstance(text, str) and text.strip():
        return text
    tick = obs.get("tick") or {}
    return (
        "MerchantBench decision window.\n"
        f"day={tick.get('day')} hour={tick.get('hour')} step={tick.get('step')}\n"
        "Inspect shop state with MerchantBench tools, act if needed, then call "
        "end_of_step when finished with this wakeup."
    )


def _tick_step(obs: dict[str, Any]) -> int:
    tick = obs.get("tick") or {}
    if tick.get("step") is not None:
        return int(tick["step"])
    day = int(tick.get("day") or 1)
    hour = int(tick.get("hour") or 0)
    return (day - 1) * 24 + hour


def _openrouter_app_headers() -> dict[str, str]:
    return {
        "HTTP-Referer": (
            os.environ.get("OPENROUTER_HTTP_REFERER")
            or "https://github.com/Artemonim/merchantbench"
        ),
        "X-Title": os.environ.get("OPENROUTER_X_TITLE") or "MerchantBench",
    }


def _apply_openrouter_app_headers(agent: Any) -> None:
    """Force OpenRouter dashboard App name to MerchantBench (not Hermes Agent)."""
    headers = _openrouter_app_headers()
    client_kwargs = getattr(agent, "_client_kwargs", None)
    if isinstance(client_kwargs, dict):
        merged = dict(client_kwargs.get("default_headers") or {})
        merged.update(headers)
        client_kwargs["default_headers"] = merged
    # Also keep the live OpenAI client in sync when already constructed.
    client = getattr(agent, "client", None) or getattr(agent, "_client", None)
    if client is not None:
        try:
            default_headers = getattr(client, "default_headers", None)
            if default_headers is not None and hasattr(default_headers, "update"):
                default_headers.update(headers)
        except Exception:
            logger.debug("Could not update live client OpenRouter headers", exc_info=True)


class MerchantBenchHermesRuntime:
    """ONE AIAgent + ONE conversation history across all decision windows."""

    def __init__(
        self,
        *,
        base_url: str,
        run_id: str,
        agent_id: str = "agent_0",
        model: Optional[str] = None,
        max_hops_per_step: int = 30,
        max_steps: Optional[int] = None,
        quiet: bool = True,
        timeout: float = 60.0,
        observation_timeout: float = 30.0,
        retry_sleep: float = 1.0,
        max_observations: Optional[int] = None,
        provider: Optional[str] = None,
        openai_base_url: Optional[str] = None,
        openai_api_key: Optional[str] = None,
    ) -> None:
        creds = _resolve_credentials(
            model,
            openai_api_key=openai_api_key,
            openai_base_url=openai_base_url,
            provider=provider,
        )
        client_cls = _load_client_class()
        self.client = client_cls(
            base_url,
            run_id,
            agent_id,
            timeout=timeout,
            observation_timeout=observation_timeout,
        )
        self.model = creds["model"]
        self.api_key = creds["api_key"]
        self.base_url_llm = creds["base_url"]
        self.provider = creds["provider"]
        self.max_hops_per_step = max_hops_per_step
        self.max_steps = max_steps
        self.quiet = quiet
        self.retry_sleep = retry_sleep
        self.max_observations = max_observations
        self.history: list[dict[str, Any]] = []
        self.system_prompt: Optional[str] = None
        self.agent = None
        self.bridge = set_bridge(
            Bridge(
                self.client,
                max_hops_per_step=max_hops_per_step,
                quiet=quiet,
            )
        )

    def register(self) -> None:
        self.client.register(
            framework=FRAMEWORK,
            model=self.model,
            version="0.1.0-reconstructed",
            extra={
                "adapter": "merchantbench_adapter",
                "max_hops_per_step": self.max_hops_per_step,
            },
        )
        names = refresh_merchantbench_tools(self.bridge, self.agent)
        if not self.quiet:
            logger.info("Registered %d MerchantBench tools", len(names))

    def _ensure_agent(self, system_prompt: Optional[str]) -> None:
        if self.agent is not None:
            if system_prompt:
                self.system_prompt = system_prompt
                self.agent.ephemeral_system_prompt = system_prompt
            return

        from run_agent import AIAgent

        self.system_prompt = system_prompt
        agent_kwargs = _load_run_local_agent_kwargs()
        self.agent = AIAgent(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url_llm,
            provider=self.provider or "openrouter",
            quiet_mode=self.quiet,
            max_iterations=self.max_hops_per_step,
            ephemeral_system_prompt=system_prompt,
            skip_context_files=True,
            skip_memory=False,
            platform="merchantbench",
            # Keep Hermes native tools; MerchantBench tools are an extra toolset.
            enabled_toolsets=None,
            **agent_kwargs,
        )
        # * Force headless values after init; AIAgent may overwrite them from yaml.
        self.agent._disable_streaming = True
        self.agent._api_max_retries = 6
        _apply_openrouter_app_headers(self.agent)
        self.bridge.bind_agent(self.agent)
        # Ensure the merchantbench toolset stays visible even if defaults change.
        enabled = getattr(self.agent, "enabled_toolsets", None)
        if isinstance(enabled, list) and TOOLSET_NAME not in enabled:
            enabled.append(TOOLSET_NAME)

    def _flush_auxiliary_usage(self, *, attempts: int = 3) -> None:
        """Flush compression/review usage to the env auxiliary ledger.

        Flush-before-act (attempts=1) lives in the bridge locked /act path.
        HTTP 410 / max_steps / max_observations call sites use attempts=3.
        """
        _flush_pending_auxiliary_usage(
            self.bridge, self.client, attempts=attempts
        )

    def _apply_brief_system_prompt(self, obs: dict[str, Any]) -> None:
        """Extract the dict brief's system_prompt once and suffix capability guidance."""
        if self.system_prompt is not None:
            return
        brief = obs.get("brief")
        if not isinstance(brief, dict):
            return
        system_prompt = brief.get("system_prompt")
        if not system_prompt:
            return
        self.system_prompt = (
            f"{str(system_prompt).rstrip()}\n\n"
            f"{MERCHANTBENCH_CAPABILITY_GUIDANCE}"
        )

    def _discard_stale_step(self, history_before_step: list[dict[str, Any]]) -> bool:
        """Drop a voided decision window and restore the pre-step history."""
        self.history = list(history_before_step)
        self.bridge.on_stale_discard(self.history)
        try:
            self.agent.clear_interrupt()
        except Exception:
            logger.debug("clear_interrupt failed after stale_step", exc_info=True)
        return False

    def _drive_step(self, obs: dict[str, Any]) -> bool:
        """Drive one decision window. Return True when the step completed."""
        self._apply_brief_system_prompt(obs)
        self._ensure_agent(self.system_prompt)
        refresh_merchantbench_tools(self.bridge, self.agent)

        self.history = _sanitize_merchantbench_agent_history(
            self.agent, self.history
        )
        self.bridge.note_history_rewritten(self.history, assume_all_posted=True)
        history_before_step = list(self.history)
        self.bridge.reset_step_flags()
        user_message = _obs_user_message(obs)
        self.bridge.begin_step(
            observation_text=user_message, history=self.history
        )

        try:
            result = self.agent.run_conversation(
                user_message=user_message,
                conversation_history=list(self.history),
                system_message=self.system_prompt,
            )
        except Exception:
            if self.bridge.stale_step:
                return self._discard_stale_step(history_before_step)
            raise
        messages = result.get("messages") if isinstance(result, dict) else None
        if isinstance(messages, list):
            self.history = messages
            self.bridge.bind_history(self.history)

        if self.bridge.stale_step:
            return self._discard_stale_step(history_before_step)

        if not self.bridge.step_released:
            self.bridge.force_end_of_step(
                "Hermes wakeup ended without end_of_step "
                f"(max_hops={self.max_hops_per_step})"
            )
            if self.bridge.stale_step:
                return self._discard_stale_step(history_before_step)

        self.history = _sanitize_merchantbench_agent_history(
            self.agent, self.history
        )
        self.bridge.note_history_rewritten(self.history, assume_all_posted=True)
        try:
            self.agent.clear_interrupt()
        except Exception:
            logger.debug("clear_interrupt failed", exc_info=True)
        return True

    def run(self) -> int:
        self.register()
        if not self.quiet:
            print(
                f"[{FRAMEWORK}] model={self.model} "
                f"max_hops={self.max_hops_per_step} max_steps={self.max_steps}",
                file=sys.stderr,
                flush=True,
            )

        completed_observations = 0
        while True:
            try:
                obs = self.client.observation()
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 410:
                    self._flush_auxiliary_usage(attempts=3)
                    if not self.quiet:
                        print("[done] run finished (HTTP 410)", file=sys.stderr)
                    return 0
                if not self.quiet:
                    print(
                        f"[merchantbench-adapter] observation error: {exc}",
                        file=sys.stderr,
                    )
                time.sleep(self.retry_sleep)
                continue
            except requests.RequestException as exc:
                logger.warning(
                    "MerchantBench observation transport error: %s", exc
                )
                if not self.quiet:
                    print(
                        f"[merchantbench-adapter] observation error: {exc}",
                        file=sys.stderr,
                    )
                time.sleep(self.retry_sleep)
                continue

            step = _tick_step(obs)
            if self.max_steps is not None and step >= int(self.max_steps):
                self._flush_auxiliary_usage(attempts=3)
                if not self.quiet:
                    print(f"[done] reached step={step}", file=sys.stderr)
                return 0

            tick = obs.get("tick") or {}
            if not self.quiet:
                print(
                    f"[step] day={tick.get('day')} hour={tick.get('hour')} "
                    f"step={tick.get('step')}",
                    file=sys.stderr,
                    flush=True,
                )
            try:
                completed = self._drive_step(obs)
            except Exception as exc:
                logger.exception("step crash at t=%s", step)
                self.bridge.force_end_of_step(
                    f"step-crash: {type(exc).__name__}: {exc}"
                )
                if self.bridge.stale_step:
                    continue
                completed = True
            if not completed:
                continue
            completed_observations += 1
            if (
                self.max_observations is not None
                and completed_observations >= int(self.max_observations)
            ):
                self._flush_auxiliary_usage(attempts=3)
                if not self.quiet:
                    print(
                        f"[done] reached max_observations={self.max_observations}",
                        file=sys.stderr,
                    )
                return 0
        return 0


def run_adapter(**kwargs: Any) -> int:
    runtime = MerchantBenchHermesRuntime(**kwargs)
    return runtime.run()
