"""Opt-in sticky OpenRouter/Nous provider pin and failure rotation."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.chat_completion_helpers import (
    _provider_preferences_for_agent,
    handle_max_iterations,
)
from agent.error_classifier import FailoverReason, classify_api_error
from agent.sticky_provider_order import (
    apply_sticky_retry_budget,
    begin_sticky_logical_request,
    bind_sticky_order,
    rotate,
    note_sticky_attempt,
    rotate_sticky_on_classified_error,
    should_fallback_on_transport_failure,
    should_rotate_for_reason,
    sticky_defers_model_fallback,
    sticky_is_live,
    sticky_retry_floor,
)
from hermes_constants import (
    apply_provider_routing_to_agent,
    resolve_provider_routing_for_model,
    resolve_sticky_order_config,
)


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _agent(
    *,
    order=None,
    only=None,
    ignore=None,
    sort=None,
    sticky=None,
    routing=None,
    clock=None,
    provider="openrouter",
    base_url="https://openrouter.ai/api/v1",
    api_mode="chat_completions",
    require_parameters=False,
    data_collection=None,
):
    if order is None:
        order = ["z-ai/fp8", "novita/fp8"]
    agent = SimpleNamespace(
        provider=provider,
        base_url=base_url,
        api_mode=api_mode,
        model="google/gemini-flash",
        providers_order=order,
        providers_allowed=only,
        providers_ignored=ignore,
        provider_sort=sort,
        provider_require_parameters=require_parameters,
        provider_data_collection=data_collection,
    )
    if routing is None:
        routing = {"order": order}
        if only is not None:
            routing["only"] = only
        if sticky is not None:
            routing["sticky_order"] = sticky
    bind_sticky_order(agent, routing, clock=clock)
    return agent


class TestResolveStickyOrderConfig:
    def test_missing_section_is_disabled(self):
        cfg = resolve_sticky_order_config({})
        assert cfg.enabled is False
        assert cfg.ttl_seconds == 600.0

    def test_valid_opt_in(self):
        cfg = resolve_sticky_order_config(
            {"sticky_order": {"enabled": True, "ttl_seconds": 120}},
        )
        assert cfg.enabled is True
        assert cfg.ttl_seconds == 120.0

    def test_invalid_values_warn_and_use_defaults(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="hermes_constants"):
            cfg = resolve_sticky_order_config(
                {"sticky_order": {"enabled": "sometimes", "ttl_seconds": -1}},
            )
        assert cfg.enabled is False
        assert cfg.ttl_seconds == 600.0
        assert "sticky_order" in caplog.text

    def test_non_dict_section_warns_and_disables(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="hermes_constants"):
            cfg = resolve_sticky_order_config({"sticky_order": "yes"})
        assert cfg.enabled is False
        assert cfg.ttl_seconds == 600.0


class TestStickyOrderDefaultOff:
    def test_preferences_unchanged_without_enabled(self):
        agent = _agent(order=["z-ai/fp8", "novita/fp8"])
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8", "novita/fp8"]
        assert "allow_fallbacks" not in prefs

    def test_unconfigured_agent_is_disabled(self):
        agent = SimpleNamespace(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            providers_order=["z-ai/fp8"],
            providers_allowed=None,
            providers_ignored=None,
            provider_sort=None,
            provider_require_parameters=False,
            provider_data_collection=None,
        )
        bind_sticky_order(agent, None)
        assert agent._sticky_provider_order.enabled is False
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert "allow_fallbacks" not in prefs


class TestStickyOrderPin:
    def test_enabled_pins_first_slug_and_disables_fallbacks(self):
        agent = _agent(sticky={"enabled": True})
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["allow_fallbacks"] is False
        assert "only" not in prefs

    def test_only_intersection_narrows_only_key(self):
        agent = _agent(
            order=["z-ai/fp8", "novita/fp8"],
            only=["novita/fp8", "other"],
            sticky={"enabled": True},
        )
        assert agent._sticky_provider_order.pool == ["novita/fp8"]
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["novita/fp8"]
        assert prefs["only"] == ["novita/fp8"]
        assert prefs["allow_fallbacks"] is False

    def test_empty_intersection_disables_sticky(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="run_agent"):
            agent = _agent(
                order=["z-ai/fp8"],
                only=["nope"],
                sticky={"enabled": True},
            )
        assert agent._sticky_provider_order.is_active is False
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["only"] == ["nope"]
        assert "allow_fallbacks" not in prefs
        assert "intersection" in caplog.text

    def test_ignore_and_sort_are_left_alone(self):
        agent = _agent(
            ignore=["deepinfra"],
            sort="throughput",
            sticky={"enabled": True},
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["ignore"] == ["deepinfra"]
        assert prefs["sort"] == "throughput"
        assert prefs["order"] == ["z-ai/fp8"]


class TestStickyOrderRotation:
    def test_timeout_rotates_to_next_slug_and_wraps(self):
        agent = _agent(order=["z-ai/fp8", "novita/fp8", "atlas"], sticky={"enabled": True})
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is True
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["novita/fp8"]

        state = agent._sticky_provider_order
        state.active_index = len(state.pool) - 1
        state.rotations_this_request = 0
        assert rotate(state, FailoverReason.overloaded) is True
        assert state.active_index == 0
        assert _provider_preferences_for_agent(agent)["order"] == ["z-ai/fp8"]

    def test_overloaded_and_server_error_rotate(self):
        agent = _agent(
            order=["z-ai/fp8", "novita/fp8", "atlas"],
            sticky={"enabled": True},
        )
        assert should_rotate_for_reason(FailoverReason.overloaded) is True
        assert should_rotate_for_reason(FailoverReason.server_error) is True
        assert rotate_sticky_on_classified_error(agent, FailoverReason.overloaded) is True
        assert agent._sticky_provider_order.active_index == 1
        assert rotate_sticky_on_classified_error(agent, FailoverReason.server_error) is True
        assert agent._sticky_provider_order.active_index == 2

    def test_rate_limit_and_empty_content_do_not_rotate(self):
        agent = _agent(sticky={"enabled": True})
        assert should_rotate_for_reason(FailoverReason.rate_limit) is False
        assert should_rotate_for_reason("invalid_response") is False
        assert should_rotate_for_reason("empty_content") is False
        assert rotate_sticky_on_classified_error(agent, FailoverReason.rate_limit) is False
        assert rotate_sticky_on_classified_error(agent, "invalid_response") is False
        assert agent._sticky_provider_order.active_index == 0
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]

    def test_rotation_cap_is_pool_minus_one(self):
        agent = _agent(
            order=["a", "b", "c"],
            sticky={"enabled": True},
        )
        state = agent._sticky_provider_order
        assert rotate(state, "timeout") is True
        assert rotate(state, "timeout") is True
        assert state.active_index == 2
        assert state.rotations_this_request == 2
        assert rotate(state, "timeout") is False
        assert state.active_index == 2
        assert state.rotations_this_request == 2

    def test_single_slug_pins_without_rotation(self):
        agent = _agent(order=["z-ai/fp8"], sticky={"enabled": True})
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["allow_fallbacks"] is False
        assert rotate(agent._sticky_provider_order, "timeout") is False
        assert agent._sticky_provider_order.active_index == 0


class TestStickyOrderTtl:
    def test_idle_beyond_ttl_returns_to_index_zero(self):
        clock = _Clock(1000.0)
        agent = _agent(sticky={"enabled": True, "ttl_seconds": 600}, clock=clock)
        begin_sticky_logical_request(agent)
        _provider_preferences_for_agent(agent)
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_index == 1
        clock.t = 1000.0 + 601.0
        begin_sticky_logical_request(agent)
        prefs = _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.active_index == 0
        assert prefs["order"] == ["z-ai/fp8"]

    def test_pause_within_ttl_keeps_index(self):
        clock = _Clock(1000.0)
        agent = _agent(sticky={"enabled": True, "ttl_seconds": 600}, clock=clock)
        begin_sticky_logical_request(agent)
        _provider_preferences_for_agent(agent)
        rotate(agent._sticky_provider_order, "timeout")
        clock.t = 1000.0 + 60.0
        begin_sticky_logical_request(agent)
        prefs = _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.active_index == 1
        assert prefs["order"] == ["novita/fp8"]

    def test_prefs_rebuild_after_idle_does_not_reset(self):
        """Retry-loop prefs rebuilds must not treat backoff as idle."""
        clock = _Clock(1000.0)
        agent = _agent(
            order=["a", "b", "c"],
            sticky={"enabled": True, "ttl_seconds": 1},
            clock=clock,
        )
        begin_sticky_logical_request(agent)
        assert _provider_preferences_for_agent(agent)["order"] == ["a"]
        rotate_sticky_on_classified_error(agent, FailoverReason.timeout)
        clock.t = 1000.0 + 10.0
        prefs = _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.active_index == 1
        assert prefs["order"] == ["b"]

    def test_idle_between_logical_requests_returns_to_pool_zero(self):
        clock = _Clock(1000.0)
        agent = _agent(
            order=["a", "b", "c"],
            sticky={"enabled": True, "ttl_seconds": 1},
            clock=clock,
        )
        begin_sticky_logical_request(agent)
        _provider_preferences_for_agent(agent)
        rotate_sticky_on_classified_error(agent, FailoverReason.timeout)
        assert _provider_preferences_for_agent(agent)["order"] == ["b"]
        clock.t = 1000.0 + 10.0
        begin_sticky_logical_request(agent)
        prefs = _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.active_index == 0
        assert prefs["order"] == ["a"]

    def test_prefs_rebuild_ticks_only_when_live(self):
        clock = _Clock(1000.0)
        agent = _agent(
            order=["a", "b"],
            sticky={"enabled": True, "ttl_seconds": 10},
            clock=clock,
        )
        rotate(agent._sticky_provider_order, "timeout")
        begin_sticky_logical_request(agent)
        _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.last_attempt_at == 1000.0
        assert agent._sticky_provider_order.active_index == 1

        clock.t = 1005.0
        agent.api_mode = "anthropic_messages"
        _provider_preferences_for_agent(agent)
        note_sticky_attempt(agent)
        assert agent._sticky_provider_order.last_attempt_at == 1000.0

        agent.api_mode = "chat_completions"
        _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.last_attempt_at == 1005.0


class TestStickyOrderRebind:
    def test_new_order_resets_index(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        rotate(agent._sticky_provider_order, "timeout")
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_index == 2
        agent.providers_order = ["x", "y"]
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["x", "y"]
        assert agent._sticky_provider_order.active_index == 0

    def test_shortened_order_keeps_active_slug_when_still_in_pool(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_slug == "b"
        agent.providers_order = ["a", "b"]
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["a", "b"]
        assert agent._sticky_provider_order.active_index == 1
        assert agent._sticky_provider_order.active_slug == "b"

    def test_shortened_order_resets_when_active_slug_dropped(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        rotate(agent._sticky_provider_order, "timeout")
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_slug == "c"
        agent.providers_order = ["a", "b"]
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["a", "b"]
        assert agent._sticky_provider_order.active_index == 0
        assert agent._sticky_provider_order.active_slug == "a"

    def test_only_change_keeps_active_slug_when_still_in_pool(self):
        agent = _agent(
            order=["a", "b", "c"],
            only=["a", "b", "c"],
            sticky={"enabled": True},
        )
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_slug == "b"
        agent.providers_allowed = ["a", "b"]
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["a", "b"]
        assert agent._sticky_provider_order.active_index == 1
        assert agent._sticky_provider_order.active_slug == "b"

    def test_only_change_resets_when_active_slug_dropped(self):
        agent = _agent(
            order=["a", "b", "c"],
            only=["a", "b", "c"],
            sticky={"enabled": True},
        )
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_slug == "b"
        agent.providers_allowed = ["a", "c"]
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["a", "c"]
        assert agent._sticky_provider_order.active_index == 0
        assert agent._sticky_provider_order.active_slug == "a"

    def test_same_order_rebind_keeps_index(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_index == 1
        bind_sticky_order(agent, {"sticky_order": {"enabled": True}})
        assert agent._sticky_provider_order.pool == ["a", "b", "c"]
        assert agent._sticky_provider_order.active_index == 1

    def test_init_then_apply_double_bind_keeps_index(self):
        """agent_init bind + apply() resync must not reset the pin."""
        order = ["z-ai/fp8", "novita/fp8"]
        routing = {"order": order, "sticky_order": {"enabled": True}}
        agent = SimpleNamespace(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="google/gemini-flash",
            providers_order=order,
            providers_allowed=None,
            providers_ignored=None,
            provider_sort=None,
            provider_require_parameters=False,
            provider_data_collection=None,
            _provider_routing_config=routing,
        )
        bind_sticky_order(
            agent,
            resolve_provider_routing_for_model(routing, agent.model),
        )
        rotate(agent._sticky_provider_order, "timeout")
        assert agent._sticky_provider_order.active_index == 1
        apply_provider_routing_to_agent(agent, routing, agent.model)
        assert agent._sticky_provider_order.pool == order
        assert agent._sticky_provider_order.active_index == 1
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["novita/fp8"]


class TestStickyOrderConstructorBind:
    def test_config_bind_without_apply_pins_constructor_order(self):
        """Cron / subagent / batch pass order on the constructor, not apply()."""
        routing = {
            "order": ["z-ai/fp8", "novita/fp8"],
            "sticky_order": {"enabled": True},
        }
        agent = SimpleNamespace(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            model="google/gemini-flash",
            providers_order=routing["order"],
            providers_allowed=None,
            providers_ignored=None,
            provider_sort=None,
            provider_require_parameters=False,
            provider_data_collection=None,
            _provider_routing_config=routing,
        )
        bind_sticky_order(
            agent,
            resolve_provider_routing_for_model(
                agent._provider_routing_config, agent.model,
            ),
        )
        assert sticky_is_live(agent) is True
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["allow_fallbacks"] is False


class TestStickyOrderRouteGate:
    def test_non_openrouter_preferences_are_not_rewritten(self):
        agent = _agent(
            provider="openai",
            base_url="https://api.openai.com/v1",
            sticky={"enabled": True},
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8", "novita/fp8"]
        assert "allow_fallbacks" not in prefs
        assert sticky_is_live(agent) is False
        assert sticky_retry_floor(agent) is None
        assert sticky_defers_model_fallback(agent) is False
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False

    def test_anthropic_messages_is_full_noop(self):
        agent = _agent(
            provider="openrouter",
            api_mode="anthropic_messages",
            sticky={"enabled": True},
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8", "novita/fp8"]
        assert "allow_fallbacks" not in prefs
        assert sticky_is_live(agent) is False
        assert sticky_retry_floor(agent) is None
        assert sticky_defers_model_fallback(agent) is False
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False
        assert agent._sticky_provider_order.active_index == 0
        assert apply_sticky_retry_budget(agent, 2) == 2

    def test_codex_responses_is_full_noop(self):
        agent = _agent(
            provider="openrouter",
            api_mode="codex_responses",
            sticky={"enabled": True},
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8", "novita/fp8"]
        assert "allow_fallbacks" not in prefs
        assert sticky_is_live(agent) is False
        assert sticky_retry_floor(agent) is None
        assert sticky_defers_model_fallback(agent) is False
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False
        assert agent._sticky_provider_order.active_index == 0
        assert apply_sticky_retry_budget(agent, 2) == 2

    def test_nous_anthropic_messages_is_full_noop(self):
        agent = _agent(
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1",
            api_mode="anthropic_messages",
            sticky={"enabled": True},
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8", "novita/fp8"]
        assert "allow_fallbacks" not in prefs
        assert sticky_is_live(agent) is False
        assert sticky_retry_floor(agent) is None

    def test_nous_chat_completions_still_pins(self):
        agent = _agent(
            provider="nous",
            base_url="https://inference-api.nousresearch.com/v1",
            api_mode="chat_completions",
            sticky={"enabled": True},
        )
        assert sticky_is_live(agent) is True
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["allow_fallbacks"] is False

    def test_apply_provider_routing_binds_resolved_pool(self):
        agent = SimpleNamespace(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_sort=None,
            provider_require_parameters=False,
            provider_data_collection=None,
        )
        apply_provider_routing_to_agent(
            agent,
            {
                "order": ["z-ai/fp8", "novita/fp8"],
                "sticky_order": {"enabled": True},
            },
        )
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["z-ai/fp8"]
        assert prefs["allow_fallbacks"] is False

    def test_per_model_overlay_rebinds_pool_and_resets_index(self):
        agent = SimpleNamespace(
            provider="openrouter",
            base_url="https://openrouter.ai/api/v1",
            api_mode="chat_completions",
            provider_sort=None,
            provider_require_parameters=False,
            provider_data_collection=None,
        )
        routing = {
            "order": ["flat-a", "flat-b"],
            "sticky_order": {"enabled": True},
            "models": {
                "model-a": {"order": ["overlay-x", "overlay-y"]},
                "model-b": {"order": ["other-1", "other-2", "other-3"]},
            },
        }
        apply_provider_routing_to_agent(agent, routing, "model-a")
        assert agent.providers_order == ["overlay-x", "overlay-y"]
        assert agent._sticky_provider_order.pool == ["overlay-x", "overlay-y"]
        assert agent._sticky_provider_order.active_index == 0
        assert rotate(agent._sticky_provider_order, "timeout") is True
        assert agent._sticky_provider_order.active_index == 1

        apply_provider_routing_to_agent(agent, routing, "model-b")
        assert agent.providers_order == ["other-1", "other-2", "other-3"]
        assert agent._sticky_provider_order.pool == ["other-1", "other-2", "other-3"]
        assert agent._sticky_provider_order.active_index == 0
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["other-1"]
        assert prefs["allow_fallbacks"] is False


class TestStickyOrderLoopWiring:
    def test_classify_then_rotate_rebuilds_prefs_with_next_slug(self):
        agent = _agent(sticky={"enabled": True})
        classified = classify_api_error(
            TimeoutError("request timed out"),
            provider="openrouter",
            model=agent.model,
        )
        assert classified.reason == FailoverReason.timeout
        assert rotate_sticky_on_classified_error(agent, classified.reason) is True
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["novita/fp8"]
        assert prefs["allow_fallbacks"] is False

    def test_defer_until_pool_exhausted_then_allows_fallback(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        begin_sticky_logical_request(agent)
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is True
        assert agent._sticky_provider_order.attempts_this_request == 1
        assert agent._sticky_provider_order.rotations_this_request == 1
        assert sticky_defers_model_fallback(agent) is True
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=1,
        ) is False

        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is True
        assert agent._sticky_provider_order.attempts_this_request == 2
        assert agent._sticky_provider_order.rotations_this_request == 2
        assert sticky_defers_model_fallback(agent) is True
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=2,
        ) is False

        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False
        assert agent._sticky_provider_order.attempts_this_request == 3
        assert agent._sticky_provider_order.rotations_this_request == 2
        assert agent._sticky_provider_order.active_index == 2
        assert sticky_defers_model_fallback(agent) is False
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=3,
        ) is True

    def test_begin_logical_request_resets_rotation_budget(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        rotate_sticky_on_classified_error(agent, FailoverReason.timeout)
        rotate_sticky_on_classified_error(agent, FailoverReason.timeout)
        rotate_sticky_on_classified_error(agent, FailoverReason.timeout)
        assert agent._sticky_provider_order.rotations_this_request == 2
        assert agent._sticky_provider_order.attempts_this_request == 3
        assert sticky_defers_model_fallback(agent) is False
        begin_sticky_logical_request(agent)
        assert agent._sticky_provider_order.rotations_this_request == 0
        assert agent._sticky_provider_order.attempts_this_request == 0
        assert sticky_defers_model_fallback(agent) is True
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=2,
        ) is False

    def test_retry_budget_raises_to_pool_size_when_live(self):
        agent = _agent(order=["a", "b", "c"], sticky={"enabled": True})
        agent._sticky_provider_order.rotations_this_request = 2
        agent._sticky_provider_order.attempts_this_request = 3
        assert sticky_retry_floor(agent) == 3
        assert apply_sticky_retry_budget(agent, 1) == 3
        assert agent._sticky_provider_order.rotations_this_request == 0
        assert agent._sticky_provider_order.attempts_this_request == 0
        assert apply_sticky_retry_budget(agent, 8) == 8

    def test_disabled_transport_fallback_matches_classic_gate(self):
        agent = _agent(order=["a", "b", "c"])
        assert sticky_is_live(agent) is False
        assert sticky_defers_model_fallback(agent) is False
        assert apply_sticky_retry_budget(agent, 3) == 3
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=1,
        ) is False
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=2,
        ) is True
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=False, retry_count=2,
        ) is False

    def test_anthropic_messages_uses_classic_retry_and_fallback_gates(self):
        agent = _agent(
            order=["a", "b", "c"],
            api_mode="anthropic_messages",
            sticky={"enabled": True},
        )
        assert apply_sticky_retry_budget(agent, 2) == 2
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=2,
        ) is True
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False

    def test_codex_responses_uses_classic_retry_and_fallback_gates(self):
        agent = _agent(
            order=["a", "b", "c"],
            api_mode="codex_responses",
            sticky={"enabled": True},
        )
        assert apply_sticky_retry_budget(agent, 2) == 2
        assert should_fallback_on_transport_failure(
            agent, is_transport_failure=True, retry_count=2,
        ) is True
        assert rotate_sticky_on_classified_error(agent, FailoverReason.timeout) is False
        assert agent._sticky_provider_order.attempts_this_request == 0


def _conversation_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _sticky_walk_agent(*, sticky, clock=None):
    """Build a quiet OpenRouter agent whose chat path walks a three-slug pool."""
    from run_agent import AIAgent

    primary = "google/gemini-flash"
    fallback_model = "qwen/qwen3.8-27b"
    pool = ["a", "b", "c"]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "agent.context_compressor.get_model_context_length",
            return_value=200_000,
        ),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model=primary,
            providers_order=pool,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[
                {
                    "provider": "openrouter",
                    "model": fallback_model,
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_mode": "chat_completions",
                }
            ],
        )
    agent.client = MagicMock()
    agent.api_mode = "chat_completions"
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    # * Floor must raise 1 → len(pool) so three slug attempts fit.
    agent._api_max_retries = 1
    apply_provider_routing_to_agent(
        agent,
        {"order": pool, "sticky_order": sticky},
        primary,
    )
    if clock is not None:
        agent._sticky_provider_order.clock = clock
    return agent, primary, fallback_model, pool


def _timeout_walk_conversation(agent, fallback_model, captured, *, on_primary_timeout=None):
    """Run one turn that times out on the primary until model fallback."""

    def fake_api_call(api_kwargs):
        extra = api_kwargs.get("extra_body") or {}
        provider_prefs = extra.get("provider") or {}
        captured.append(
            {
                "model": agent.model,
                "order": provider_prefs.get("order"),
                "allow_fallbacks": provider_prefs.get("allow_fallbacks"),
            }
        )
        if not agent._fallback_activated:
            if on_primary_timeout is not None:
                on_primary_timeout()
            raise TimeoutError("request timed out")
        return _conversation_response("recovered via fallback")

    mock_fb = MagicMock()
    mock_fb.base_url = "https://openrouter.ai/api/v1"
    mock_fb.api_key = "fb-key"
    mock_fb._custom_headers = None
    mock_fb.default_headers = None

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("run_agent.OpenAI", return_value=MagicMock()),
        # * wait_time=0 so the retry loop's time.time() wait is a no-op.
        patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(mock_fb, fallback_model),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda m, p: m,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ):
        return agent.run_conversation("hello")


class TestStickyOrderPoolWalkIntegration:
    """Real conversation-loop wiring: every pool slug is attempted first."""

    def test_three_timeouts_walk_abc_then_model_fallback(self):
        agent, primary, fallback_model, _pool = _sticky_walk_agent(
            sticky={"enabled": True},
        )
        assert sticky_is_live(agent) is True
        assert apply_sticky_retry_budget(agent, agent._api_max_retries) == 3

        captured = []
        result = _timeout_walk_conversation(agent, fallback_model, captured)

        assert result["completed"] is True
        assert result["final_response"] == "recovered via fallback"
        primary_pins = [row for row in captured if row["model"] == primary]
        assert [row["order"] for row in primary_pins] == [["a"], ["b"], ["c"]]
        assert all(row["allow_fallbacks"] is False for row in primary_pins)
        assert agent._fallback_activated is True
        assert agent.model == fallback_model
        assert any(row["model"] == fallback_model for row in captured)

    def test_retry_backoff_beyond_ttl_still_walks_pool(self):
        """Backoff longer than TTL must not snap the pin back to pool[0]."""
        clock = _Clock(1000.0)
        agent, primary, fallback_model, _pool = _sticky_walk_agent(
            sticky={"enabled": True, "ttl_seconds": 1},
            clock=clock,
        )

        def after_timeout():
            clock.t += 5.0

        captured = []
        result = _timeout_walk_conversation(
            agent,
            fallback_model,
            captured,
            on_primary_timeout=after_timeout,
        )

        assert result["completed"] is True
        primary_pins = [row for row in captured if row["model"] == primary]
        assert [row["order"] for row in primary_pins] == [["a"], ["b"], ["c"]]
        assert all(row["allow_fallbacks"] is False for row in primary_pins)
        assert agent._fallback_activated is True

    def test_idle_between_two_conversations_returns_to_pool_zero(self):
        clock = _Clock(1000.0)
        agent, _primary, fallback_model, _pool = _sticky_walk_agent(
            sticky={"enabled": True, "ttl_seconds": 1},
            clock=clock,
        )
        captured = []
        first = _timeout_walk_conversation(agent, fallback_model, captured)
        assert first["completed"] is True
        assert agent._sticky_provider_order.active_index == 2
        clock.t += 5.0
        begin_sticky_logical_request(agent)
        prefs = _provider_preferences_for_agent(agent)
        assert agent._sticky_provider_order.active_index == 0
        assert prefs["order"] == ["a"]


def _summary_sticky_agent(*, sticky=None, clock=None, order=None):
    """Quiet OpenRouter agent for handle_max_iterations sticky tests."""
    from run_agent import AIAgent

    pool = list(order or ["a", "b", "c"])
    routing = {"order": pool}
    if sticky is not None:
        routing["sticky_order"] = sticky
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch(
            "agent.context_compressor.get_model_context_length",
            return_value=200_000,
        ),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            model="google/gemini-flash",
            providers_order=pool,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.api_mode = "chat_completions"
    agent._cached_system_prompt = "You are helpful."
    agent.suppress_status_output = True
    agent.compression_enabled = False
    apply_provider_routing_to_agent(agent, routing, agent.model)
    if clock is not None:
        agent._sticky_provider_order.clock = clock
    return agent, pool


def _run_iteration_summary(agent, create_impl):
    """Drive handle_max_iterations and capture each extra_body.provider."""
    captured = []

    def fake_create(**kwargs):
        extra = kwargs.get("extra_body") or {}
        captured.append(dict(extra.get("provider") or {}))
        return create_impl(kwargs, captured)

    agent.client.chat.completions.create.side_effect = fake_create
    result = handle_max_iterations(
        agent,
        [{"role": "user", "content": "do stuff"}],
        1,
    )
    return result, captured


class _StatusError(Exception):
    def __init__(self, status_code, message="upstream error"):
        super().__init__(message)
        self.status_code = status_code


class TestStickyOrderSummaryPath:
    """handle_max_iterations is a separate logical request for the pin."""

    def test_expired_ttl_before_summary_uses_pool_zero(self):
        clock = _Clock(1000.0)
        agent, pool = _summary_sticky_agent(
            sticky={"enabled": True, "ttl_seconds": 10},
            clock=clock,
        )
        rotate(agent._sticky_provider_order, "timeout")
        agent._sticky_provider_order.note_attempt()
        assert agent._sticky_provider_order.active_index == 1
        clock.t = 1000.0 + 11.0

        result, captured = _run_iteration_summary(
            agent,
            lambda _kwargs, _captured: _conversation_response("Summary"),
        )

        assert result == "Summary"
        assert captured
        assert captured[0]["order"] == [pool[0]]
        assert captured[0]["allow_fallbacks"] is False
        assert agent._sticky_provider_order.active_index == 0

    def test_timeout_summary_rotates_pin_for_next_request(self):
        agent, pool = _summary_sticky_agent(sticky={"enabled": True})
        assert agent._sticky_provider_order.active_index == 0

        def raise_timeout(_kwargs, _captured):
            raise TimeoutError("request timed out")

        result, captured = _run_iteration_summary(agent, raise_timeout)

        assert "error" in result.lower()
        assert "timed out" in result
        assert captured
        assert captured[0]["order"] == [pool[0]]
        assert agent._sticky_provider_order.active_index == 1
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == [pool[1]]
        assert prefs["allow_fallbacks"] is False

    def test_non_live_summary_tick_does_not_extend_ttl(self):
        """A summary after /model to anthropic_messages must not keep the pin warm."""
        clock = _Clock(1000.0)
        agent, pool = _summary_sticky_agent(
            sticky={"enabled": True, "ttl_seconds": 10},
            clock=clock,
        )
        rotate(agent._sticky_provider_order, "timeout")
        agent._sticky_provider_order.note_attempt()
        assert agent._sticky_provider_order.active_index == 1
        assert agent._sticky_provider_order.last_attempt_at == 1000.0

        agent.api_mode = "anthropic_messages"
        assert sticky_is_live(agent) is False
        clock.t = 1000.0 + 5.0
        note_sticky_attempt(agent)
        assert agent._sticky_provider_order.last_attempt_at == 1000.0

        agent.api_mode = "chat_completions"
        clock.t = 1000.0 + 11.0
        begin_sticky_logical_request(agent)
        assert agent._sticky_provider_order.active_index == 0
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == [pool[0]]

    def test_live_summary_tick_extends_ttl(self):
        """A chat_completions summary is a real pinned request and refreshes TTL."""
        clock = _Clock(1000.0)
        agent, pool = _summary_sticky_agent(
            sticky={"enabled": True, "ttl_seconds": 10},
            clock=clock,
        )
        rotate(agent._sticky_provider_order, "timeout")
        agent._sticky_provider_order.note_attempt()
        assert agent._sticky_provider_order.active_index == 1
        assert agent._sticky_provider_order.last_attempt_at == 1000.0

        clock.t = 1000.0 + 5.0
        result, captured = _run_iteration_summary(
            agent,
            lambda _kwargs, _captured: _conversation_response("Summary"),
        )
        assert result == "Summary"
        assert captured
        assert captured[0]["order"] == [pool[1]]
        assert agent._sticky_provider_order.last_attempt_at == 1005.0
        assert agent._sticky_provider_order.active_index == 1

        clock.t = 1000.0 + 11.0
        begin_sticky_logical_request(agent)
        assert agent._sticky_provider_order.active_index == 1
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == [pool[1]]

    def test_empty_summary_retry_updates_last_attempt_at(self):
        clock = _Clock(1000.0)
        agent, _pool = _summary_sticky_agent(
            sticky={"enabled": True, "ttl_seconds": 10},
            clock=clock,
        )
        rotate(agent._sticky_provider_order, "timeout")
        agent._sticky_provider_order.note_attempt()
        assert agent._sticky_provider_order.active_index == 1
        assert agent._sticky_provider_order.last_attempt_at == 1000.0

        def empty_then_ok(_kwargs, captured):
            if len(captured) == 1:
                clock.t = 1000.0 + 8.0
                return _conversation_response("")
            return _conversation_response("Summary")

        result, captured = _run_iteration_summary(agent, empty_then_ok)

        assert result == "Summary"
        assert len(captured) == 2
        assert captured[0]["order"] == captured[1]["order"] == ["b"]
        assert agent._sticky_provider_order.last_attempt_at == 1008.0
        assert agent._sticky_provider_order.active_index == 1

        clock.t = 1000.0 + 11.0
        begin_sticky_logical_request(agent)
        assert agent._sticky_provider_order.active_index == 1
        prefs = _provider_preferences_for_agent(agent)
        assert prefs["order"] == ["b"]

    def test_rate_limit_and_empty_summary_do_not_rotate(self):
        agent, pool = _summary_sticky_agent(sticky={"enabled": True})

        def raise_429(_kwargs, _captured):
            raise _StatusError(429, "rate limited")

        result, _captured = _run_iteration_summary(agent, raise_429)
        assert "error" in result.lower()
        assert agent._sticky_provider_order.active_index == 0
        assert _provider_preferences_for_agent(agent)["order"] == [pool[0]]

        def empty_then_ok(_kwargs, captured):
            if len(captured) == 1:
                return _conversation_response("")
            return _conversation_response("Summary")

        result, captured = _run_iteration_summary(agent, empty_then_ok)
        assert result == "Summary"
        assert len(captured) == 2
        assert agent._sticky_provider_order.active_index == 0
        assert _provider_preferences_for_agent(agent)["order"] == [pool[0]]

    def test_disabled_summary_provider_block_matches_feature_off(self):
        unbound, pool = _summary_sticky_agent()
        disabled, _ = _summary_sticky_agent(sticky={"enabled": False})

        def ok(_kwargs, _captured):
            return _conversation_response("Summary")

        unbound_result, unbound_captured = _run_iteration_summary(unbound, ok)
        disabled_result, disabled_captured = _run_iteration_summary(disabled, ok)

        assert unbound_result == disabled_result == "Summary"
        assert unbound_captured == disabled_captured
        assert unbound_captured[0]["order"] == pool
        assert "allow_fallbacks" not in unbound_captured[0]
        assert "allow_fallbacks" not in disabled_captured[0]

    def test_sticky_wrapper_failure_does_not_break_summary(self):
        agent, pool = _summary_sticky_agent(sticky={"enabled": True})

        def ok(_kwargs, _captured):
            return _conversation_response("Summary")

        with patch(
            "agent.sticky_provider_order.begin_sticky_logical_request",
            side_effect=RuntimeError("sticky begin failed"),
        ):
            result, captured = _run_iteration_summary(agent, ok)

        assert result == "Summary"
        assert captured[0]["order"] == [pool[0]]

        def raise_timeout(_kwargs, _captured):
            raise TimeoutError("request timed out")

        with patch(
            "agent.sticky_provider_order.rotate_sticky_on_classified_error",
            side_effect=RuntimeError("sticky rotate failed"),
        ):
            result, _captured = _run_iteration_summary(agent, raise_timeout)

        assert "error" in result.lower()
        assert "timed out" in result
        assert agent._sticky_provider_order.active_index == 0
