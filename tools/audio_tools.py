"""Listen-to-audio tool: native envelope or STT fallback.

``audio_analyze`` loads a local audio (or video soundtrack) file so the
main model can hear it. When the active model supports native audio input
and the current prompt does not already carry a clip (Gemini 3.x Flash
accepts at most one audio file per request), the tool returns a
``_multimodal`` envelope with an OpenAI ``input_audio`` part.

Otherwise it transcribes via the existing STT pipeline (including
``stt.providers.*.type: command`` backends) and returns the transcript
as JSON. Video paths contribute a soundtrack extracted with ffmpeg;
silent clips fall through to a clear error rather than a second native
clip.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from tools.registry import registry, tool_error

logger = logging.getLogger(__name__)


AUDIO_ANALYZE_SCHEMA = {
    "name": "audio_analyze",
    "description": (
        "Load an audio file (or a video soundtrack) into the conversation "
        "so you can hear it. Accepts a local file path. When the active "
        "model supports native audio, the clip is attached to your context "
        "directly. Otherwise the file is transcribed to text. Use this when "
        "the user references an audio or video file whose sound matters. "
        "Do not use this for images."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "audio_url": {
                "type": "string",
                "description": (
                    "Local file path of an audio file, or a video file whose "
                    "soundtrack should be heard. http(s) URLs are not fetched."
                ),
            },
            "question": {
                "type": "string",
                "description": (
                    "Optional question or focus for the clip (language, "
                    "speaker, what to listen for)."
                ),
            },
        },
        "required": ["audio_url"],
    },
}


def _local_path_from_audio_url(audio_url: str) -> Optional[Path]:
    """Resolve a tool argument to a local filesystem path.

    data: URLs and remote http(s) are rejected — native audio is local-only
    so we never pull arbitrary network bytes into the prompt.
    """
    raw = (audio_url or "").strip()
    if not raw:
        return None
    if raw.startswith("data:"):
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"}:
        return None
    if parsed.scheme == "file":
        raw = parsed.path
        if raw.startswith("/") and len(raw) >= 3 and raw[2] == ":":
            # * Windows file:///C:/... → /C:/...
            raw = raw[1:]
    path = Path(raw).expanduser()
    try:
        path = path.resolve()
    except OSError:
        return None
    return path if path.is_file() else None


def _should_use_native_audio_fast_path() -> bool:
    """True when native audio routing is in effect and no clip is in-flight."""
    try:
        from agent.audio_routing import (
            decide_audio_input_mode,
            native_audio_in_flight,
        )
        from agent.auxiliary_client import _read_main_model, _read_main_provider
        from hermes_cli.config import load_config

        if native_audio_in_flight():
            return False
        provider = _read_main_provider()
        model = _read_main_model()
        cfg = load_config()
        return decide_audio_input_mode(provider, model, cfg) == "native"
    except Exception as exc:
        logger.debug("audio_analyze: native fast-path check failed: %s", exc)
        return False


def _build_native_audio_tool_result(
    audio_url: str,
    question: str,
    audio_part: Dict[str, Any],
    size_bytes: int,
    *,
    from_video: bool = False,
) -> Dict[str, Any]:
    """Build the multimodal tool-result envelope for the native fast path."""
    text_part = (
        "Audio loaded into your context — you can hear it natively now. "
        "Use your built-in audio understanding to answer the user."
    )
    if from_video:
        text_part += (
            " This clip is the soundtrack extracted from a video file; "
            "there is no native video attached."
        )
    if isinstance(question, str) and question.strip():
        text_part += f"\n\nQuestion: {question.strip()}"

    source = "video soundtrack" if from_video else "audio"
    summary = (
        f"{source.capitalize()} attached natively for the main model "
        f"({size_bytes / 1024:.1f} KB). Answer using built-in audio."
    )
    return {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": text_part},
            audio_part,
        ],
        "text_summary": summary,
        "meta": {
            "audio_url": audio_url[:200],
            "size_bytes": size_bytes,
            "native_audio": True,
            "from_video": from_video,
        },
    }


def _transcribe_fallback(
    path: Path,
    question: str,
    *,
    reason: str,
) -> str:
    """Run the existing STT pipeline and return a JSON tool result."""
    from tools.transcription_tools import transcribe_audio

    result = transcribe_audio(str(path))
    if not result.get("success"):
        err = result.get("error") or "transcription failed"
        return tool_error(
            f"Could not transcribe {path.name}: {err}",
            success=False,
        )
    transcript = str(result.get("transcript") or "").strip()
    payload: Dict[str, Any] = {
        "success": True,
        "native_audio": False,
        "reason": reason,
        "transcript": transcript,
        "provider": result.get("provider"),
        "path": str(path),
    }
    if question.strip():
        payload["question"] = question.strip()
    return json.dumps(payload)


def _audio_analyze_native(path: Path, question: str, audio_url: str) -> Any:
    """Attach the file as a native ``input_audio`` tool-result envelope."""
    from agent.audio_routing import (
        file_to_input_audio_part,
        mark_native_audio_in_flight,
        path_looks_like_video,
    )

    part = file_to_input_audio_part(path)
    if part is None:
        return _transcribe_fallback(
            path,
            question,
            reason="native_attach_failed",
        )
    payload = part.get("input_audio") or {}
    data = payload.get("data") or ""
    size_bytes = (len(data) * 3) // 4 if isinstance(data, str) else 0
    mark_native_audio_in_flight()
    return _build_native_audio_tool_result(
        audio_url,
        question,
        part,
        size_bytes,
        from_video=path_looks_like_video(path),
    )


def audio_analyze(
    audio_url: str,
    question: str = "",
    **_kw: Any,
) -> Any:
    """Listen to a local audio/video file natively or via STT.

    Returns a ``_multimodal`` envelope dict on the native fast path, or a
    JSON string (transcript / error) otherwise.
    """
    if not isinstance(audio_url, str) or not audio_url.strip():
        return tool_error("audio_url is required", success=False)

    path = _local_path_from_audio_url(audio_url)
    if path is None:
        return tool_error(
            "audio_url must be an existing local file path "
            "(http(s) and data: URLs are not fetched)",
            success=False,
        )

    q = question if isinstance(question, str) else ""
    if _should_use_native_audio_fast_path():
        logger.info("audio_analyze: native fast path for %s", path)
        return _audio_analyze_native(path, q, audio_url)

    from agent.audio_routing import native_audio_in_flight

    reason = (
        "native_audio_already_in_flight"
        if native_audio_in_flight()
        else "model_does_not_support_native_audio"
    )
    logger.info("audio_analyze: STT fallback (%s) for %s", reason, path)
    return _transcribe_fallback(path, q, reason=reason)


def _handle_audio_analyze(args: Dict[str, Any], **kw: Any) -> Any:
    return audio_analyze(
        audio_url=str(args.get("audio_url") or ""),
        question=str(args.get("question") or ""),
        **kw,
    )


registry.register(
    name="audio_analyze",
    toolset="audio",
    schema=AUDIO_ANALYZE_SCHEMA,
    handler=_handle_audio_analyze,
    emoji="🎧",
)
