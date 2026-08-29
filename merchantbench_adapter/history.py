"""Sanitize MerchantBench conversation history before it is replayed to the LLM."""

from __future__ import annotations

from typing import Any

MERCHANTBENCH_TOOL_PREFIX = "merchantbench__"


def _merchantbench_env_tool_name(name: Any) -> str:
    """Return the raw env tool name, stripping a legacy prefixed alias."""
    text = str(name or "")
    if text.startswith(MERCHANTBENCH_TOOL_PREFIX):
        return text[len(MERCHANTBENCH_TOOL_PREFIX) :]
    return text


def _sanitize_merchantbench_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove MerchantBench hook-control messages from the next LLM prompt.

    ``end_of_step`` is a transport-level acknowledgement that must remain in
    the MerchantBench trace, but replaying it to the model can trigger
    provider loop detection. Orphaned tool results left by compaction are
    also dropped so the retained OpenAI message sequence stays valid.
    """
    eos_call_ids: set[str] = set()
    sanitized: list[dict[str, Any]] = []

    for raw in history:
        if not isinstance(raw, dict):
            continue
        msg = raw
        if msg.get("role") != "assistant":
            sanitized.append(msg)
            continue

        tool_calls = msg.get("tool_calls")
        if not isinstance(tool_calls, list):
            sanitized.append(msg)
            continue

        kept_calls: list[dict[str, Any]] = []
        removed_eos = False
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            if _merchantbench_env_tool_name(fn.get("name")) == "end_of_step":
                removed_eos = True
                call_id = call.get("id")
                if call_id:
                    eos_call_ids.add(str(call_id))
                continue
            kept_calls.append(call)

        if not removed_eos:
            sanitized.append(msg)
            continue
        msg = dict(raw)
        if kept_calls:
            msg["tool_calls"] = kept_calls
            sanitized.append(msg)
            continue

        msg.pop("tool_calls", None)
        msg.pop("tool_origin", None)
        content = msg.get("content")
        drop_with_observation = False
        if isinstance(content, str):
            for marker in ("\n\n[fallback]", "\n\n[llm-error]"):
                marker_idx = content.find(marker)
                if marker_idx >= 0:
                    content = content[:marker_idx].rstrip()
            if content.startswith(("[fallback]", "[llm-error]")):
                content = ""
            msg["content"] = content
        if not content and not msg.get("reasoning_content"):
            # * A synthetic/empty acknowledgement contains no model decision.
            #   Drop its observation too so the next full observation does not
            #   create adjacent user roles.
            drop_with_observation = True
            msg["_merchantbench_drop_with_observation"] = True
        if content or msg.get("reasoning_content"):
            sanitized.append(msg)
        elif drop_with_observation:
            sanitized.append(msg)

    valid_tool_call_ids = {
        str(call.get("id"))
        for msg in sanitized
        if msg.get("role") == "assistant"
        for call in (msg.get("tool_calls") or [])
        if isinstance(call, dict) and call.get("id")
    }
    filtered = [
        msg
        for msg in sanitized
        if msg.get("role") != "tool"
        or (
            str(msg.get("tool_call_id") or "") not in eos_call_ids
            and str(msg.get("tool_call_id") or "") in valid_tool_call_ids
        )
    ]
    repaired: list[dict[str, Any]] = []
    for msg in filtered:
        if msg.pop("_merchantbench_drop_with_observation", False):
            if repaired and repaired[-1].get("role") == "user":
                repaired.pop()
            continue
        repaired.append(msg)
    return repaired


def _sanitize_merchantbench_agent_history(
    agent: Any,
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Sanitize provider history and reconcile SessionDB identity tracking.

    SessionDB cursor attributes are updated only when present on ``agent``.
    """
    sanitized = _sanitize_merchantbench_history(history)
    flushed_ids = getattr(agent, "_flushed_db_message_ids", None)
    if isinstance(flushed_ids, set):
        flushed_ids.intersection_update(
            id(msg) for msg in sanitized if isinstance(msg, dict)
        )
    flush_cursor = getattr(agent, "_last_flushed_db_idx", None)
    if isinstance(flush_cursor, int):
        agent._last_flushed_db_idx = min(flush_cursor, len(sanitized))
    return sanitized
