"""Bounded fast-mode windows (``/fast auto`` and ``/fast cold``).

``agent.service_tier``: ``None`` (normal), ``"priority"`` (static fast), ``"flex"``
(OpenRouter only), ``"auto"`` (every user turn opens a window of
``agent.fast_auto_seconds``) or ``"cold"`` (only a session's first turn, no prior
history, opens it).

Effective wire kwargs are resolved **per request** in
:func:`effective_request_overrides`: session ``/fast`` pin >
``agent.service_tier_overrides`` > global ``agent.service_tier``. Only
per-request params (``service_tier`` / ``speed``) vary, so the prompt cache
survives the boundary. ``extra_body`` is never rewritten here.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

BOUNDED_MODES = frozenset({"auto", "cold"})
DEFAULT_WINDOW_SECONDS = 60


def _agent_route(agent: Any) -> tuple[Any, Any, Any]:
    """``(model, provider, base_url)`` for the current request, Anthropic Messages URL included."""
    base_url = getattr(agent, "base_url", None)
    if getattr(agent, "api_mode", None) == "anthropic_messages":
        base_url = getattr(agent, "_anthropic_base_url", None) or base_url
    return getattr(agent, "model", None), getattr(agent, "provider", None), base_url


def logical_service_tier(agent: Any) -> str | None:
    """Canonical tier for this request: session pin, else config (per-model then global).

    Unpinned agents re-read config so ``/model``, fallback, cron, and delegated
    children pick the overlay for *their* model without per-surface resync.
    A session pin (including explicit ``None`` = normal) wins and is not
    inherited by children — the flag lives on this agent object only.
    """
    from hermes_constants import parse_service_tier, resolve_service_tier_for_model

    if getattr(agent, "_service_tier_session_pinned", False) is True:
        return parse_service_tier(getattr(agent, "service_tier", None))
    agent_cfg: dict = {}
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        raw = cfg.get("agent")
        if isinstance(raw, dict):
            agent_cfg = raw
    return resolve_service_tier_for_model(
        agent_cfg,
        str(getattr(agent, "model", "") or ""),
        fallback=getattr(agent, "service_tier", None),
    )


def begin_turn(agent: Any, conversation_history: Any) -> None:
    """Open (or refuse) the fast window at a user-turn boundary."""
    mode = logical_service_tier(agent)
    agent._fast_until = 0.0
    if mode not in BOUNDED_MODES:
        return
    if mode == "cold" and any(
        isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")
        for m in (conversation_history or ())
    ):
        return
    try:
        window = float(getattr(agent, "fast_auto_seconds", DEFAULT_WINDOW_SECONDS))
    except (TypeError, ValueError):
        window = DEFAULT_WINDOW_SECONDS
    agent._fast_until = time.monotonic() + max(window, 0.0)


def effective_request_overrides(agent: Any) -> dict[str, Any]:
    """``agent.request_overrides`` plus the resolved service-tier wire keys.

    Stale ``service_tier`` / ``speed`` copied from a previous model or a parent
    session are replaced from the logical tier for *this* request. Other keys
    (including ``extra_body``) are copied as-is.
    """
    overrides = dict(getattr(agent, "request_overrides", None) or {})
    overrides.pop("service_tier", None)
    overrides.pop("speed", None)
    mode = logical_service_tier(agent)
    model, provider, base_url = _agent_route(agent)
    if mode in BOUNDED_MODES:
        if time.monotonic() >= getattr(agent, "_fast_until", 0.0):
            return overrides
        from hermes_cli.models import resolve_fast_mode_overrides

        overrides.update(
            resolve_fast_mode_overrides(model, provider=provider, base_url=base_url) or {}
        )
        return overrides
    from hermes_cli.models import resolve_service_tier_overrides

    mapped = resolve_service_tier_overrides(
        model, mode, provider=provider, base_url=base_url,
    )
    if mapped:
        overrides.update(mapped)
    return overrides
