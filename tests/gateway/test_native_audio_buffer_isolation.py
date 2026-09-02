"""Native audio path buffer is session-scoped, like native images."""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key


def _make_runner() -> GatewayRunner:
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
        stt_enabled=True,
    )
    runner.adapters = {}
    runner._model = "google/gemini-3.7-flash"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_: "text"
    runner._decide_audio_input_mode = lambda **_: "native"
    runner._should_echo_stt_transcripts = lambda: False
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _voice_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="",
        message_type=MessageType.VOICE,
        source=source,
        media_urls=[path],
        media_types=["audio/ogg"],
    )


@pytest.mark.asyncio
async def test_native_audio_buffer_isolated_per_session():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello", "provider": "test"},
    ):
        await runner._prepare_inbound_message_text(
            event=_voice_event(source_a, "/tmp/a.ogg"),
            source=source_a,
            history=[],
        )
        await runner._prepare_inbound_message_text(
            event=_voice_event(source_b, "/tmp/b.ogg"),
            source=source_b,
            history=[],
        )

    assert runner._consume_pending_native_audio_paths(build_session_key(source_a)) == [
        "/tmp/a.ogg"
    ]
    assert runner._consume_pending_native_audio_paths(build_session_key(source_b)) == [
        "/tmp/b.ogg"
    ]


@pytest.mark.asyncio
async def test_voice_stt_still_runs_when_native_audio_is_staged():
    runner = _make_runner()
    source = _source("chat-stt")
    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello world", "provider": "test"},
    ) as mock_stt:
        result = await runner._prepare_inbound_message_text(
            event=_voice_event(source, "/tmp/voice.ogg"),
            source=source,
            history=[],
        )
    mock_stt.assert_called_once_with("/tmp/voice.ogg", None, "gateway")
    assert "hello world" in result
    assert runner._consume_pending_native_audio_paths(build_session_key(source)) == [
        "/tmp/voice.ogg"
    ]


@pytest.mark.asyncio
async def test_native_audio_buffer_not_cleared_by_other_sessions_without_audio():
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")

    with patch(
        "tools.transcription_tools.transcribe_audio",
        return_value={"success": True, "transcript": "hello", "provider": "test"},
    ):
        await runner._prepare_inbound_message_text(
            event=_voice_event(source_a, "/tmp/a.ogg"),
            source=source_a,
            history=[],
        )

    text_event = MessageEvent(
        text="just text",
        message_type=MessageType.TEXT,
        source=source_b,
        media_urls=[],
        media_types=[],
    )
    await runner._prepare_inbound_message_text(
        event=text_event,
        source=source_b,
        history=[],
    )

    assert runner._consume_pending_native_audio_paths(build_session_key(source_a)) == [
        "/tmp/a.ogg"
    ]
    assert runner._consume_pending_native_audio_paths(build_session_key(source_b)) == []
