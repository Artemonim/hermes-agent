"""Tests for audio_analyze — native envelope vs STT fallback vs in-flight cap."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from tools.audio_tools import (
    _build_native_audio_tool_result,
    audio_analyze,
)


def _write_clip(tmp_path: Path) -> Path:
    clip = tmp_path / "note.mp3"
    clip.write_bytes(b"ID3" + b"\x00" * 16)
    return clip


class TestBuildNativeAudioToolResult:
    def test_envelope_shape(self):
        part = {
            "type": "input_audio",
            "input_audio": {"data": "QQ==", "format": "mp3"},
        }
        env = _build_native_audio_tool_result(
            "/tmp/foo.mp3",
            "what language?",
            part,
            1024,
        )
        assert env["_multimodal"] is True
        assert env["content"][0]["type"] == "text"
        assert env["content"][1]["type"] == "input_audio"
        assert "what language?" in env["content"][0]["text"]
        assert env["meta"]["native_audio"] is True


class TestAudioAnalyzeRouting:
    def test_native_fast_path(self, tmp_path: Path):
        clip = _write_clip(tmp_path)
        fake_part = {
            "type": "input_audio",
            "input_audio": {"data": "QQ==", "format": "mp3"},
        }
        with patch("tools.audio_tools._should_use_native_audio_fast_path", return_value=True), \
             patch("agent.audio_routing.file_to_input_audio_part", return_value=fake_part), \
             patch("agent.audio_routing.mark_native_audio_in_flight") as mark, \
             patch("agent.audio_routing.path_looks_like_video", return_value=False):
            out = audio_analyze(str(clip), question="transcribe")
        assert isinstance(out, dict)
        assert out["_multimodal"] is True
        assert out["content"][1]["type"] == "input_audio"
        mark.assert_called_once()

    def test_stt_fallback_when_not_native(self, tmp_path: Path):
        clip = _write_clip(tmp_path)
        with patch("tools.audio_tools._should_use_native_audio_fast_path", return_value=False), \
             patch("agent.audio_routing.native_audio_in_flight", return_value=False), \
             patch(
                 "tools.transcription_tools.transcribe_audio",
                 return_value={
                     "success": True,
                     "transcript": "hello from stt",
                     "provider": "command",
                 },
             ):
            out = audio_analyze(str(clip))
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["native_audio"] is False
        assert payload["transcript"] == "hello from stt"
        assert payload["reason"] == "model_does_not_support_native_audio"

    def test_in_flight_uses_stt_not_second_clip(self, tmp_path: Path):
        clip = _write_clip(tmp_path)
        with patch("tools.audio_tools._should_use_native_audio_fast_path", return_value=False), \
             patch("agent.audio_routing.native_audio_in_flight", return_value=True), \
             patch(
                 "tools.transcription_tools.transcribe_audio",
                 return_value={
                     "success": True,
                     "transcript": "second clip as text",
                     "provider": "command",
                 },
             ):
            out = audio_analyze(str(clip))
        payload = json.loads(out)
        assert payload["reason"] == "native_audio_already_in_flight"
        assert payload["transcript"] == "second clip as text"

    def test_rejects_http_url(self):
        out = audio_analyze("https://example.com/clip.mp3")
        assert "local file" in str(out).lower() or "audio_url" in str(out).lower()
