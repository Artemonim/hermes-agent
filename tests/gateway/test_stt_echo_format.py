"""Tests for gateway STT transcript echo formatting."""

from gateway.config import Platform
from gateway.stt_echo import format_stt_transcript_echo, stt_echo_metadata


def test_non_telegram_keeps_classic_quoted_line():
    assert format_stt_transcript_echo("hello once", Platform.DISCORD) == '🎙️ "hello once"'
    assert format_stt_transcript_echo("hello once", "whatsapp") == '🎙️ "hello once"'
    assert stt_echo_metadata(Platform.DISCORD, {"thread_id": 1}) == {"thread_id": 1}


def test_telegram_uses_html_expandable_blockquote():
    formatted = format_stt_transcript_echo("hello once", Platform.TELEGRAM)

    assert formatted == "🎙️\n<blockquote expandable>hello once</blockquote>"
    assert stt_echo_metadata(Platform.TELEGRAM, None) == {"telegram_html": True}
    assert stt_echo_metadata("telegram", {"thread_id": 7}) == {
        "thread_id": 7,
        "telegram_html": True,
    }


def test_telegram_multiline_and_markdown_chars_are_html_escaped():
    formatted = format_stt_transcript_echo(
        "line one\n**bold** & <tag>\nline three",
        "telegram",
    )

    assert formatted == (
        "🎙️\n"
        "<blockquote expandable>"
        "line one\n"
        "**bold** &amp; &lt;tag&gt;\n"
        "line three"
        "</blockquote>"
    )
