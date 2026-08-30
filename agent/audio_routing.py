"""Routing helpers for inbound user-attached audio.

Two modes:

  native  — attach audio as OpenAI-style ``input_audio`` content parts on the
            *current* user turn. Chat Completions (OpenRouter) pass these
            through; the Gemini native adapter translates them into
            ``inlineData``. Bytes are not written to the durable transcript.

  text    — keep the STT transcript and/or a path note only. The main model
            never hears the waveform.

The decision is made once per message turn by :func:`decide_audio_input_mode`.
It reads ``agent.audio_input_mode`` from config.yaml (``auto`` | ``native``
| ``text``, default ``auto``) and the active model's capability metadata.

In ``auto`` mode:
  - If the active model reports ``supports_audio=True`` (config override,
    models.dev ``modalities.input`` containing ``audio``, or the Gemini 3.5+
    slug heuristic), attach natively.
  - Otherwise fall back to text (existing STT / path-note pipeline).

Native audio is **current-turn-only**. Gemini 3.5/3.7 Flash accept at most
one audio file per prompt, and prompt-cache forbids mutating historical
audio parts. After the turn, live in-memory messages replace ``input_audio``
with an ``[audio]`` placeholder so a cached ``AIAgent`` does not re-send
the clip on the next user turn. Voice-note STT (and its echo) still run
alongside native attach — the transcript is the durable evidence.

There is no native video path. A video file may contribute a soundtrack
extracted with ffmpeg; silent clips are skipped. Sampled stills (when
``video.frame_extract`` is enabled) attach as ephemeral ``image_url``
parts — see ``agent.video_frame_extract``.
"""

from __future__ import annotations

import base64
import contextvars
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

from agent.image_routing import _coerce_mode, _supports_capability_override


_VALID_MODES = frozenset({"auto", "native", "text"})

AUDIO_PART_TYPES = frozenset({"input_audio", "audio"})
AUDIO_PERSIST_PLACEHOLDER = "[audio]"
MAX_NATIVE_AUDIO_BYTES = 10 * 1024 * 1024
MAX_NATIVE_AUDIO_CLIPS = 1

# OpenAI chat.completions ``input_audio.format`` values we emit after
# transcode. When ffmpeg is missing we may pass a sniffed container name
# through so OpenRouter/Gemini can still accept ogg/opus voice notes.
_OPENAI_AUDIO_FORMATS = frozenset({"mp3", "wav"})

_GEMINI_VERSION_RE = re.compile(
    r"(?:^|/)gemini[-_](\d+)(?:\.(\d+))?",
    re.IGNORECASE,
)

# Suffix → OpenAI format / Gemini mime.
_FORMAT_FROM_SUFFIX = {
    ".mp3": "mp3",
    ".mpga": "mp3",
    ".mpeg": "mp3",
    ".wav": "wav",
    ".wave": "wav",
    ".ogg": "ogg",
    ".oga": "ogg",
    ".opus": "ogg",
    ".m4a": "m4a",
    ".aac": "aac",
    ".flac": "flac",
    ".webm": "webm",
    ".mp4": "mp4",
}

_MIME_FROM_FORMAT = {
    "mp3": "audio/mp3",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "webm": "audio/webm",
    "mp4": "audio/mp4",
}

_VIDEO_SUFFIXES = frozenset({
    ".mp4", ".webm", ".mov", ".mkv", ".avi", ".mpeg", ".mpg", ".m4v",
})

# ContextVar: True while the current API request already carries a native
# audio clip (user-turn attach or a prior audio_analyze envelope). Gemini
# 3.x Flash rejects a second audio file on the same prompt.
_native_audio_in_flight: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hermes_native_audio_in_flight",
    default=False,
)


def mark_native_audio_in_flight() -> None:
    """Record that this request already carries one native audio clip."""
    _native_audio_in_flight.set(True)


def clear_native_audio_in_flight() -> None:
    """Clear the in-flight flag (end of API call / turn)."""
    _native_audio_in_flight.set(False)


def native_audio_in_flight() -> bool:
    """True when a native audio clip is already on the current prompt."""
    return bool(_native_audio_in_flight.get())


def gemini_slug_supports_native_audio(model: str) -> Optional[bool]:
    """Gemini 3.5+ heuristic for native audio input.

    models.dev caches lag new Gemini Flash drops. When the slug parses as
    Gemini 3.5 or newer, claim audio support even if the catalog is stale.
    Gemini 3.0–3.4 are explicit False. Other families (2.x, non-Gemini)
    return None so the caller falls through to models.dev / text mode.

    Args:
        model: Provider slug, optionally prefixed (``google/gemini-3.7-flash``).

    Returns:
        True, False, or None when the slug is not a Gemini 3.x claim.
    """
    slug = str(model or "").strip()
    if not slug:
        return None
    match = _GEMINI_VERSION_RE.search(slug)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    if major > 3:
        return True
    if major == 3 and minor >= 5:
        return True
    if major == 3:
        return False
    return None


def _supports_audio_override(
    cfg: Optional[Dict[str, Any]],
    provider: str,
    model: str,
    *,
    requested_provider: str = "",
) -> Optional[bool]:
    """Resolve user-declared audio capability from config.yaml."""
    return _supports_capability_override(
        cfg,
        provider,
        model,
        primary_key="supports_audio",
        alias_key="audio",
        requested_provider=requested_provider,
    )


def _lookup_supports_audio(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    requested_provider: str = "",
) -> Optional[bool]:
    """Return True/False if we can resolve audio caps, None if unknown.

    Order: config override → models.dev ``modalities.input`` → Gemini 3.5+
    slug heuristic (upgrades a stale False/missing catalog entry).
    """
    if not requested_provider:
        try:
            from agent.auxiliary_client import _runtime_main_value

            runtime_provider = str(
                _runtime_main_value("provider") or ""
            ).strip().lower()
            runtime_model = str(_runtime_main_value("model") or "").strip()
            lookup_provider = str(provider or "").strip().lower()
            lookup_model = str(model or "").strip()
            if runtime_provider == lookup_provider and runtime_model == lookup_model:
                requested_provider = str(
                    _runtime_main_value("requested_provider") or ""
                ).strip()
        except Exception:
            pass

    override = _supports_audio_override(
        cfg,
        provider,
        model,
        requested_provider=requested_provider,
    )
    if override is not None:
        return override

    caps = None
    try:
        from agent.models_dev import get_model_capabilities

        if provider and model:
            caps = get_model_capabilities(provider, model)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug(
            "audio_routing: caps lookup failed for %s:%s — %s",
            provider, model, exc,
        )

    heuristic = gemini_slug_supports_native_audio(model)
    if caps is not None and getattr(caps, "supports_audio", False):
        return True
    # * Stale models.dev: Gemini 3.5+ may not list audio yet.
    if heuristic is True:
        return True
    if caps is not None:
        return bool(getattr(caps, "supports_audio", False))
    if heuristic is False:
        return False
    return None


def decide_audio_input_mode(
    provider: str,
    model: str,
    cfg: Optional[Dict[str, Any]],
    *,
    requested_provider: str = "",
) -> str:
    """Return ``"native"`` or ``"text"`` for the given turn.

    Args:
        provider: Active inference provider ID (e.g. ``"openrouter"``).
        model: Active model slug as it would be sent to the provider.
        cfg: Loaded config.yaml dict, or None. When None, behaves as auto.
        requested_provider: Provider identity before runtime canonicalization.
    """
    mode_cfg = "auto"
    if isinstance(cfg, dict):
        agent_cfg = cfg.get("agent") or {}
        if isinstance(agent_cfg, dict):
            mode_cfg = _coerce_mode(agent_cfg.get("audio_input_mode"))
            if mode_cfg not in _VALID_MODES:
                mode_cfg = "auto"

    if mode_cfg == "native":
        return "native"
    if mode_cfg == "text":
        return "text"

    if requested_provider:
        supports = _lookup_supports_audio(
            provider,
            model,
            cfg,
            requested_provider=requested_provider,
        )
    else:
        # Keep the three-argument call contract so tests can replace the hook.
        supports = _lookup_supports_audio(provider, model, cfg)
    if supports is True:
        return "native"
    return "text"


def is_audio_part(part: Any) -> bool:
    """True if ``part`` is an OpenAI-style native audio content block."""
    if not isinstance(part, dict):
        return False
    return str(part.get("type") or "") in AUDIO_PART_TYPES


def content_has_audio_parts(content: Any) -> bool:
    """True if message content is a list that includes native audio parts."""
    if not isinstance(content, list):
        return False
    return any(is_audio_part(p) for p in content)


def count_audio_parts(content: Any) -> int:
    """Count native audio parts in a message content value."""
    if not isinstance(content, list):
        return 0
    return sum(1 for p in content if is_audio_part(p))


def messages_have_audio_parts(messages: Sequence[Any]) -> bool:
    """True if any message in ``messages`` carries native audio parts."""
    for msg in messages:
        if isinstance(msg, dict) and content_has_audio_parts(msg.get("content")):
            return True
    return False


# * Best-effort English phrases from OpenRouter / Gemini / OpenAI-compatible
#   4xx bodies. Locale-translated or heavily reworded errors skip this guard
#   and fall through to the normal conversation-loop retry path.
_AUDIO_REJECTION_PHRASES = (
    "does not support audio",
    "audio input is not supported",
    "no endpoints found that support audio",
    "only one audio file",
    "maximum number of audio files",
    "too many audio files",
)


def looks_like_audio_content_rejection(error_body: Any) -> bool:
    """Return True when a provider error says native audio input is unsupported.

    Args:
        error_body: Raw exception body, message, or ``str(error)``.

    Returns:
        True if any known audio-rejection phrase appears in the lowercased body.
    """
    body = str(error_body or "").lower()
    return any(phrase in body for phrase in _AUDIO_REJECTION_PHRASES)


def replace_audio_parts_with_placeholder(
    messages: List[Any],
    placeholder: str = AUDIO_PERSIST_PLACEHOLDER,
) -> bool:
    """Replace native audio parts in-place with a text placeholder.

    Used at end-of-turn (so a cached agent does not re-send historical
    audio) and on provider rejection (so the retry is text-only).

    Tool-role messages whose content was entirely audio keep a placeholder
    string so ``tool_call_id`` pairing stays intact. Non-tool messages
    whose content becomes empty are dropped.

    Returns:
        True if any audio part was removed.
    """
    found = False
    to_delete: List[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        new_parts: List[Any] = []
        for part in content:
            if is_audio_part(part):
                found = True
                new_parts.append({"type": "text", "text": placeholder})
            else:
                new_parts.append(part)
        if len(new_parts) == len(content) and not any(
            is_audio_part(p) for p in content
        ):
            continue
        # Collapse adjacent placeholder-only lists into a single text blob
        # when no other structured parts remain.
        text_only = [
            str(p.get("text", "")) if isinstance(p, dict) and p.get("type") == "text"
            else None
            for p in new_parts
        ]
        if new_parts and all(t is not None for t in text_only):
            merged = "\n".join(t for t in text_only if t).strip()
            if merged:
                msg["content"] = merged
            elif msg.get("role") == "tool":
                msg["content"] = (
                    "[audio content removed — server does not support audio]"
                )
            else:
                to_delete.append(i)
        elif new_parts:
            msg["content"] = new_parts
        elif msg.get("role") == "tool":
            msg["content"] = (
                "[audio content removed — server does not support audio]"
            )
        else:
            to_delete.append(i)
    for i in reversed(to_delete):
        del messages[i]
    return found


def flatten_audio_parts_for_persist(content: Any) -> Optional[str]:
    """Flatten OpenAI-style parts to persistable text, tagging audio.

    Image parts become ``[screenshot]`` (same contract as the session-DB
    flush). Audio parts become ``[audio]``. Returns None when the list
    had no text/image/audio parts at all.
    """
    if not isinstance(content, list):
        return content if isinstance(content, str) else None
    chunks: List[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            chunks.append(str(part.get("text", "")))
        elif ptype in {"image", "image_url", "input_image"}:
            if part.get("_hermes_ephemeral") == "video_frame":
                chunks.append("[video]")
            else:
                chunks.append("[screenshot]")
        elif ptype in AUDIO_PART_TYPES:
            chunks.append(AUDIO_PERSIST_PLACEHOLDER)
    return "\n".join(chunks) if chunks else None


def _has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def _windows_hide_flags() -> int:
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        return int(windows_hide_flags())
    except Exception:
        return 0


def _cache_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        dest = get_hermes_home() / "cache" / "native_audio"
    except Exception:
        dest = Path(tempfile.gettempdir()) / "hermes_native_audio"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _sniff_format(path: Path, raw: Optional[bytes] = None) -> Optional[str]:
    """Return an OpenAI-ish format id from magic bytes, then suffix."""
    data = raw
    if data is None:
        try:
            data = path.read_bytes()[:16]
        except Exception:
            data = b""
    if data:
        try:
            from tools.audio_container import sniff_container

            sniffed = sniff_container(data)
            if sniffed == "mp3":
                return "mp3"
            if sniffed == "wav":
                return "wav"
            if sniffed in {"ogg"}:
                return "ogg"
            if sniffed == "m4a":
                return "m4a"
            if sniffed == "flac":
                return "flac"
            if sniffed == "webm":
                return "webm"
            if sniffed == "aac":
                return "aac"
            if sniffed == "mp4":
                return "mp4"
        except Exception:
            pass
    return _FORMAT_FROM_SUFFIX.get(path.suffix.lower())


def _ffmpeg_to_mp3(src: Path, dst: Path) -> bool:
    """Extract the first audio stream to MP3. Returns False on failure."""
    if not _has_ffmpeg():
        return False
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-map",
        "0:a:0",
        "-acodec",
        "libmp3lame",
        "-b:a",
        "96k",
        "-ar",
        "16000",
        "-ac",
        "1",
        str(dst),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=_windows_hide_flags(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.info("audio_routing: ffmpeg failed for %s — %s", src, exc)
        return False
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="ignore")[:240]
        logger.info(
            "audio_routing: ffmpeg rc=%s for %s — %s",
            result.returncode, src, stderr,
        )
        return False
    try:
        return dst.is_file() and dst.stat().st_size > 0
    except OSError:
        return False


def extract_soundtrack_mp3(src: Path) -> Optional[Path]:
    """Extract a video (or audio) file's first audio stream to a temp MP3.

    Returns None when ffmpeg is missing, the file has no audio stream, or
    transcode fails. Silent GIFs/videos therefore never become a native
    audio clip.
    """
    if not src.is_file():
        return None
    fd, name = tempfile.mkstemp(suffix=".mp3", dir=str(_cache_dir()))
    os.close(fd)
    dst = Path(name)
    if _ffmpeg_to_mp3(src, dst):
        return dst
    try:
        dst.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def path_looks_like_video(path: Path) -> bool:
    """True when the suffix is a common video container (not a voice note)."""
    return path.suffix.lower() in _VIDEO_SUFFIXES


def _read_blocked(path: Path) -> Optional[str]:
    try:
        from agent.file_safety import raise_if_read_blocked

        raise_if_read_blocked(str(path))
    except ValueError as exc:
        return str(exc)
    except Exception:
        return None
    return None


def file_to_input_audio_part(path: Path) -> Optional[Dict[str, Any]]:
    """Read a local audio/video file into an OpenAI ``input_audio`` part.

    Prefers MP3 via ffmpeg so OpenRouter and Gemini share one wire format.
    Already-small MP3/WAV files pass through without transcode. Video
    containers contribute a soundtrack only. Returns None when the file
    cannot be read, exceeds :data:`MAX_NATIVE_AUDIO_BYTES`, or has no
    audio stream.
    """
    blocked = _read_blocked(path)
    if blocked:
        logger.warning("audio_routing: blocked local audio %s — %s", path, blocked)
        return None
    if not path.is_file():
        return None

    work_path = path
    cleanup: Optional[Path] = None
    sniffed = _sniff_format(path)
    needs_extract = path_looks_like_video(path) or sniffed not in _OPENAI_AUDIO_FORMATS
    if needs_extract:
        extracted = extract_soundtrack_mp3(path)
        if extracted is None:
            if sniffed in _OPENAI_AUDIO_FORMATS or sniffed in {"ogg", "m4a", "webm", "flac"}:
                work_path = path
            else:
                logger.info(
                    "audio_routing: no soundtrack from %s (silent or ffmpeg missing)",
                    path,
                )
                return None
        else:
            work_path = extracted
            cleanup = extracted
            sniffed = "mp3"

    try:
        raw = work_path.read_bytes()
    except Exception as exc:
        logger.warning("audio_routing: failed to read %s — %s", work_path, exc)
        return None
    finally:
        if cleanup is not None and cleanup != path:
            try:
                cleanup.unlink(missing_ok=True)
            except OSError:
                pass

    if not raw:
        return None
    if len(raw) > MAX_NATIVE_AUDIO_BYTES:
        logger.warning(
            "audio_routing: %s is %d bytes (cap %d); skipping native attach",
            path, len(raw), MAX_NATIVE_AUDIO_BYTES,
        )
        return None

    fmt = sniffed or _sniff_format(work_path, raw) or "mp3"
    if fmt not in _OPENAI_AUDIO_FORMATS:
        # OpenRouter/Gemini often still accept ogg/opus; keep the sniffed id.
        fmt = fmt if fmt else "mp3"

    return {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(raw).decode("ascii"),
            "format": fmt,
        },
    }


def gemini_inline_audio_from_part(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate an OpenAI ``input_audio`` part into Gemini ``inlineData``."""
    if not is_audio_part(part):
        return None
    payload = part.get("input_audio") or part.get("audio") or {}
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, str) or not data:
        return None
    fmt = str(payload.get("format") or "mp3").strip().lower() or "mp3"
    mime = _MIME_FROM_FORMAT.get(fmt, f"audio/{fmt}")
    return {
        "inlineData": {
            "mimeType": mime,
            "data": data,
        }
    }


def build_native_audio_parts(
    user_text: str,
    audio_paths: Sequence[str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Build an OpenAI-style content list with at most one native audio clip.

    Extra clips beyond :data:`MAX_NATIVE_AUDIO_CLIPS` are skipped (their
    STT / path notes already live in ``user_text``). Returns
    ``(parts, skipped)``.
    """
    skipped: List[str] = []
    audio_parts: List[Dict[str, Any]] = []
    attached: List[str] = []

    for raw_path in audio_paths:
        if len(audio_parts) >= MAX_NATIVE_AUDIO_CLIPS:
            skipped.append(str(raw_path))
            continue
        p = Path(raw_path)
        part = file_to_input_audio_part(p)
        if part is None:
            skipped.append(str(raw_path))
            continue
        audio_parts.append(part)
        attached.append(str(raw_path))

    text = (user_text or "").strip()
    if attached:
        base_text = text or "The user sent audio."
        hints = "\n".join(f"[Audio attached at: {p}]" for p in attached)
        combined = f"{base_text}\n\n{hints}"
        parts: List[Dict[str, Any]] = [{"type": "text", "text": combined}]
        parts.extend(audio_parts)
        return parts, skipped

    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    return parts, skipped


def append_native_audio_parts(
    content: Any,
    audio_paths: Sequence[str],
) -> Tuple[Any, List[str]]:
    """Append at most one native audio clip onto existing message content.

    ``content`` may be a string or an OpenAI-style parts list (e.g. already
    carrying native images). Extra clips are skipped. Returns
    ``(new_content, skipped)``.
    """
    if not audio_paths:
        return content, []

    existing_audio = count_audio_parts(content)
    remaining = max(0, MAX_NATIVE_AUDIO_CLIPS - existing_audio)
    if remaining <= 0:
        return content, [str(p) for p in audio_paths]

    skipped: List[str] = []
    attached: List[str] = []
    audio_parts: List[Dict[str, Any]] = []
    for raw_path in audio_paths:
        if len(audio_parts) >= remaining:
            skipped.append(str(raw_path))
            continue
        part = file_to_input_audio_part(Path(raw_path))
        if part is None:
            skipped.append(str(raw_path))
            continue
        audio_parts.append(part)
        attached.append(str(raw_path))

    if not audio_parts:
        return content, skipped

    if isinstance(content, list):
        parts = list(content)
    elif isinstance(content, str) and content.strip():
        parts = [{"type": "text", "text": content.strip()}]
    else:
        parts = [{"type": "text", "text": "The user sent audio."}]

    # Append path hints onto the first text part when present.
    hint_block = "\n".join(f"[Audio attached at: {p}]" for p in attached)
    if parts and isinstance(parts[0], dict) and parts[0].get("type") == "text":
        existing_text = str(parts[0].get("text") or "").rstrip()
        parts[0] = {
            "type": "text",
            "text": f"{existing_text}\n\n{hint_block}" if existing_text else hint_block,
        }
    else:
        parts.insert(0, {"type": "text", "text": hint_block})
    parts.extend(audio_parts)
    return parts, skipped


def _append_image_paths(
    content: Any,
    image_paths: Sequence[str],
    *,
    ephemeral_tag: Optional[str] = None,
) -> Tuple[Any, List[str]]:
    """Attach local images onto *content*, optionally tagging them ephemeral."""
    from agent.image_routing import build_native_content_parts

    existing_images: List[Dict[str, Any]] = []
    existing_audio: List[Dict[str, Any]] = []
    base_text = content if isinstance(content, str) else ""
    if isinstance(content, list):
        for part in content:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type")
            if ptype == "text" and not base_text:
                base_text = str(part.get("text") or "")
            elif ptype == "image_url":
                existing_images.append(part)
            elif ptype in AUDIO_PART_TYPES:
                existing_audio.append(part)
    new_parts, skipped = build_native_content_parts(
        base_text if isinstance(base_text, str) else "",
        list(image_paths),
    )
    new_images = [
        p for p in new_parts
        if isinstance(p, dict) and p.get("type") == "image_url"
    ]
    if ephemeral_tag:
        for part in new_images:
            part["_hermes_ephemeral"] = ephemeral_tag
    if not new_images and not existing_images:
        return content, skipped
    new_text = base_text
    for part in new_parts:
        if isinstance(part, dict) and part.get("type") == "text":
            new_text = str(part.get("text") or "")
            break
    parts: List[Dict[str, Any]] = [{"type": "text", "text": new_text or "The user sent media."}]
    parts.extend(existing_images)
    parts.extend(new_images)
    parts.extend(existing_audio)
    return parts, skipped


def merge_native_media_parts(
    user_text: str,
    image_paths: Optional[Sequence[str]] = None,
    audio_paths: Optional[Sequence[str]] = None,
    video_frame_paths: Optional[Sequence[str]] = None,
) -> Tuple[Any, List[str], List[str]]:
    """Build a user-turn payload with native images, video stills, and audio.

    Video stills are tagged ``_hermes_ephemeral: video_frame`` so persist and
    end-of-turn strip treat them like native audio. User photos stay untagged.

    Returns ``(content, skipped_images, skipped_audio)``. ``content`` is a
    parts list when any media attached, otherwise the original text string.
    """
    skipped_images: List[str] = []
    content: Any = user_text
    if image_paths:
        content, skipped_images = _append_image_paths(content, image_paths)
    if video_frame_paths:
        content, skipped_frames = _append_image_paths(
            content, video_frame_paths, ephemeral_tag="video_frame",
        )
        skipped_images.extend(skipped_frames)

    skipped_audio: List[str] = []
    if audio_paths:
        content, skipped_audio = append_native_audio_parts(content, audio_paths)

    if isinstance(content, list) and not any(
        isinstance(p, dict) and p.get("type") in {"image_url", "input_audio", "audio"}
        for p in content
    ):
        # Degenerate: parts list with only text — keep a string so callers
        # that special-case list-vs-str still see a plain turn.
        text_bits = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        merged = "\n".join(t for t in text_bits if t).strip()
        return merged or user_text, skipped_images, skipped_audio
    return content, skipped_images, skipped_audio


__all__ = [
    "AUDIO_PART_TYPES",
    "AUDIO_PERSIST_PLACEHOLDER",
    "MAX_NATIVE_AUDIO_CLIPS",
    "append_native_audio_parts",
    "build_native_audio_parts",
    "clear_native_audio_in_flight",
    "content_has_audio_parts",
    "count_audio_parts",
    "decide_audio_input_mode",
    "extract_soundtrack_mp3",
    "file_to_input_audio_part",
    "flatten_audio_parts_for_persist",
    "gemini_inline_audio_from_part",
    "gemini_slug_supports_native_audio",
    "is_audio_part",
    "looks_like_audio_content_rejection",
    "mark_native_audio_in_flight",
    "merge_native_media_parts",
    "messages_have_audio_parts",
    "native_audio_in_flight",
    "path_looks_like_video",
    "replace_audio_parts_with_placeholder",
]
