"""Tests for inbound video frame sampling (command provider + ephemeral tag)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

from agent.agent_runtime_helpers import sanitize_api_messages
from agent.audio_routing import merge_native_media_parts
from agent.video_frame_extract import (
    EPHEMERAL_VIDEO_FRAME,
    HERMES_EPHEMERAL_KEY,
    VIDEO_PERSIST_PLACEHOLDER,
    extract_video_frames,
    frame_extract_is_enabled,
    replace_ephemeral_video_frame_parts,
    strip_ephemeral_metadata_from_content,
)


# 1x1 PNG so extract tests can write a real still without ffmpeg.
_TINY_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc```\x00\x00"
    b"\x00\x04\x00\x01\xdd\x8d\xb4\x1c\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _python_write_frame_command() -> str:
    """Shell command that writes one PNG into {output_dir} and prints JSON."""
    interpreter = sys.executable
    payload = (
        "import json, pathlib, sys; "
        "out=pathlib.Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True); "
        "p=out/'frame-000001.png'; "
        f"p.write_bytes({_TINY_PNG!r}); "
        "sys.stdout.write(json.dumps({'frames':[str(p)], 'fps_used':0.2, 'duration_s':1.0}))"
    )
    return f'"{interpreter}" -c "{payload}" {{output_dir}}'


def _enabled_command_cfg(command: str, name: str = "fake-frames") -> dict:
    return {
        "frame_extract": {
            "enabled": True,
            "provider": name,
            "fps": 0.2,
            "fps_fallback": 0.2,
            "frame_budget": 20,
            "timeout": 30,
        },
        "providers": {
            name: {"type": "command", "command": command},
        },
    }


class TestFrameExtractEnabled:
    def test_default_config_is_off(self):
        assert frame_extract_is_enabled({}) is False
        assert frame_extract_is_enabled({"frame_extract": {}}) is False

    def test_enabled_true(self):
        assert frame_extract_is_enabled(
            {"frame_extract": {"enabled": True}}
        ) is True


class TestExtractCommandProvider:
    def test_disabled_returns_empty(self, tmp_path: Path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-a-real-video")
        assert extract_video_frames(str(video), {"frame_extract": {"enabled": False}}) == []

    def test_command_provider_writes_manifest_frames(self, tmp_path: Path):
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"not-a-real-video")
        cfg = _enabled_command_cfg(_python_write_frame_command())
        frames = extract_video_frames(str(video), cfg)
        assert len(frames) == 1
        assert Path(frames[0]).is_file()
        assert Path(frames[0]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


class TestEphemeralTagAndStrip:
    def test_merge_tags_video_frames_not_photos(self, tmp_path: Path):
        photo = tmp_path / "photo.png"
        frame = tmp_path / "frame.png"
        photo.write_bytes(_TINY_PNG)
        frame.write_bytes(_TINY_PNG)
        content, skipped, _audio = merge_native_media_parts(
            "what is this?",
            [str(photo)],
            None,
            video_frame_paths=[str(frame)],
        )
        assert skipped == []
        assert isinstance(content, list)
        photos = [
            p for p in content
            if p.get("type") == "image_url" and not p.get(HERMES_EPHEMERAL_KEY)
        ]
        frames = [
            p for p in content
            if p.get(HERMES_EPHEMERAL_KEY) == EPHEMERAL_VIDEO_FRAME
        ]
        assert len(photos) == 1
        assert len(frames) == 1

    def test_replace_strips_frames_keeps_photos(self, tmp_path: Path):
        photo = tmp_path / "photo.png"
        frame = tmp_path / "frame.png"
        photo.write_bytes(_TINY_PNG)
        frame.write_bytes(_TINY_PNG)
        content, _, _ = merge_native_media_parts(
            "see",
            [str(photo)],
            video_frame_paths=[str(frame)],
        )
        messages = [{"role": "user", "content": content}]
        assert replace_ephemeral_video_frame_parts(messages) is True
        leftover = messages[0]["content"]
        assert isinstance(leftover, list)
        types = [p.get("type") for p in leftover]
        assert "image_url" in types
        assert not any(
            p.get(HERMES_EPHEMERAL_KEY) == EPHEMERAL_VIDEO_FRAME
            for p in leftover if isinstance(p, dict)
        )
        assert any(
            p.get("type") == "text" and VIDEO_PERSIST_PLACEHOLDER in str(p.get("text"))
            for p in leftover if isinstance(p, dict)
        )

    def test_sanitize_api_messages_drops_internal_key(self, tmp_path: Path):
        frame = tmp_path / "frame.png"
        frame.write_bytes(_TINY_PNG)
        content, _, _ = merge_native_media_parts(
            "see",
            video_frame_paths=[str(frame)],
        )
        live = [{"role": "user", "content": content}]
        assert any(
            isinstance(p, dict) and HERMES_EPHEMERAL_KEY in p
            for p in content
        )
        out = sanitize_api_messages(live)
        wire_content = out[0]["content"]
        assert all(
            HERMES_EPHEMERAL_KEY not in p
            for p in wire_content if isinstance(p, dict)
        )
        # * Live messages keep the tag for end-of-turn strip.
        assert any(
            isinstance(p, dict) and HERMES_EPHEMERAL_KEY in p
            for p in live[0]["content"]
        )

    def test_strip_helper_copies_on_change(self):
        parts = [
            {"type": "text", "text": "x"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AA"},
                HERMES_EPHEMERAL_KEY: EPHEMERAL_VIDEO_FRAME,
            },
        ]
        cleaned = strip_ephemeral_metadata_from_content(parts)
        assert cleaned is not parts
        assert HERMES_EPHEMERAL_KEY not in cleaned[1]
        assert HERMES_EPHEMERAL_KEY in parts[1]
