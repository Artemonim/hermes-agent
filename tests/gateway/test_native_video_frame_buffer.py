"""Sampled video stills are session-scoped, like native images and audio."""

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
    runner._decide_image_input_mode = lambda **_: "native"
    runner._decide_audio_input_mode = lambda **_: "text"
    runner._should_echo_stt_transcripts = lambda: False
    return runner


def _source(chat_id: str) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=chat_id,
        chat_type="private",
        user_name=f"user-{chat_id}",
    )


def _video_event(source: SessionSource, path: str) -> MessageEvent:
    return MessageEvent(
        text="what happens here?",
        message_type=MessageType.VIDEO,
        source=source,
        media_urls=[path],
        media_types=["video/mp4"],
    )


@pytest.mark.asyncio
async def test_video_frame_buffer_isolated_per_session(tmp_path):
    runner = _make_runner()
    source_a = _source("chat-a")
    source_b = _source("chat-b")
    frame_a = str(tmp_path / "a.png")
    frame_b = str(tmp_path / "b.png")

    def _extract(path, video_cfg=None):
        return [frame_a] if path.endswith("a.mp4") else [frame_b]

    with patch(
        "agent.video_frame_extract.frame_extract_is_enabled",
        return_value=True,
    ), patch(
        "agent.video_frame_extract.extract_video_frames",
        side_effect=_extract,
    ), patch(
        "agent.video_frame_extract.max_frames_per_turn",
        return_value=20,
    ), patch(
        "tools.credential_files.to_agent_visible_cache_path",
        side_effect=lambda path: path,
    ):
        await runner._prepare_inbound_message_text(
            event=_video_event(source_a, "/tmp/a.mp4"),
            source=source_a,
            history=[],
        )
        await runner._prepare_inbound_message_text(
            event=_video_event(source_b, "/tmp/b.mp4"),
            source=source_b,
            history=[],
        )

    assert runner._consume_pending_native_video_frame_paths(
        build_session_key(source_a)
    ) == [frame_a]
    assert runner._consume_pending_native_video_frame_paths(
        build_session_key(source_b)
    ) == [frame_b]


@pytest.mark.asyncio
async def test_video_path_note_mentions_sampled_frames_when_extract_hits():
    runner = _make_runner()
    source = _source("chat-frames")
    with patch(
        "agent.video_frame_extract.frame_extract_is_enabled",
        return_value=True,
    ), patch(
        "agent.video_frame_extract.extract_video_frames",
        return_value=["/tmp/frame-000001.png"],
    ), patch(
        "agent.video_frame_extract.max_frames_per_turn",
        return_value=20,
    ), patch(
        "tools.credential_files.to_agent_visible_cache_path",
        side_effect=lambda path: path,
    ):
        text = await runner._prepare_inbound_message_text(
            event=_video_event(source, "/tmp/clip.mp4"),
            source=source,
            history=[],
        )

    assert "sampled frame(s) are attached on this turn" in text
    assert "/tmp/clip.mp4" in text
    assert runner._consume_pending_native_video_frame_paths(
        build_session_key(source)
    ) == ["/tmp/frame-000001.png"]


@pytest.mark.asyncio
async def test_disabled_extract_keeps_path_note_and_empty_buffer():
    runner = _make_runner()
    source = _source("chat-off")
    with patch(
        "tools.credential_files.to_agent_visible_cache_path",
        side_effect=lambda path: path,
    ):
        text = await runner._prepare_inbound_message_text(
            event=_video_event(source, "/tmp/clip.mp4"),
            source=source,
            history=[],
        )

    assert "Its content is not inlined here" in text
    assert runner._consume_pending_native_video_frame_paths(
        build_session_key(source)
    ) == []
