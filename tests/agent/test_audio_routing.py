"""Tests for agent/audio_routing.py — native audio input mode and persist strip."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import patch

from agent.audio_routing import (
    AUDIO_PERSIST_PLACEHOLDER,
    MAX_NATIVE_AUDIO_CLIPS,
    append_native_audio_parts,
    build_native_audio_parts,
    clear_native_audio_in_flight,
    content_has_audio_parts,
    decide_audio_input_mode,
    flatten_audio_parts_for_persist,
    gemini_inline_audio_from_part,
    gemini_slug_supports_native_audio,
    looks_like_audio_content_rejection,
    mark_native_audio_in_flight,
    merge_native_media_parts,
    native_audio_in_flight,
    replace_audio_parts_with_placeholder,
)


# Minimal ID3-tagged payload so sniff_container reports mp3.
_TINY_MP3 = b"ID3" + b"\x00" * 13 + b"\xff\xfb" + b"\x00" * 32


class TestGeminiSlugHeuristic:
    def test_gemini_37_flash_true(self):
        assert gemini_slug_supports_native_audio("google/gemini-3.7-flash") is True
        assert gemini_slug_supports_native_audio("gemini-3.7-flash") is True

    def test_gemini_35_true(self):
        assert gemini_slug_supports_native_audio("gemini-3.5-flash") is True

    def test_gemini_30_false(self):
        assert gemini_slug_supports_native_audio("gemini-3-flash") is False
        assert gemini_slug_supports_native_audio("gemini-3.0-flash") is False

    def test_gemini_2x_unknown(self):
        assert gemini_slug_supports_native_audio("gemini-2.5-flash") is None

    def test_non_gemini_unknown(self):
        assert gemini_slug_supports_native_audio("deepseek/deepseek-v4-flash") is None
        assert gemini_slug_supports_native_audio("") is None


class TestDecideAudioInputMode:
    def test_explicit_native(self):
        cfg = {"agent": {"audio_input_mode": "native"}}
        with patch("agent.audio_routing._lookup_supports_audio", return_value=False):
            assert decide_audio_input_mode("openrouter", "x", cfg) == "native"

    def test_explicit_text(self):
        cfg = {"agent": {"audio_input_mode": "text"}}
        with patch("agent.audio_routing._lookup_supports_audio", return_value=True):
            assert decide_audio_input_mode("openrouter", "google/gemini-3.7-flash", cfg) == "text"

    def test_auto_native_when_capable(self):
        with patch("agent.audio_routing._lookup_supports_audio", return_value=True):
            assert decide_audio_input_mode("openrouter", "google/gemini-3.7-flash", {}) == "native"

    def test_auto_text_when_unknown(self):
        with patch("agent.audio_routing._lookup_supports_audio", return_value=None):
            assert decide_audio_input_mode("openrouter", "brand-new-slug", {}) == "text"

    def test_override_supports_audio(self):
        cfg = {"model": {"supports_audio": True}}
        with patch("agent.models_dev.get_model_capabilities", return_value=None):
            assert decide_audio_input_mode("custom", "local-whisper-llm", cfg) == "native"

    def test_heuristic_upgrades_stale_catalog(self):
        class _Caps:
            supports_audio = False

        from agent.audio_routing import _lookup_supports_audio

        with patch("agent.models_dev.get_model_capabilities", return_value=_Caps()):
            assert _lookup_supports_audio(
                "openrouter", "google/gemini-3.7-flash", {}
            ) is True


class TestBuildNativeAudioParts:
    def test_attaches_mp3_and_caps_at_one(self, tmp_path: Path):
        a = tmp_path / "a.mp3"
        b = tmp_path / "b.mp3"
        a.write_bytes(_TINY_MP3)
        b.write_bytes(_TINY_MP3)
        parts, skipped = build_native_audio_parts("hello", [str(a), str(b)])
        assert MAX_NATIVE_AUDIO_CLIPS == 1
        assert skipped == [str(b)]
        types = [p.get("type") for p in parts]
        assert types.count("input_audio") == 1
        assert parts[0]["type"] == "text"
        assert "hello" in parts[0]["text"]
        payload = parts[1]["input_audio"]
        assert payload["format"] == "mp3"
        assert base64.b64decode(payload["data"]) == _TINY_MP3

    def test_missing_file_skipped(self, tmp_path: Path):
        missing = tmp_path / "nope.mp3"
        parts, skipped = build_native_audio_parts("hi", [str(missing)])
        assert skipped == [str(missing)]
        assert parts == [{"type": "text", "text": "hi"}]


class TestReplaceAndFlatten:
    def test_flatten_keeps_text_screenshot_and_audio(self):
        content = [
            {"type": "text", "text": "caption"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {
                "type": "input_audio",
                "input_audio": {"data": "QQ==", "format": "mp3"},
            },
        ]
        assert flatten_audio_parts_for_persist(content) == (
            f"caption\n[screenshot]\n{AUDIO_PERSIST_PLACEHOLDER}"
        )

    def test_flatten_ephemeral_video_frames_as_video(self):
        content = [
            {"type": "text", "text": "clip"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
                "_hermes_ephemeral": "video_frame",
            },
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}},
        ]
        assert flatten_audio_parts_for_persist(content) == (
            "clip\n[video]\n[screenshot]"
        )

    def test_replace_preserves_images(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "see"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "QQ==", "format": "mp3"},
                    },
                ],
            }
        ]
        assert replace_audio_parts_with_placeholder(messages) is True
        content = messages[0]["content"]
        assert isinstance(content, list)
        types = [p.get("type") for p in content]
        assert "input_audio" not in types
        assert "image_url" in types
        assert any(
            p.get("type") == "text" and AUDIO_PERSIST_PLACEHOLDER in str(p.get("text"))
            for p in content
        )

    def test_replace_collapses_audio_only_to_text(self):
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "voice note"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "QQ==", "format": "mp3"},
                    },
                ],
            }
        ]
        assert replace_audio_parts_with_placeholder(messages) is True
        assert messages[0]["content"] == f"voice note\n{AUDIO_PERSIST_PLACEHOLDER}"


class TestGeminiInline:
    def test_input_audio_becomes_inline_data(self):
        part = {
            "type": "input_audio",
            "input_audio": {"data": "abc", "format": "mp3"},
        }
        out = gemini_inline_audio_from_part(part)
        assert out == {
            "inlineData": {"mimeType": "audio/mp3", "data": "abc"},
        }


class TestInFlightFlag:
    def test_mark_and_clear(self):
        clear_native_audio_in_flight()
        assert native_audio_in_flight() is False
        mark_native_audio_in_flight()
        assert native_audio_in_flight() is True
        clear_native_audio_in_flight()
        assert native_audio_in_flight() is False


class TestMergeNativeMedia:
    def test_audio_appended_to_image_parts(self, tmp_path: Path):
        audio = tmp_path / "clip.mp3"
        audio.write_bytes(_TINY_MP3)
        image_parts = [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        with patch(
            "agent.image_routing.build_native_content_parts",
            return_value=(image_parts, []),
        ):
            content, skipped_img, skipped_audio = merge_native_media_parts(
                "look",
                ["/tmp/a.png"],
                [str(audio)],
            )
        assert skipped_img == []
        assert skipped_audio == []
        types = [p.get("type") for p in content]
        assert "image_url" in types
        assert "input_audio" in types


class TestAppendCapsExistingAudio:
    def test_does_not_add_second_clip(self, tmp_path: Path):
        existing = [
            {"type": "text", "text": "first"},
            {
                "type": "input_audio",
                "input_audio": {"data": "QQ==", "format": "mp3"},
            },
        ]
        extra = tmp_path / "extra.mp3"
        extra.write_bytes(_TINY_MP3)
        out, skipped = append_native_audio_parts(existing, [str(extra)])
        assert skipped == [str(extra)]
        assert content_has_audio_parts(out)
        assert sum(1 for p in out if p.get("type") == "input_audio") == 1


class TestPrepareNonAudioModel:
    def test_strips_when_model_cannot_hear(self):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.provider = "deepseek"
        agent.model = "deepseek-chat"
        agent.requested_provider = ""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "QQ==", "format": "mp3"},
                    },
                ],
            }
        ]
        with patch.object(agent, "_model_supports_audio", return_value=False):
            out = agent._prepare_messages_for_non_audio_model(msgs)
        assert out[0]["content"] == f"hi\n{AUDIO_PERSIST_PLACEHOLDER}"
        assert native_audio_in_flight() is False

    def test_marks_in_flight_when_model_hears(self):
        from run_agent import AIAgent

        agent = object.__new__(AIAgent)
        agent.provider = "openrouter"
        agent.model = "google/gemini-3.7-flash"
        agent.requested_provider = ""
        msgs = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hi"},
                    {
                        "type": "input_audio",
                        "input_audio": {"data": "QQ==", "format": "mp3"},
                    },
                ],
            }
        ]
        clear_native_audio_in_flight()
        with patch.object(agent, "_model_supports_audio", return_value=True):
            out = agent._prepare_messages_for_non_audio_model(msgs)
        assert out[0]["content"][1]["type"] == "input_audio"
        assert native_audio_in_flight() is True
        clear_native_audio_in_flight()


class TestLooksLikeAudioContentRejection:
    """Phrase detector used by the conversation-loop audio 4xx recovery."""

    def test_openrouter_and_gemini_wordings_trip(self):
        bodies = [
            "This model does not support audio input",
            "Bad request: audio input is not supported by this endpoint",
            "HTTP 404: No endpoints found that support audio",
            "Gemini 3.5 Flash: only one audio file is allowed per request",
            "maximum number of audio files exceeded",
            "too many audio files in the prompt",
        ]
        for body in bodies:
            assert looks_like_audio_content_rejection(body) is True, body

    def test_case_insensitive(self):
        assert looks_like_audio_content_rejection(
            "AUDIO INPUT IS NOT SUPPORTED"
        ) is True

    def test_stale_watchdog_abort_does_not_trip(self):
        """The live NameError trigger: a killed connection must not look
        like an audio-capability rejection, so the loop can retry."""
        bodies = [
            "APIConnectionError: Connection aborted.",
            "Inline non-streaming API call stale for 90/150s. "
            "model=z-ai/glm-5.3-flash context=~36000 tokens. Killing connection.",
            "Request timed out after 150s",
            "",
            None,
        ]
        for body in bodies:
            assert looks_like_audio_content_rejection(body) is False, body

    def test_image_rejection_wording_does_not_trip(self):
        assert looks_like_audio_content_rejection(
            "Only 'text' content type is supported."
        ) is False
        assert looks_like_audio_content_rejection(
            "No endpoints found that support image input"
        ) is False
