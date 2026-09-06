"""Request-time per-model service_tier_overrides and /fast pin provenance.

Resolution lives in ``agent.fast_mode.effective_request_overrides``:

session ``/fast`` pin > ``agent.service_tier_overrides`` > global ``agent.service_tier``.

Routing overlays belong in ``tests/agent/test_per_model_provider_routing.py``.
"""

from types import SimpleNamespace

import pytest

from agent import fast_mode
from hermes_cli.models import resolve_fast_mode_overrides, resolve_service_tier_overrides
from hermes_constants import parse_service_tier, resolve_service_tier_for_model, service_tier_status_label


_OPENROUTER = "https://openrouter.ai/api/v1"
_OPENAI = "https://api.openai.com/v1"
_NOUS = "https://inference-api.nousresearch.com/v1"
_MATCH = "openai/gpt-5"


def _agent(**kw):
    base = dict(
        service_tier=None,
        model=_MATCH,
        provider="openrouter",
        base_url=_OPENROUTER,
        api_mode="chat_completions",
        request_overrides={"extra_body": {"keep": 1}},
        fast_auto_seconds=60,
        _service_tier_session_pinned=False,
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def tier_cfg(monkeypatch):
    cfg = {
        "agent": {
            "service_tier": "priority",
            "service_tier_overrides": {_MATCH: "flex"},
        }
    }
    import hermes_cli.config as config_mod

    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: cfg)
    return cfg


def test_default_config_effective_overrides_match_baseline(monkeypatch):
    """Default-off: no new keys, no empty objects, extra_body unchanged."""
    import hermes_cli.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_config_readonly",
        lambda: {"agent": {"service_tier": "", "service_tier_overrides": {}}},
    )
    agent = _agent(provider="openai", base_url=_OPENAI, request_overrides={"extra_body": {"keep": 1}})
    orig = dict(agent.request_overrides)
    assert fast_mode.effective_request_overrides(agent) == {"extra_body": {"keep": 1}}
    assert agent.request_overrides == orig

    empty = _agent(provider="openai", base_url=_OPENAI, request_overrides={})
    assert fast_mode.effective_request_overrides(empty) == {}


def test_pin_beats_per_model_beats_global(tier_cfg):
    unpinned = _agent()
    assert fast_mode.logical_service_tier(unpinned) == "flex"
    assert fast_mode.effective_request_overrides(unpinned)["service_tier"] == "flex"

    other = _agent(model="moonshotai/kimi-k2.6")
    assert fast_mode.logical_service_tier(other) == "priority"
    assert fast_mode.effective_request_overrides(other)["service_tier"] == "priority"

    pinned_normal = _agent(_service_tier_session_pinned=True, service_tier=None)
    assert fast_mode.logical_service_tier(pinned_normal) is None
    assert "service_tier" not in fast_mode.effective_request_overrides(pinned_normal)

    pinned_priority = _agent(_service_tier_session_pinned=True, service_tier="priority")
    assert fast_mode.logical_service_tier(pinned_priority) == "priority"
    assert fast_mode.effective_request_overrides(pinned_priority)["service_tier"] == "priority"


def test_fast_normal_pin_blocks_per_model_flex(tier_cfg):
    agent = _agent(_service_tier_session_pinned=True, service_tier=None)
    assert service_tier_status_label(fast_mode.logical_service_tier(agent)) == "normal"
    wire = fast_mode.effective_request_overrides(agent)
    assert "service_tier" not in wire
    assert wire["extra_body"] == {"keep": 1}


def test_unpinned_model_switch_picks_new_overlay(tier_cfg):
    agent = _agent()
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "flex"
    agent.model = "moonshotai/kimi-k2.6"
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "priority"


def test_spelling_tolerant_override_match():
    cfg = {"service_tier_overrides": {"gpt-5": "flex"}, "service_tier": ""}
    assert resolve_service_tier_for_model(cfg, "openai/gpt-5") == "flex"
    assert resolve_service_tier_for_model(cfg, "openrouter/openai/gpt-5") == "flex"
    assert resolve_service_tier_for_model(cfg, "gpt-5") == "flex"
    assert resolve_service_tier_for_model(cfg, "other-model", fallback="priority") == "priority"


def test_fallback_model_uses_that_models_overlay(tier_cfg):
    agent = _agent(model=_MATCH)
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "flex"
    agent.model = "openai/gpt-4.1"
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "priority"


def test_delegated_child_does_not_inherit_parent_pin(tier_cfg):
    parent = _agent(_service_tier_session_pinned=True, service_tier=None)
    child = _agent(_service_tier_session_pinned=False, model=_MATCH)
    assert fast_mode.logical_service_tier(parent) is None
    assert fast_mode.logical_service_tier(child) == "flex"
    assert fast_mode.effective_request_overrides(child)["service_tier"] == "flex"


def test_nous_and_first_party_ignore_flex():
    nous = _agent(
        provider="nous", base_url=_NOUS, service_tier="flex",
        _service_tier_session_pinned=True,
    )
    wire = fast_mode.effective_request_overrides(nous)
    assert "service_tier" not in wire
    assert "speed" not in wire
    assert wire["extra_body"] == {"keep": 1}

    openai = _agent(
        provider="openai", base_url=_OPENAI, model="gpt-5.4", service_tier="flex",
        _service_tier_session_pinned=True,
    )
    wire = fast_mode.effective_request_overrides(openai)
    assert "service_tier" not in wire
    assert "speed" not in wire

    portal = resolve_service_tier_overrides(
        "gpt-5.4", "flex", provider="nous", base_url=_NOUS,
    )
    assert portal is None


def test_openrouter_any_catalog_model_gets_flex_and_priority():
    llama = "meta-llama/llama-3.1-8b-instruct"
    assert resolve_fast_mode_overrides(
        llama, provider="openrouter", base_url=_OPENROUTER,
    ) == {"service_tier": "priority"}
    assert resolve_service_tier_overrides(
        llama, "flex", provider="openrouter", base_url=_OPENROUTER,
    ) == {"service_tier": "flex"}
    agent = _agent(model=llama, service_tier="flex", _service_tier_session_pinned=True)
    assert fast_mode.effective_request_overrides(agent)["service_tier"] == "flex"


def test_extra_body_not_rewritten_when_tier_applied(tier_cfg):
    extra = {"keep": 1, "provider": {"order": ["anthropic"]}}
    agent = _agent(request_overrides={"extra_body": extra, "temperature": 0.2})
    wire = fast_mode.effective_request_overrides(agent)
    assert wire["service_tier"] == "flex"
    assert wire["extra_body"] is extra
    assert wire["temperature"] == 0.2
    assert agent.request_overrides["extra_body"] is extra
    assert "service_tier" not in agent.request_overrides


def test_stale_parent_tier_keys_are_replaced(tier_cfg):
    agent = _agent(request_overrides={"service_tier": "priority", "speed": "fast", "extra_body": {"keep": 1}})
    wire = fast_mode.effective_request_overrides(agent)
    assert wire["service_tier"] == "flex"
    assert "speed" not in wire
    assert agent.request_overrides["service_tier"] == "priority"


def test_parse_and_status_labels():
    assert parse_service_tier("flex") == "flex"
    assert parse_service_tier("FAST") == "priority"
    assert parse_service_tier("normal") is None
    assert parse_service_tier("") is None
    assert service_tier_status_label("priority") == "fast"
    assert service_tier_status_label("flex") == "flex"
    assert service_tier_status_label(None) == "normal"
