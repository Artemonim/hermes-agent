"""Bridge state shared by MerchantBench tool handlers and the outer runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from typing import Any, Optional

import requests
from tools.registry import registry, tool_error, tool_result

from merchantbench_adapter.history import (
    MERCHANTBENCH_TOOL_PREFIX,
    _merchantbench_env_tool_name,
)

logger = logging.getLogger(__name__)

TOOLSET_NAME = "merchantbench"
ENV_TOOL_ORIGIN = "merchantbench_env"
HERMES_TOOL_ORIGIN = "hermes_native"

_TOKEN_USAGE_KEYS = (
    "input",
    "output",
    "cache_read",
    "cache_write",
    "reasoning",
    "total",
)


def _usage_snapshot(agent: Any) -> dict[str, int]:
    """Return the agent's cumulative session token counters as canonical buckets."""
    return {
        "input": int(getattr(agent, "session_input_tokens", 0) or 0),
        "output": int(getattr(agent, "session_output_tokens", 0) or 0),
        "cache_read": int(getattr(agent, "session_cache_read_tokens", 0) or 0),
        "cache_write": int(getattr(agent, "session_cache_write_tokens", 0) or 0),
        "reasoning": int(getattr(agent, "session_reasoning_tokens", 0) or 0),
        "total": int(getattr(agent, "session_total_tokens", 0) or 0),
    }


def _usage_delta(before: dict[str, int], agent: Any) -> Optional[dict[str, int]]:
    """Return non-negative per-bucket growth since ``before``, or None if none."""
    after = _usage_snapshot(agent)
    delta = {
        key: max(0, after.get(key, 0) - int(before.get(key, 0) or 0))
        for key in _TOKEN_USAGE_KEYS
    }
    return delta if any(delta.values()) else None


def _canonical_token_usage(
    usage: Optional[dict[str, int]],
) -> Optional[dict[str, int]]:
    """Clamp usage to the env's six canonical buckets.

    When ``total`` is omitted, it is filled as input + output + cache_read +
    cache_write (reasoning is a split of output, not an extra addend).
    """
    if not usage:
        return None
    normalized = {
        key: max(0, int(usage.get(key, 0) or 0))
        for key in _TOKEN_USAGE_KEYS
    }
    if "total" not in usage:
        normalized["total"] = sum(
            normalized[key]
            for key in ("input", "output", "cache_read", "cache_write")
        )
    return normalized if any(normalized.values()) else None


def _token_usage(
    assistant_message: Any,
    *,
    provider: Optional[str] = None,
    api_mode: Optional[str] = None,
) -> Optional[dict[str, int]]:
    """Normalize one assistant message's raw usage into canonical env buckets.

    Uses ``agent.usage_pricing.normalize_usage`` when available (lazy import).
    """
    usage = getattr(assistant_message, "usage", None)
    if usage is None and isinstance(assistant_message, dict):
        usage = assistant_message.get("usage")
    if usage is None:
        return None
    try:
        from agent.usage_pricing import normalize_usage

        normalized = normalize_usage(usage, provider=provider, api_mode=api_mode)
        return {
            "input": int(normalized.input_tokens or 0),
            "output": int(normalized.output_tokens or 0),
            "cache_read": int(normalized.cache_read_tokens or 0),
            "cache_write": int(normalized.cache_write_tokens or 0),
            "reasoning": int(normalized.reasoning_tokens or 0),
            "total": int(normalized.total_tokens or 0),
        }
    except Exception:
        if isinstance(usage, dict):
            prompt = int(usage.get("prompt_tokens", usage.get("input", 0)) or 0)
            completion = int(
                usage.get("completion_tokens", usage.get("output", 0)) or 0
            )
            cached = int(usage.get("cached_tokens", usage.get("cached", 0)) or 0)
        else:
            prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion = int(getattr(usage, "completion_tokens", 0) or 0)
            cached = int(getattr(usage, "cached_tokens", 0) or 0)
        return {
            "input": max(0, prompt - cached),
            "output": completion,
            "cache_read": cached,
            "cache_write": 0,
            "reasoning": 0,
            "total": prompt + completion,
        }


def _raw_prompt_tokens(assistant_message: Any) -> int:
    """Return prompt_tokens from one assistant message's raw usage, or 0."""
    usage = getattr(assistant_message, "usage", None)
    if usage is None and isinstance(assistant_message, dict):
        usage = assistant_message.get("usage")
    if usage is None:
        return 0
    try:
        if isinstance(usage, dict):
            return int(usage.get("prompt_tokens") or 0)
        return int(getattr(usage, "prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _compressor_count(agent: Any) -> int:
    """Return the context compressor's completed-compression counter."""
    if agent is None:
        return 0
    compressor = getattr(agent, "context_compressor", None)
    try:
        return int(getattr(compressor, "compression_count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _compressor_prompt_tokens(agent: Any) -> int:
    """Return the compressor's last observed prompt token count."""
    if agent is None:
        return 0
    compressor = getattr(agent, "context_compressor", None)
    try:
        return int(getattr(compressor, "last_prompt_tokens", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _message_signature(message: dict[str, Any]) -> str:
    """Return a stable identity key for one conversation message."""
    return json.dumps(
        message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def _is_merchantbench_tool_name(
    name: str,
    merchantbench_tool_names: Optional[set[str]] = None,
) -> bool:
    """Return True when ``name`` is a MerchantBench env tool (incl. aliases)."""
    text = str(name or "")
    env_name = _merchantbench_env_tool_name(text)
    return (
        env_name == "end_of_step"
        or text.startswith(MERCHANTBENCH_TOOL_PREFIX)
        or text in (merchantbench_tool_names or set())
        or env_name in (merchantbench_tool_names or set())
    )


def _reorder_end_of_step_last(
    tool_calls: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return ``tool_calls`` with env ``end_of_step`` moved last."""
    normal_calls = [
        tc
        for tc in tool_calls
        if _merchantbench_env_tool_name((tc.get("function") or {}).get("name"))
        != "end_of_step"
    ]
    end_calls = [
        tc
        for tc in tool_calls
        if _merchantbench_env_tool_name((tc.get("function") or {}).get("name"))
        == "end_of_step"
    ]
    if not end_calls:
        return tool_calls
    return [*normal_calls, end_calls[-1]]


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
        # * Last session snapshot committed to a /act body (process-lifetime).
        self._last_posted_usage_snapshot: Optional[dict[str, int]] = None
        self._usage_session_id = uuid.uuid4().hex
        self._reported_summary_record_count = 0
        self._summary_usage_steps: dict[int, Optional[int]] = {}
        self._review_usage_lock = threading.Lock()
        self._review_usage_batches: list[dict[str, Any]] = []
        # * Native Hermes tools captured once so env-tool refresh cannot drop them.
        self._native_tool_names: Optional[set[str]] = None
        self._native_tools: Optional[list[dict[str, Any]]] = None
        # * Cursor into the live conversation history for /act message batches.
        self._trace_cursor = 0
        self._observation_user_text: Optional[str] = None
        self._bound_history: Optional[list[dict[str, Any]]] = None
        self._reported_compression_count = 0
        self._seen_compression_count = 0
        self._posted_signatures: dict[str, int] = {}
        self._posted_env_call_ids: set[str] = set()
        self._posted_env_call_ids_at_step_start: set[str] = set()
        self._cached_env_results: dict[str, tuple[str, Any]] = {}

    def reset_step_flags(self) -> None:
        with self.lock:
            self.step_released = False
            self.stale_step = False
            self.force_release_reason = None

    def bind_agent(self, agent: Any) -> None:
        self.agent = agent
        try:
            from merchantbench_adapter.usage_capture import (
                install_auxiliary_usage_capture,
            )

            install_auxiliary_usage_capture(self, agent)
        except Exception:
            logger.warning(
                "MerchantBench aux capture install failed; ledgers stay no-op",
                exc_info=True,
            )
        # * Pre-existing compressor records were billed before this bind.
        self._reported_summary_record_count = len(
            _summary_usage_records_snapshot(agent)
        )
        # * Pre-existing compressions are not reported as compacted on the
        #   first /act after bind.
        count = _compressor_count(agent)
        self._reported_compression_count = count
        self._seen_compression_count = count

    def bind_history(self, history: Optional[list[dict[str, Any]]]) -> None:
        """Pin a fallback history list when ``agent._session_messages`` is absent."""
        self._bound_history = history

    def live_history(self) -> list[dict[str, Any]]:
        """Return the conversation list that reflects in-flight tool results."""
        return self._live_history()

    def begin_step(
        self,
        *,
        observation_text: str,
        history: list[dict[str, Any]],
    ) -> None:
        """Pin the trace cursor at the start of a decision window."""
        self._observation_user_text = observation_text
        self.bind_history(history)
        self._trace_cursor = len(history)
        self._posted_signatures = {}
        self._posted_env_call_ids_at_step_start = set(self._posted_env_call_ids)
        self._cached_env_results = {}

    def note_history_rewritten(
        self,
        history: list[dict[str, Any]],
        *,
        assume_all_posted: bool = False,
    ) -> None:
        """Rebase the trace cursor after sanitize or an external history rewrite.

        Args:
            history: The rewritten conversation list.
            assume_all_posted: When True, every remaining message has already
                been sent to /act (sanitize at a step boundary). The cursor
                jumps to ``len(history)`` so the next step cannot replay them.
        """
        self.bind_history(history)
        if assume_all_posted:
            self._trace_cursor = len(history)
            return
        self._rebase_cursor_by_signature(history)

    def on_stale_discard(self, history: list[dict[str, Any]]) -> None:
        """Reset cursor bookkeeping after a 425 voided the decision window."""
        self.bind_history(history)
        self._trace_cursor = len(history)
        self._posted_signatures = {}
        self._cached_env_results = {}
        self._posted_env_call_ids = set(self._posted_env_call_ids_at_step_start)

    def _live_history(self) -> list[dict[str, Any]]:
        """Prefer the agent's live session list; fall back to the bound copy.

        ``run_conversation`` copies ``conversation_history`` into a local
        ``messages`` list. After turn-start persist, ``agent._session_messages``
        aliases that same object, so tool results appear as they are appended.
        """
        agent = self.agent
        if agent is not None:
            session = getattr(agent, "_session_messages", None)
            if isinstance(session, list):
                return session
        if self._bound_history is not None:
            return self._bound_history
        return []

    def _env_tool_names(self) -> set[str]:
        """Return registered env tool names plus the hook-control sentinel."""
        names = set(self.registered_tools or [])
        names.add("end_of_step")
        return names

    def _maybe_rebase_for_compaction(self) -> None:
        """Rebase the cursor when Hermes compaction rewrites live history."""
        current = _compressor_count(self.agent)
        if current > int(self._seen_compression_count or 0):
            self._seen_compression_count = current
            self._rebase_cursor_by_signature(self._live_history())

    def _rebase_cursor_by_signature(self, history: list[dict[str, Any]]) -> None:
        """Set the cursor at the first unposted assistant/tool message."""
        remaining = dict(self._posted_signatures)
        for index, msg in enumerate(history):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") not in ("assistant", "tool"):
                continue
            sig = _message_signature(msg)
            if remaining.get(sig, 0) > 0:
                remaining[sig] -= 1
                continue
            self._trace_cursor = index
            return
        self._trace_cursor = len(history)

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

    def _take_act_usage_delta(self) -> Optional[dict[str, int]]:
        """Return session-counter growth since the last /act, then commit.

        Per-act delta-since-last-act is used instead of per-assistant-message
        ``_token_usage`` because env tool handlers never receive the LLM
        response object. ``conversation_loop`` updates session counters
        (already normalized, including cache/reasoning) before tools run, so
        the delta of those counters is the correct per-turn payload. The
        snapshot advances even when the delta is None so a later /act cannot
        replay a stale cumulative value. ``force_end_of_step`` shares this
        path and therefore posts remaining step tokens, not a lifetime
        snapshot.
        """
        agent = self.agent
        if agent is None:
            return None
        before = self._last_posted_usage_snapshot
        if before is None:
            before = {key: 0 for key in _TOKEN_USAGE_KEYS}
        delta = _usage_delta(before, agent)
        self._last_posted_usage_snapshot = _usage_snapshot(agent)
        return delta

    def flush_pending_auxiliary_usage(self, *, attempts: int = 3) -> None:
        """Flush compression/review tokens to the env auxiliary ledger."""
        _flush_pending_auxiliary_usage(self, self.client, attempts=attempts)

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
        # * A 425 voided this window; remaining parallel tools must not POST.
        if self.stale_step:
            return tool_error(
                "stale_step: simulation advanced; discard this decision "
                "window and re-observe.",
                error_type="stale_step",
            )
        self._maybe_rebase_for_compaction()
        call_id = self._resolve_tool_call_id(tool_name, tool_call_id)
        cached = self._pop_cached_env_result(tool_name, call_id)
        if cached is not None:
            return self._format_tool_content(cached, tool_name, response=None)
        if self.step_released and tool_name != "end_of_step":
            return tool_error(
                "Decision window already released via end_of_step; "
                "wait for the next observation."
            )

        tagged, raw_included = self._build_act_batch(
            tool_name, params or {}, call_id
        )
        env_assistant = self._primary_env_assistant(tagged)
        # * Flush aux ledger first so a lost /act cannot duplicate compression
        #   tokens. Then claim the per-act session delta (see _take_act_usage_delta).
        _flush_pending_auxiliary_usage(self, self.client, attempts=1)
        # * Snapshot is claimed before POST. 4xx except 425 is a pre-commit
        #   rejection, so restore the snapshot and let the next successful
        #   /act re-claim those tokens. 425 is not rolled back (voided-turn
        #   usage is dropped, matching hermes-agent). 5xx and transport
        #   errors keep the claim: the server may already have recorded.
        pre_claim_snapshot = self._last_posted_usage_snapshot
        token_usage = self._take_act_usage_delta()
        context = self._act_context()
        try:
            response = self.client.act(
                assistant_message=env_assistant,
                token_usage=token_usage,
                messages=tagged,
                context=context,
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
            if isinstance(status, int) and 400 <= status < 500:
                # * Pre-commit rejection (validation before any recording).
                self._last_posted_usage_snapshot = (
                    dict(pre_claim_snapshot)
                    if pre_claim_snapshot is not None
                    else None
                )
            # * 5xx / missing status: keep the claim. Env appends usage to
            #   in-memory turn metadata before persist; a persist failure
            #   surfaces as 500 with usage already recorded. Rollback would
            #   double-count on the next act and can exhaust the deposit.
            logger.exception("MerchantBench /act failed for %s", tool_name)
            return tool_error(f"/act failed for {tool_name}: {exc}")

        self._commit_act_cursor(tagged, raw_included)
        self._cache_sibling_env_results(response, call_id)
        self._mark_context_reported()
        content = _extract_tool_content(response, call_id, tool_name)
        released = bool(
            response.get("hook_released")
            or response.get("step_done")
            or tool_name == "end_of_step"
        )
        if released:
            self.step_released = True
            self.interrupt_agent("end_of_step")
        return self._format_tool_content(content, tool_name, response)

    def _resolve_tool_call_id(
        self,
        tool_name: str,
        tool_call_id: Optional[str],
    ) -> str:
        """Return the live-history tool_call id, or a generated fallback."""
        if tool_call_id:
            return str(tool_call_id)
        env_name = _merchantbench_env_tool_name(tool_name)
        history = self._live_history()
        cursor = min(self._trace_cursor, len(history))
        for msg in reversed(history[cursor:]):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                name = _merchantbench_env_tool_name(
                    (call.get("function") or {}).get("name")
                )
                call_id = str(call.get("id") or "")
                if name != env_name or not call_id:
                    continue
                if call_id in self._posted_env_call_ids:
                    continue
                return call_id
        return f"mb_{env_name}_{uuid.uuid4().hex[:10]}"

    def _pop_cached_env_result(
        self,
        tool_name: str,
        call_id: str,
    ) -> Any:
        """Return a sibling env result from a combined /act, or None."""
        cached = self._cached_env_results
        if call_id and call_id in cached:
            _name, content = cached.pop(call_id)
            return content
        env_name = _merchantbench_env_tool_name(tool_name)
        for cached_id, (name, content) in list(cached.items()):
            if name == env_name or name == tool_name:
                cached.pop(cached_id)
                return content
        return None

    def _cache_sibling_env_results(
        self,
        response: dict[str, Any],
        current_call_id: str,
    ) -> None:
        """Store extra env results from a combined /act for sibling handlers."""
        for item in response.get("tool_results") or []:
            if not isinstance(item, dict):
                continue
            result_id = str(item.get("tool_call_id") or "")
            if not result_id or result_id == str(current_call_id):
                continue
            self._cached_env_results[result_id] = (
                str(item.get("name") or ""),
                item.get("content"),
            )

    def _build_act_batch(
        self,
        tool_name: str,
        params: dict[str, Any],
        call_id: str,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the origin-tagged /act messages batch and raw source rows.

        The batch is ``history[cursor:]`` (assistant/tool only, skipping the
        observation user message and already-posted env tool results) plus a
        constructed env assistant when the current call is not yet in history.
        """
        history = self._live_history()
        cursor = min(max(0, self._trace_cursor), len(history))
        env_names = self._env_tool_names() | {
            tool_name,
            _merchantbench_env_tool_name(tool_name),
        }
        raw_included: list[dict[str, Any]] = []
        tagged: list[dict[str, Any]] = []
        current_found = False
        for msg in history[cursor:]:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            # * Observation (and other user/system) text is recorded by the env.
            if role in ("user", "system"):
                continue
            if role == "tool":
                result_id = str(msg.get("tool_call_id") or "")
                if result_id and (
                    result_id == str(call_id)
                    or result_id in self._posted_env_call_ids
                ):
                    continue
            if role == "assistant" and self._assistant_has_env_call(
                msg, tool_name, call_id
            ):
                current_found = True
            tagged_msg = self._tag_message(msg, env_names)
            if tagged_msg is None:
                continue
            raw_included.append(msg)
            tagged.append(tagged_msg)
        if not current_found:
            tagged.append(self._current_env_assistant(tool_name, params, call_id))
        if not tagged:
            tagged.append(self._current_env_assistant(tool_name, params, call_id))
        return tagged, raw_included

    def _assistant_has_env_call(
        self,
        msg: dict[str, Any],
        tool_name: str,
        call_id: str,
    ) -> bool:
        """Return True when this assistant carries the in-flight env tool call."""
        env_name = _merchantbench_env_tool_name(tool_name)
        for call in msg.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = _merchantbench_env_tool_name(
                (call.get("function") or {}).get("name")
            )
            cid = str(call.get("id") or "")
            if call_id and cid == str(call_id):
                return True
            if name == env_name and cid not in self._posted_env_call_ids:
                return True
        return False

    def _tag_message(
        self,
        message: dict[str, Any],
        env_names: set[str],
    ) -> Optional[dict[str, Any]]:
        """Return an origin-tagged copy of an assistant/tool message, or None."""
        role = message.get("role")
        if role not in ("assistant", "tool"):
            return None
        out = dict(message)
        if role == "assistant":
            tagged_calls: list[dict[str, Any]] = []
            for call in out.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                tagged = self._tag_tool_call(call, env_names)
                call_id = str(tagged.get("id") or "")
                if (
                    tagged.get("tool_origin") == ENV_TOOL_ORIGIN
                    and call_id in self._posted_env_call_ids
                ):
                    continue
                tagged_calls.append(tagged)
            if tagged_calls:
                tagged_calls = _reorder_end_of_step_last(tagged_calls)
                out["tool_calls"] = tagged_calls
                origins = {str(call.get("tool_origin") or "") for call in tagged_calls}
                if origins == {ENV_TOOL_ORIGIN}:
                    out["tool_origin"] = ENV_TOOL_ORIGIN
                elif origins == {HERMES_TOOL_ORIGIN}:
                    out["tool_origin"] = HERMES_TOOL_ORIGIN
                else:
                    out["tool_origin"] = "mixed"
            else:
                out.pop("tool_calls", None)
                out.setdefault("tool_origin", HERMES_TOOL_ORIGIN)
        else:
            name = str(out.get("name") or "")
            if _is_merchantbench_tool_name(name, env_names):
                out["tool_origin"] = ENV_TOOL_ORIGIN
            else:
                out.setdefault("tool_origin", HERMES_TOOL_ORIGIN)
        return out

    def _tag_tool_call(
        self,
        tool_call: dict[str, Any],
        env_names: set[str],
    ) -> dict[str, Any]:
        """Tag one tool_call and rewrite env names to raw env wire names."""
        out = dict(tool_call)
        function = dict(out.get("function") or {})
        hermes_name = str(function.get("name") or "")
        if _is_merchantbench_tool_name(hermes_name, env_names):
            env_name = _merchantbench_env_tool_name(hermes_name)
            function["name"] = env_name
            out["function"] = function
            out["tool_origin"] = ENV_TOOL_ORIGIN
            if hermes_name != env_name:
                out["hermes_tool_name"] = hermes_name
            else:
                out.pop("hermes_tool_name", None)
        else:
            out["function"] = function
            out.setdefault("type", "function")
            out.setdefault("tool_origin", HERMES_TOOL_ORIGIN)
        if not out.get("id"):
            out["id"] = f"call_hermes_{uuid.uuid4().hex[:10]}"
        out.setdefault("type", "function")
        return out

    def _current_env_assistant(
        self,
        tool_name: str,
        params: dict[str, Any],
        call_id: str,
    ) -> dict[str, Any]:
        """Build the env assistant message for the in-flight tool call."""
        env_name = _merchantbench_env_tool_name(tool_name)
        arguments = json.dumps(params or {}, ensure_ascii=False)
        return {
            "role": "assistant",
            "content": "",
            "tool_origin": ENV_TOOL_ORIGIN,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": env_name,
                        "arguments": arguments,
                    },
                    "tool_origin": ENV_TOOL_ORIGIN,
                }
            ],
        }

    def _primary_env_assistant(
        self,
        messages: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return the last env/mixed assistant in ``messages`` for SDK compat."""
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue
            origin = str(msg.get("tool_origin") or "")
            if origin in {ENV_TOOL_ORIGIN, "mixed"}:
                return msg
        return messages[-1]

    def _commit_act_cursor(
        self,
        tagged: list[dict[str, Any]],
        raw_included: list[dict[str, Any]],
    ) -> None:
        """Advance the cursor and record posted identities after a successful /act."""
        history = self._live_history()
        self._trace_cursor = len(history)
        for msg in raw_included:
            if not isinstance(msg, dict) or msg.get("role") not in ("assistant", "tool"):
                continue
            sig = _message_signature(msg)
            self._posted_signatures[sig] = self._posted_signatures.get(sig, 0) + 1
        for msg in tagged:
            if msg.get("role") != "assistant":
                continue
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                if str(call.get("tool_origin") or "") != ENV_TOOL_ORIGIN:
                    continue
                call_id = str(call.get("id") or "")
                if call_id:
                    self._posted_env_call_ids.add(call_id)

    def _act_context(self) -> Optional[dict[str, Any]]:
        """Return ``context=`` payload when tokens or a compaction event exist."""
        prompt_tokens = 0
        for msg in reversed(self._live_history()):
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            prompt_tokens = _raw_prompt_tokens(msg)
            if prompt_tokens > 0:
                break
        if prompt_tokens <= 0:
            prompt_tokens = _compressor_prompt_tokens(self.agent)
        context: dict[str, Any] = {}
        if prompt_tokens > 0:
            context["tokens"] = prompt_tokens
        current_count = _compressor_count(self.agent)
        if current_count > int(self._reported_compression_count or 0):
            context["compacted"] = True
        return context or None

    def _mark_context_reported(self) -> None:
        """Consume the compacted=True one-shot after a successful /act."""
        current_count = _compressor_count(self.agent)
        if current_count > int(self._reported_compression_count or 0):
            self._reported_compression_count = current_count

    def _format_tool_content(
        self,
        content: Any,
        tool_name: str,
        response: Optional[dict[str, Any]],
    ) -> str:
        """Normalize env /act content into the Hermes tool-result string."""
        if isinstance(content, (dict, list)):
            return tool_result(content)
        if content is None:
            if response is None:
                return tool_result(ok=True, tool=tool_name)
            return tool_result(ok=True, tool=tool_name, response=response)
        return str(content)

    def force_end_of_step(self, reason: str) -> None:
        """Release the hook when Hermes exits without calling end_of_step."""
        with self.lock:
            # * Fallback end_of_step on a voided window would itself 425.
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


def _summary_usage_records_snapshot(agent: Any) -> list[dict[str, Any]]:
    """Return compressor summary-usage records, or [] when the API is absent."""
    if agent is None:
        return []
    compressor = getattr(agent, "context_compressor", None)
    snapshotter = getattr(compressor, "summary_usage_records_snapshot", None)
    if not callable(snapshotter):
        return []
    try:
        return [dict(record) for record in (snapshotter() or [])]
    except (AttributeError, TypeError, ValueError):
        return []


def _post_auxiliary_usage(
    client: Any,
    batch: dict[str, Any],
    *,
    attempts: int,
) -> bool:
    """POST one aux-ledger row. Retry with backoff; warn and return False."""
    last_error: Optional[Exception] = None
    max_attempts = max(1, attempts)
    for attempt in range(max_attempts):
        try:
            client.record_usage(
                batch["token_usage"],
                usage_id=batch["usage_id"],
                source=batch["source"],
                step=batch.get("step"),
                model=batch.get("model"),
                provider=batch.get("provider"),
                cost_usd=batch.get("cost_usd"),
                cost_status=batch.get("cost_status"),
                cost_source=batch.get("cost_source"),
            )
            return True
        except Exception as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                time.sleep(2 ** attempt)
    if last_error is not None:
        logger.warning(
            "Could not record auxiliary usage %s: %s",
            batch.get("usage_id"),
            last_error,
        )
    return False


def _flush_pending_summary_usage(
    bridge: Bridge,
    client: Any,
    *,
    attempts: int,
) -> None:
    """Post unreported context-compression records with frozen-step ids."""
    records = _summary_usage_records_snapshot(bridge.agent)
    reported = int(bridge._reported_summary_record_count or 0)
    reported_steps = bridge._summary_usage_steps
    if not isinstance(reported_steps, dict):
        reported_steps = {}
        bridge._summary_usage_steps = reported_steps
    usage_session_id = str(bridge._usage_session_id or "")
    if not usage_session_id:
        usage_session_id = uuid.uuid4().hex
        bridge._usage_session_id = usage_session_id
    while reported < len(records):
        record = records[reported]
        usage = _canonical_token_usage(record.get("token_usage"))
        if usage is None:
            reported += 1
            bridge._reported_summary_record_count = reported
            continue
        usage_step = reported_steps.setdefault(reported, client.latest_env_t())
        digest = hashlib.sha256(
            json.dumps(
                {
                    "session": usage_session_id,
                    "index": reported,
                    "step": usage_step,
                    "record": record,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()[:20]
        batch = {
            **record,
            "usage_id": f"compression-{reported}-{digest}",
            "source": "context_compression",
            "token_usage": usage,
            "step": usage_step,
        }
        if not _post_auxiliary_usage(client, batch, attempts=attempts):
            return
        reported += 1
        bridge._reported_summary_record_count = reported
        reported_steps.pop(reported - 1, None)


def _flush_pending_review_usage(
    bridge: Bridge,
    client: Any,
    *,
    attempts: int,
) -> None:
    """Post queued background-review batches; leave failures for later."""
    lock = bridge._review_usage_lock
    with lock:
        batches = [dict(batch) for batch in bridge._review_usage_batches]
    for batch in batches:
        if not _post_auxiliary_usage(client, batch, attempts=attempts):
            return
        with lock:
            bridge._review_usage_batches = [
                queued
                for queued in bridge._review_usage_batches
                if queued.get("usage_id") != batch.get("usage_id")
            ]


def _flush_pending_auxiliary_usage(
    bridge: Bridge,
    client: Any = None,
    *,
    attempts: int = 3,
) -> None:
    """Persist all unreported auxiliary usage through idempotent entries.

    Never raises: failures are logged and retried on a later flush. Runtime
    loop call sites (HTTP 410, max_steps, max_observations) should pass
    ``attempts=3``; the locked /act path uses ``attempts=1``.
    """
    try:
        target = client if client is not None else bridge.client
        _flush_pending_summary_usage(bridge, target, attempts=attempts)
        _flush_pending_review_usage(bridge, target, attempts=attempts)
    except Exception:
        logger.exception("Auxiliary usage flush failed; will retry later")


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


def _openai_tool_name(tool: Any) -> str:
    """Return the function name from an OpenAI-format tools[] entry."""
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "")
    return str(tool.get("name") or "")


def _exposed_merchantbench_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Return an OpenAI-format env tool tagged for the agent's outbound schema."""
    out = dict(tool)
    function = dict(out.get("function") or {})
    env_name = str(function.get("name") or "")
    function["name"] = env_name
    description = function.get("description") or ""
    function["description"] = f"[MerchantBench env] {description}".strip()
    function["x-tool-origin"] = ENV_TOOL_ORIGIN
    function["x-merchantbench-tool-name"] = env_name
    out["function"] = function
    out["tool_origin"] = ENV_TOOL_ORIGIN
    out["x-merchantbench-tool-name"] = env_name
    if "type" not in out:
        out["type"] = "function"
    return out


def _capture_native_tools(bridge: Bridge, agent: Any) -> None:
    """Snapshot Hermes-native tool names/schemas before mixing in env tools."""
    if bridge._native_tool_names is None:
        bridge._native_tool_names = {
            entry.name
            for entry in registry.get_all_entries()
            if entry.toolset != TOOLSET_NAME
        }
        if agent is not None:
            for tool in getattr(agent, "tools", None) or []:
                name = _openai_tool_name(tool)
                if name:
                    bridge._native_tool_names.add(name)
        previous_env = set(bridge.registered_tools or [])
        bridge._native_tool_names -= previous_env
    if bridge._native_tools is None and agent is not None:
        previous_env = set(bridge.registered_tools or [])
        captured: list[dict[str, Any]] = []
        for tool in getattr(agent, "tools", None) or []:
            if not isinstance(tool, dict):
                continue
            name = _openai_tool_name(tool)
            if not name or name in previous_env:
                continue
            captured.append(tool)
        bridge._native_tools = captured


def _publish_env_tools_to_agent(
    bridge: Bridge,
    agent: Any,
    env_tools: list[dict[str, Any]],
) -> None:
    """Replace the agent's env tools while preserving the native snapshot."""
    if agent is None:
        return
    native_tools = list(bridge._native_tools or [])
    merged = [*native_tools, *env_tools]
    agent.tools = merged
    agent.valid_tool_names = {
        _openai_tool_name(tool) for tool in merged if _openai_tool_name(tool)
    }
    enabled = getattr(agent, "enabled_toolsets", None)
    if isinstance(enabled, list) and TOOLSET_NAME not in enabled:
        enabled.append(TOOLSET_NAME)


def refresh_merchantbench_tools(bridge: Bridge, agent: Any = None) -> list[str]:
    """Re-fetch env tools and update the registry plus the agent's schema.

    Native Hermes tools are preserved. A name collision with a native tool
    raises ``ValueError``. Removed env tools are deregistered so they cannot
    be dispatched after the env dropped them.
    """
    from toolsets import create_custom_toolset

    _capture_native_tools(bridge, agent)
    native_names = set(bridge._native_tool_names or [])

    # SDK ``tools()`` returns the schema cached at client construction
    # until ``refresh_schema()`` re-GETs ``/tools/schema``.
    refresh_schema = getattr(bridge.client, "refresh_schema", None)
    if callable(refresh_schema):
        refresh_schema()
    openai_schemas = list(bridge.client.tools() or [])
    env_tools: list[dict[str, Any]] = []
    names: list[str] = []
    for openai_schema in openai_schemas:
        if not isinstance(openai_schema, dict):
            continue
        exposed = _exposed_merchantbench_tool(openai_schema)
        name = _openai_tool_name(exposed)
        if not name:
            continue
        env_tools.append(exposed)
        names.append(name)

    collisions = set(names) & native_names
    extra_native = {
        _openai_tool_name(tool)
        for tool in (bridge._native_tools or [])
        if _openai_tool_name(tool)
    }
    collisions |= set(names) & extra_native
    if collisions:
        raise ValueError(
            "MerchantBench env tool names collide with Hermes native tools: "
            f"{', '.join(sorted(collisions))}"
        )

    previous = set(bridge.registered_tools or [])
    for removed in previous - set(names):
        if removed in native_names:
            continue
        registry.deregister(removed)

    for openai_schema, name in zip(env_tools, names):
        hermes_schema = openai_schema_to_hermes(openai_schema)
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

    create_custom_toolset(
        name=TOOLSET_NAME,
        description="MerchantBench seller-side environment tools",
        tools=list(names),
    )
    bridge.registered_tools = names
    _publish_env_tools_to_agent(bridge, agent, env_tools)
    return names


def register_merchantbench_tools(bridge: Bridge, agent: Any = None) -> list[str]:
    """Register all env tool schemas from ``client.tools()`` into Hermes."""
    return refresh_merchantbench_tools(bridge, agent)


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
