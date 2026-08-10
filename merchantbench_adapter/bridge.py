"""Bridge state shared by MerchantBench tool handlers and the outer runtime."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from typing import Any, Optional

import requests
from tools.registry import registry, tool_error, tool_result

logger = logging.getLogger(__name__)

TOOLSET_NAME = "merchantbench"
ENV_TOOL_ORIGIN = "merchantbench_env"


class Bridge:
    """Shared mutable state for one MerchantBench Hermes run."""

    def __init__(
        self,
        client: Any,
        *,
        max_hops_per_step: int = 30,
        quiet: bool = True,
    ) -> None:
        self.client = client
        self.max_hops_per_step = max_hops_per_step
        self.quiet = quiet
        self.agent: Any = None
        self.lock = threading.RLock()
        self.step_released = False
        self.stale_step = False
        self.force_release_reason: Optional[str] = None
        self.registered_tools: list[str] = []
        self._usage_lock = threading.Lock()
        self._pending_usage: Optional[dict[str, int]] = None

    def reset_step_flags(self) -> None:
        with self.lock:
            self.step_released = False
            self.stale_step = False
            self.force_release_reason = None

    def bind_agent(self, agent: Any) -> None:
        self.agent = agent

    def interrupt_agent(self, reason: str) -> None:
        agent = self.agent
        if agent is None:
            return
        try:
            agent.interrupt(reason, hard_cancel=True)
        except TypeError:
            # Older / alternate interrupt ABI.
            agent.interrupt(reason)
        except Exception:
            logger.exception("Failed to interrupt Hermes agent after %s", reason)

    def note_usage(self, usage: Optional[dict[str, Any]]) -> None:
        if not usage:
            return
        with self._usage_lock:
            self._pending_usage = dict(usage)

    def pull_usage(self) -> Optional[dict[str, int]]:
        with self._usage_lock:
            usage = self._pending_usage
            self._pending_usage = None
        return usage

    def capture_agent_usage(self) -> Optional[dict[str, int]]:
        """Best-effort token usage snapshot from the live Hermes agent."""
        agent = self.agent
        if agent is None:
            return None
        compressor = getattr(agent, "context_compressor", None) or getattr(
            agent, "context_engine", None
        )
        prompt = int(getattr(compressor, "last_prompt_tokens", 0) or 0)
        completion = int(getattr(compressor, "last_completion_tokens", 0) or 0)
        if prompt <= 0 and completion <= 0:
            prompt = int(getattr(agent, "session_prompt_tokens", 0) or 0)
            completion = int(getattr(agent, "session_completion_tokens", 0) or 0)
        if prompt <= 0 and completion <= 0:
            return None
        return {
            "input": max(0, prompt),
            "output": max(0, completion),
            "cache_read": 0,
            "cache_write": 0,
            "reasoning": 0,
            "total": max(0, prompt + completion),
        }

    def call_environment_tool(
        self,
        tool_name: str,
        params: Optional[dict[str, Any]] = None,
        *,
        tool_call_id: Optional[str] = None,
    ) -> str:
        """Execute one MerchantBench tool via ``/act`` and return content."""
        # * Serialize all env /act calls — Hermes may dispatch tools concurrently.
        with self.lock:
            return self._call_environment_tool_locked(
                tool_name,
                params,
                tool_call_id=tool_call_id,
            )

    def _call_environment_tool_locked(
        self,
        tool_name: str,
        params: Optional[dict[str, Any]] = None,
        *,
        tool_call_id: Optional[str] = None,
    ) -> str:
        if self.step_released and tool_name != "end_of_step":
            return tool_error(
                "Decision window already released via end_of_step; "
                "wait for the next observation."
            )

        call_id = tool_call_id or f"mb_{tool_name}_{uuid.uuid4().hex[:10]}"
        arguments = json.dumps(params or {}, ensure_ascii=False)
        assistant_message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": arguments,
                    },
                    "tool_origin": ENV_TOOL_ORIGIN,
                }
            ],
        }
        token_usage = self.pull_usage() or self.capture_agent_usage()
        try:
            response = self.client.act(
                assistant_message=assistant_message,
                token_usage=token_usage,
            )
        except requests.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 425:
                self.stale_step = True
                self.interrupt_agent("stale_step")
                return tool_error(
                    "stale_step: simulation advanced; discard this decision "
                    "window and re-observe.",
                    error_type="stale_step",
                )
            logger.exception("MerchantBench /act failed for %s", tool_name)
            return tool_error(f"/act failed for {tool_name}: {exc}")

        content = _extract_tool_content(response, call_id, tool_name)
        released = bool(
            response.get("hook_released")
            or response.get("step_done")
            or tool_name == "end_of_step"
        )
        if released:
            self.step_released = True
            self.interrupt_agent("end_of_step")
        if isinstance(content, (dict, list)):
            return tool_result(content)
        if content is None:
            return tool_result(ok=True, tool=tool_name, response=response)
        return str(content)

    def force_end_of_step(self, reason: str) -> None:
        """Release the hook when Hermes exits without calling end_of_step."""
        with self.lock:
            if self.step_released or self.stale_step:
                return
            self.force_release_reason = reason
        if not self.quiet:
            logger.warning("Forcing end_of_step: %s", reason)
        try:
            self.call_environment_tool(
                "end_of_step",
                {},
                tool_call_id=f"mb_force_eos_{uuid.uuid4().hex[:8]}",
            )
        except Exception:
            logger.exception("force_end_of_step failed (%s)", reason)


_BRIDGE: Optional[Bridge] = None
_BRIDGE_LOCK = threading.Lock()


def set_bridge(bridge: Bridge) -> Bridge:
    global _BRIDGE
    with _BRIDGE_LOCK:
        _BRIDGE = bridge
    return bridge


def get_bridge() -> Bridge:
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            raise RuntimeError("MerchantBench bridge is not initialized")
        return _BRIDGE


def _extract_tool_content(
    response: dict[str, Any],
    call_id: str,
    tool_name: str,
) -> Any:
    results = response.get("tool_results") or []
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("tool_call_id") == call_id or item.get("name") == tool_name:
            return item.get("content")
    if len(results) == 1 and isinstance(results[0], dict):
        return results[0].get("content")
    return response


def openai_schema_to_hermes(schema: dict[str, Any]) -> dict[str, Any]:
    """Convert OpenAI tools[] entry to Hermes registry schema."""
    if "function" in schema and isinstance(schema["function"], dict):
        function = schema["function"]
        return {
            "name": function.get("name"),
            "description": function.get("description") or "",
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }
    return {
        "name": schema.get("name"),
        "description": schema.get("description") or "",
        "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
    }


def register_merchantbench_tools(bridge: Bridge) -> list[str]:
    """Register all env tool schemas from ``client.tools()`` into Hermes."""
    from toolsets import create_custom_toolset

    names: list[str] = []
    for openai_schema in bridge.client.tools():
        hermes_schema = openai_schema_to_hermes(openai_schema)
        name = hermes_schema.get("name")
        if not name:
            continue
        handler = _make_handler(name)
        registry.register(
            name=name,
            toolset=TOOLSET_NAME,
            schema=hermes_schema,
            handler=handler,
            description=hermes_schema.get("description") or "",
            emoji="🏪",
            override=True,
        )
        names.append(name)

    create_custom_toolset(
        name=TOOLSET_NAME,
        description="MerchantBench seller-side environment tools",
        tools=list(names),
    )
    bridge.registered_tools = names
    return names


def _make_handler(tool_name: str):
    def handler(args: Optional[dict] = None, **kwargs: Any) -> str:
        bridge = get_bridge()
        return bridge.call_environment_tool(
            tool_name,
            args or {},
            tool_call_id=kwargs.get("tool_call_id"),
        )

    handler.__name__ = f"merchantbench_{tool_name}"
    handler.__qualname__ = handler.__name__
    return handler
