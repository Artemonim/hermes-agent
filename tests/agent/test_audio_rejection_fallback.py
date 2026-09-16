"""Conversation-loop recovery when a provider rejects native audio.

The audio-rejection branch sits in the generic ``except Exception as
api_error`` handler, *before* classification / retry. A missing
``_err_lower`` assignment there turned every non-image API exception
(stale-watchdog kills, timeouts, unknown 4xx) into an uncaught
``NameError`` that killed the session instead of retrying.

These tests drive ``run_conversation`` through ``_interruptible_api_call``
so the except-handler actually runs.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.audio_routing import AUDIO_PERSIST_PLACEHOLDER


class _FakeApiError(Exception):
    """Stand-in for an OpenAI-compatible error with status_code + body."""

    def __init__(self, message: str, status_code: int | None = None, body=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body if body is not None else message
        self.message = message


def _mock_response(content: str):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            provider="openrouter",
            api_mode="chat_completions",
            model="z-ai/glm-5.3-flash",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.compression_enabled = False
        agent.save_trajectories = False
        agent._api_max_retries = 3
        return agent


def _run(agent, message, conversation_history=None, fake_api_call=None):
    patches = [
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch("agent.agent_runtime_helpers.time.sleep"),
        patch("agent.model_metadata.get_model_context_length", return_value=200000),
    ]
    if fake_api_call is not None:
        patches.append(
            patch.object(agent, "_interruptible_api_call", side_effect=fake_api_call)
        )
    for p in patches:
        p.start()
    try:
        return agent.run_conversation(
            message, conversation_history=conversation_history
        )
    finally:
        for p in patches:
            p.stop()


def _has_input_audio(msgs):
    return any(
        isinstance(m.get("content"), list)
        and any(
            isinstance(p, dict) and p.get("type") == "input_audio"
            for p in m["content"]
        )
        for m in msgs
    )


def _audio_history():
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "listen to this"},
                {
                    "type": "input_audio",
                    "input_audio": {"data": "QQ==", "format": "mp3"},
                },
            ],
        },
        {"role": "assistant", "content": "ok"},
    ]


class TestStaleAbortDoesNotNameError:
    def test_connection_abort_retries_and_completes(self):
        """Stale-watchdog kill must reach retry, not ``NameError: _err_lower``."""
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise _FakeApiError(
                    "APIConnectionError: Connection aborted. Inline "
                    "non-streaming API call stale for 90/150s. "
                    "Killing connection."
                )
            return _mock_response("recovered after abort")

        agent = _make_agent()
        result = _run(agent, "continue the cluster", fake_api_call=fake_api_call)

        assert "NameError" not in str(result.get("error") or "")
        assert result.get("completed") is True
        assert result["final_response"] == "recovered after abort"
        assert len(calls) == 2

    def test_stale_abort_does_not_strip_native_audio(self):
        """Watchdog abort has no status_code so ``_status_ok`` is True; the
        phrase detector must still refuse to treat it as audio rejection."""
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(copy.deepcopy(api_kwargs.get("messages")))
            if len(calls) == 1:
                raise _FakeApiError(
                    "APIConnectionError: Connection aborted. Inline "
                    "non-streaming API call stale for 90/150s. "
                    "Killing connection."
                )
            return _mock_response("recovered after abort")

        agent = _make_agent()
        agent._model_supports_audio = lambda: True
        result = _run(
            agent,
            "what did they say?",
            conversation_history=_audio_history(),
            fake_api_call=fake_api_call,
        )

        assert result.get("completed") is True
        assert len(calls) == 2
        assert _has_input_audio(calls[0]) is True
        assert _has_input_audio(calls[1]) is True


class TestAudioRejectionStripsAndRetries:
    def test_audio_400_strips_input_audio_and_retries(self):
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(copy.deepcopy(api_kwargs.get("messages")))
            if len(calls) == 1:
                raise _FakeApiError(
                    "This model does not support audio input",
                    status_code=400,
                    body="This model does not support audio input",
                )
            return _mock_response("continuing from the transcript")

        agent = _make_agent()
        # * Native audio is stripped from the API copy when the model cannot
        #   hear. Force capability on so the 4xx recovery sees input_audio.
        agent._model_supports_audio = lambda: True
        result = _run(
            agent,
            "what did they say?",
            conversation_history=_audio_history(),
            fake_api_call=fake_api_call,
        )

        assert result.get("completed") is True
        assert result["final_response"] == "continuing from the transcript"
        assert len(calls) == 2
        assert _has_input_audio(calls[0]) is True
        assert _has_input_audio(calls[1]) is False
        assert any(
            AUDIO_PERSIST_PLACEHOLDER in str(m.get("content"))
            for m in calls[1]
        )

    def test_audio_phrase_on_503_does_not_strip(self):
        """5xx must use the normal retry path even if the body contains an
        audio-rejection phrase."""
        calls = []

        def fake_api_call(api_kwargs):
            calls.append(copy.deepcopy(api_kwargs.get("messages")))
            if len(calls) == 1:
                raise _FakeApiError(
                    "This model does not support audio input",
                    status_code=503,
                    body="This model does not support audio input",
                )
            return _mock_response("provider recovered")

        agent = _make_agent()
        agent._model_supports_audio = lambda: True
        result = _run(
            agent,
            "what did they say?",
            conversation_history=_audio_history(),
            fake_api_call=fake_api_call,
        )

        assert result.get("completed") is True
        assert len(calls) >= 2
        assert _has_input_audio(calls[0]) is True
        assert _has_input_audio(calls[1]) is True
