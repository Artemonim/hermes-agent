"""User-facing formatting for gateway STT transcript echoes."""

from __future__ import annotations

import html
from typing import Any, Dict, Optional


def _platform_key(platform: Any) -> str:
    """Normalize a Platform enum / string into a lowercase key."""
    if platform is None:
        return ""
    value = getattr(platform, "value", platform)
    return str(value or "").strip().lower().replace("-", "_")


def _is_telegram_platform(platform: Any) -> bool:
    key = _platform_key(platform)
    return key == "telegram" or key.endswith("_telegram") or key.startswith("telegram_")


def format_stt_transcript_echo(transcript: str, platform: Optional[Any] = None) -> str:
    """Format a successful STT transcript for the optional user-facing echo.

    Telegram receives an HTML expandable blockquote (collapsed quote) so long
    transcripts stay out of the way and markdown characters in the transcript
    cannot break formatting. Other platforms keep the classic ``🎙️ "..."``
    plain line.
    """
    text = (transcript or "").strip("\n")
    if not text.strip():
        return "🎙️"
    if _is_telegram_platform(platform):
        return _format_telegram_expandable_stt_echo(text)
    return f'🎙️ "{text}"'


def stt_echo_metadata(
    platform: Optional[Any] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Return send metadata for an STT echo, enabling Telegram HTML delivery."""
    if not _is_telegram_platform(platform):
        return metadata
    merged = dict(metadata or {})
    merged["telegram_html"] = True
    return merged


def _format_telegram_expandable_stt_echo(transcript: str) -> str:
    """Wrap *transcript* as a Telegram HTML expandable blockquote.

    Callers must send this with ``metadata["telegram_html"]=True`` so the
    Telegram adapter skips MarkdownV2 conversion (which would otherwise mangle
    ``*`` / ``**`` inside the quote body).
    """
    escaped = html.escape(transcript, quote=False)
    return f"🎙️\n<blockquote expandable>{escaped}</blockquote>"
