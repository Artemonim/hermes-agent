"""Outer MerchantBench observation loop driving a persistent Hermes AIAgent."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any, Optional

import requests

from merchantbench_adapter.bridge import (
    TOOLSET_NAME,
    Bridge,
    register_merchantbench_tools,
    set_bridge,
)

logger = logging.getLogger(__name__)

FRAMEWORK = "hermes-reconstructed"


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


def _resolve_credentials(model: Optional[str]) -> dict[str, Optional[str]]:
    api_key = (
        os.environ.get("OPENROUTER_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )
    base_url = os.environ.get("OPENAI_BASE_URL") or "https://openrouter.ai/api/v1"
    resolved_model = (
        model
        or os.environ.get("MODEL_NAME")
        or "deepseek/deepseek-v4-flash-0731"
    )
    if not api_key:
        raise SystemExit(
            "OPENROUTER_API_KEY / OPENAI_API_KEY is required for Hermes MerchantBench runs."
        )
    return {
        "api_key": api_key,
        "base_url": base_url,
        "model": resolved_model,
    }


def _providers_allowed() -> Optional[list[str]]:
    raw = os.environ.get("MERCHANTBENCH_OPENROUTER_PROVIDERS", "baidu").strip()
    if not raw or raw.lower() in {"*", "any", "all"}:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _reasoning_config() -> dict[str, Any]:
    effort = (
        os.environ.get("MERCHANTBENCH_REASONING_EFFORT")
        or os.environ.get("HERMES_REASONING_EFFORT")
        or "xhigh"
    ).strip()
    return {"effort": effort, "enabled": True}


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
    ) -> None:
        creds = _resolve_credentials(model)
        client_cls = _load_client_class()
        self.client = client_cls(base_url, run_id, agent_id)
        self.model = creds["model"]
        self.api_key = creds["api_key"]
        self.base_url_llm = creds["base_url"]
        self.max_hops_per_step = max_hops_per_step
        self.max_steps = max_steps
        self.quiet = quiet
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
        names = register_merchantbench_tools(self.bridge)
        if not self.quiet:
            logger.info("Registered %d MerchantBench tools", len(names))

    def _ensure_agent(self, system_prompt: Optional[str]) -> None:
        if self.agent is not None:
            if system_prompt and not self.system_prompt:
                self.system_prompt = system_prompt
                self.agent.ephemeral_system_prompt = system_prompt
            return

        from run_agent import AIAgent

        self.system_prompt = system_prompt
        providers_allowed = _providers_allowed()
        self.agent = AIAgent(
            model=self.model,
            api_key=self.api_key,
            base_url=self.base_url_llm,
            provider="openrouter",
            quiet_mode=True,
            max_iterations=self.max_hops_per_step,
            ephemeral_system_prompt=system_prompt,
            skip_context_files=False,
            skip_memory=False,
            providers_allowed=providers_allowed,
            provider_require_parameters=True,
            reasoning_config=_reasoning_config(),
            platform="merchantbench",
            # Keep Hermes native tools; MerchantBench tools are an extra toolset.
            enabled_toolsets=None,
        )
        self.bridge.bind_agent(self.agent)
        # Ensure the merchantbench toolset stays visible even if defaults change.
        enabled = getattr(self.agent, "enabled_toolsets", None)
        if isinstance(enabled, list) and TOOLSET_NAME not in enabled:
            enabled.append(TOOLSET_NAME)

    def _drive_step(self, obs: dict[str, Any]) -> None:
        brief = obs.get("brief")
        if isinstance(brief, str) and brief.strip():
            system_prompt = brief.strip()
        else:
            system_prompt = self.system_prompt
        self._ensure_agent(system_prompt)

        self.bridge.reset_step_flags()
        history_checkpoint = len(self.history)
        user_message = _obs_user_message(obs)

        result = self.agent.run_conversation(
            user_message=user_message,
            conversation_history=list(self.history),
            system_message=system_prompt,
        )
        messages = result.get("messages") if isinstance(result, dict) else None
        if isinstance(messages, list):
            self.history = messages

        if self.bridge.stale_step:
            # Discard this decision window's messages and wait for a fresh obs.
            self.history = self.history[:history_checkpoint]
            self.agent.clear_interrupt()
            return

        if not self.bridge.step_released:
            self.bridge.force_end_of_step(
                "Hermes wakeup ended without end_of_step "
                f"(max_hops={self.max_hops_per_step})"
            )

        # Clear interrupt so the next wakeup starts cleanly.
        try:
            self.agent.clear_interrupt()
        except Exception:
            logger.debug("clear_interrupt failed", exc_info=True)

    def run(self) -> int:
        self.register()
        if not self.quiet:
            print(
                f"[{FRAMEWORK}] model={self.model} "
                f"max_hops={self.max_hops_per_step} max_steps={self.max_steps}",
                file=sys.stderr,
                flush=True,
            )

        while True:
            try:
                obs = self.client.observation()
            except requests.HTTPError as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 410:
                    if not self.quiet:
                        print("[done] run finished (HTTP 410)", file=sys.stderr)
                    return 0
                logger.exception("observation failed")
                continue

            step = _tick_step(obs)
            if self.max_steps is not None and step >= int(self.max_steps):
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
                self._drive_step(obs)
            except Exception as exc:
                logger.exception("step crash at t=%s", step)
                self.bridge.force_end_of_step(
                    f"step-crash: {type(exc).__name__}: {exc}"
                )
        return 0


def run_adapter(**kwargs: Any) -> int:
    runtime = MerchantBenchHermesRuntime(**kwargs)
    return runtime.run()
