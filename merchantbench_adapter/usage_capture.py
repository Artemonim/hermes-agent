"""Adapter-side interception of compression/review usage for the aux ledger.

This Hermes tree does not keep ``summary_usage_records_snapshot`` on the
compressor, and background-review usage is written only to session_db.
The Bridge flush path already knows how to read those records / the review
queue; this module feeds them by instance- and module-level wrapping from
adapter code without changing core Hermes files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import weakref
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REVIEW_WRAP_LOCK = threading.Lock()
_REVIEW_WRAP_INSTALLED = False


def install_auxiliary_usage_capture(bridge: Any, agent: Any) -> None:
    """Bind compression/review usage interception onto ``agent`` for ``bridge``.

    Never raises: missing or unexpected internals log a warning and leave the
    corresponding ledger a no-op.
    """
    if agent is None:
        return
    try:
        agent._mb_usage_bridge_ref = weakref.ref(bridge)
    except Exception:
        logger.warning(
            "MerchantBench aux capture: could not attach bridge ref; "
            "review ledger is a no-op"
        )
    try:
        _install_compressor_capture(agent)
    except Exception:
        logger.warning(
            "MerchantBench aux capture: compressor wrap failed; "
            "compression ledger is a no-op",
            exc_info=True,
        )
    try:
        _install_review_capture(agent)
    except Exception:
        logger.warning(
            "MerchantBench aux capture: review wrap failed; "
            "review ledger is a no-op",
            exc_info=True,
        )


def _install_compressor_capture(agent: Any) -> None:
    """Install a snapshot API and wrap ``_generate_summary`` when needed."""
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None:
        return
    existing = getattr(compressor, "summary_usage_records_snapshot", None)
    native_snapshot = callable(existing) and not getattr(
        existing, "_mb_aux_installed", False
    )
    if native_snapshot:
        # * Core (or a test fake) already produces records the Bridge can read.
        return
    generate = getattr(compressor, "_generate_summary", None)
    if not callable(generate):
        logger.warning(
            "MerchantBench aux capture: context compressor has no "
            "_generate_summary; compression ledger is a no-op"
        )
        return
    if not hasattr(compressor, "_summary_usage_records"):
        compressor._summary_usage_records = []
    if not hasattr(compressor, "_summary_usage_lock"):
        compressor._summary_usage_lock = threading.Lock()
    if not callable(existing):

        def snapshot() -> list[dict[str, Any]]:
            with compressor._summary_usage_lock:
                return [dict(record) for record in compressor._summary_usage_records]

        snapshot._mb_aux_installed = True
        compressor.summary_usage_records_snapshot = snapshot
    _wrap_summary_call(compressor, "_generate_summary")


def _wrap_summary_call(compressor: Any, method_name: str) -> None:
    """Instance-wrap a compressor summary method so each LLM call is recorded."""
    original = getattr(compressor, method_name, None)
    if not callable(original):
        return
    if getattr(original, "_mb_aux_wrapped", False):
        return

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return _call_with_summary_usage_capture(compressor, original, args, kwargs)

    wrapped._mb_aux_wrapped = True
    setattr(compressor, method_name, wrapped)


def _call_with_summary_usage_capture(
    compressor: Any,
    original: Any,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> Any:
    """Patch ``call_llm`` for the duration of ``original``, then restore it."""
    import agent.context_compressor as compressor_mod

    depth = int(getattr(compressor, "_mb_call_llm_wrap_depth", 0) or 0)
    compressor._mb_call_llm_wrap_depth = depth + 1
    orig_call = None
    if depth == 0:
        orig_call = compressor_mod.call_llm

        def capturing_call_llm(*call_args: Any, **call_kwargs: Any) -> Any:
            response = orig_call(*call_args, **call_kwargs)
            try:
                _append_summary_usage_from_response(compressor, response)
            except Exception:
                logger.warning(
                    "MerchantBench aux capture: failed to record summary usage",
                    exc_info=True,
                )
            return response

        compressor_mod.call_llm = capturing_call_llm
    try:
        return original(*args, **kwargs)
    finally:
        compressor._mb_call_llm_wrap_depth = depth
        if orig_call is not None:
            compressor_mod.call_llm = orig_call


def _append_summary_usage_from_response(compressor: Any, response: Any) -> None:
    """Append one canonical summary-usage record from an auxiliary LLM response."""
    from merchantbench_adapter.bridge import _canonical_token_usage, _token_usage

    # * call_llm already exposes OpenAI-compatible usage; parse as chat_completions
    #   so Anthropic/Codex translated buckets are not zeroed by the native api_mode.
    usage = _canonical_token_usage(
        _token_usage(response, provider="", api_mode="chat_completions")
    )
    if usage is None:
        return
    model = ""
    if isinstance(response, dict):
        model = str(response.get("model") or "")
    else:
        model = str(getattr(response, "model", "") or "")
    if not model:
        model = str(
            getattr(compressor, "summary_model", None)
            or getattr(compressor, "model", None)
            or ""
        )
    same_as_main = model == str(getattr(compressor, "model", "") or "")
    provider = ""
    if same_as_main:
        provider = str(getattr(compressor, "provider", "") or "")
    record: dict[str, Any] = {
        "token_usage": dict(usage),
        "model": model,
        "provider": provider,
        "cost_status": "unknown",
        "cost_source": "none",
        "cost_usd": None,
    }
    try:
        from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

        cost = estimate_usage_cost(
            model,
            CanonicalUsage(
                input_tokens=usage["input"],
                output_tokens=usage["output"],
                cache_read_tokens=usage["cache_read"],
                cache_write_tokens=usage["cache_write"],
                reasoning_tokens=usage["reasoning"],
            ),
            provider=provider or None,
            base_url=getattr(compressor, "base_url", None) if same_as_main else None,
            api_key=getattr(compressor, "api_key", None) if same_as_main else None,
        )
        record["cost_status"] = str(getattr(cost, "status", None) or "unknown")
        record["cost_source"] = str(getattr(cost, "source", None) or "none")
        amount = getattr(cost, "amount_usd", None)
        record["cost_usd"] = None if amount is None else float(amount)
    except Exception:
        pass
    lock = getattr(compressor, "_summary_usage_lock", None)
    records = getattr(compressor, "_summary_usage_records", None)
    if not isinstance(records, list):
        return
    if lock is None:
        records.append(record)
        return
    with lock:
        records.append(record)


def _install_review_capture(agent: Any) -> None:
    """Attach a bridge pointer and wrap the review usage recorder."""
    _ensure_review_recorder_wrap()
    if not getattr(agent, "_mb_usage_bridge_ref", None) and not getattr(
        agent, "_mb_usage_bridge", None
    ):
        logger.warning(
            "MerchantBench aux capture: no bridge pointer on agent; "
            "review ledger is a no-op"
        )


def _ensure_review_recorder_wrap() -> None:
    """Wrap ``_record_review_usage_to_parent`` once (completion-path intercept)."""
    global _REVIEW_WRAP_INSTALLED
    with _REVIEW_WRAP_LOCK:
        if _REVIEW_WRAP_INSTALLED:
            return
        try:
            import agent.background_review as background_review
        except Exception:
            logger.warning(
                "MerchantBench aux capture: cannot import background_review; "
                "review ledger is a no-op",
                exc_info=True,
            )
            _REVIEW_WRAP_INSTALLED = True
            return
        original = getattr(background_review, "_record_review_usage_to_parent", None)
        if not callable(original):
            logger.warning(
                "MerchantBench aux capture: _record_review_usage_to_parent "
                "missing; review ledger is a no-op"
            )
            _REVIEW_WRAP_INSTALLED = True
            return
        if getattr(original, "_mb_aux_wrapped", False):
            _REVIEW_WRAP_INSTALLED = True
            return

        def wrapped(parent_agent: Any, usage: dict[str, Any]) -> Any:
            result = original(parent_agent, usage)
            try:
                _enqueue_review_usage_from_parent(parent_agent, usage)
            except Exception:
                logger.warning(
                    "MerchantBench aux capture: failed to enqueue review usage",
                    exc_info=True,
                )
            return result

        wrapped._mb_aux_wrapped = True
        background_review._record_review_usage_to_parent = wrapped
        _REVIEW_WRAP_INSTALLED = True


def _resolve_bound_bridge(parent_agent: Any) -> Optional[Any]:
    """Return the Bridge attached to ``parent_agent``, if any."""
    ref = getattr(parent_agent, "_mb_usage_bridge_ref", None)
    if callable(ref):
        try:
            return ref()
        except Exception:
            return None
    return getattr(parent_agent, "_mb_usage_bridge", None)


def _enqueue_review_usage_from_parent(parent_agent: Any, usage: dict[str, Any]) -> None:
    """Turn one review-completion usage dict into a queued aux-ledger batch."""
    from merchantbench_adapter.bridge import _canonical_token_usage

    bridge = _resolve_bound_bridge(parent_agent)
    if bridge is None:
        return
    mapped = _review_usage_to_canonical(usage)
    normalized = _canonical_token_usage(mapped)
    if normalized is None:
        return
    client = getattr(bridge, "client", None)
    try:
        trigger_step = client.latest_env_t() if client is not None else None
    except Exception:
        trigger_step = None
    cost_usd = usage.get("cost_usd")
    if cost_usd is None:
        cost_usd = usage.get("estimated_cost_usd")
    if cost_usd is not None:
        try:
            cost_usd = float(cost_usd)
        except (TypeError, ValueError):
            cost_usd = None
    identity = {
        "turn": int(getattr(parent_agent, "_user_turn_count", 0) or 0),
        "request_index": int(
            usage.get("request_index", usage.get("api_calls", 0)) or 0
        ),
        "step": trigger_step,
        "usage": normalized,
        "model": str(usage.get("model") or ""),
        "provider": str(usage.get("provider") or ""),
        "cost_usd": cost_usd,
        "cost_status": str(usage.get("cost_status") or "unknown"),
        "cost_source": str(usage.get("cost_source") or "none"),
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:20]
    batch = {
        "usage_id": (
            f"checkpoint-review-turn-{identity['turn']}-"
            f"call-{identity['request_index']}-{digest}"
        ),
        "source": "checkpoint_review",
        "token_usage": normalized,
        "step": trigger_step,
        "model": identity["model"],
        "provider": identity["provider"],
        "cost_usd": identity["cost_usd"],
        "cost_status": identity["cost_status"],
        "cost_source": identity["cost_source"],
    }
    lock = getattr(bridge, "_review_usage_lock", None)
    batches = getattr(bridge, "_review_usage_batches", None)
    if not isinstance(batches, list):
        return
    if lock is None:
        _append_review_batch(batches, batch)
        return
    with lock:
        _append_review_batch(batches, batch)


def _append_review_batch(batches: list[dict[str, Any]], batch: dict[str, Any]) -> None:
    """Append ``batch`` unless the same usage_id is already queued."""
    usage_id = batch.get("usage_id")
    if any(queued.get("usage_id") == usage_id for queued in batches):
        return
    batches.append(batch)


def _review_usage_to_canonical(usage: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Map review snapshot keys (``input_tokens``) onto canonical bucket names."""
    if not usage:
        return None
    if "input" in usage or "output" in usage:
        return {
            "input": usage.get("input", 0),
            "output": usage.get("output", 0),
            "cache_read": usage.get("cache_read", 0),
            "cache_write": usage.get("cache_write", 0),
            "reasoning": usage.get("reasoning", 0),
            **(
                {"total": usage["total"]}
                if "total" in usage
                else {}
            ),
        }
    mapped = {
        "input": usage.get("input_tokens", 0),
        "output": usage.get("output_tokens", 0),
        "cache_read": usage.get("cache_read_tokens", 0),
        "cache_write": usage.get("cache_write_tokens", 0),
        "reasoning": usage.get("reasoning_tokens", 0),
    }
    if "total" in usage:
        mapped["total"] = usage["total"]
    return mapped
