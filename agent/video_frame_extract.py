"""Sample video frames as native images for the current user turn.

There is no native video path. When ``video.frame_extract.enabled`` is on
and inbound image routing is ``native``, a video file is sampled into stills
and those stills ride the existing ``image_url`` attach path.

Sampling is a command-provider (same shape as ``stt.providers.<name>``:
``video.providers.<name>: type: command``). Empty ``provider`` uses a
best-effort ffmpeg fallback. AskVLM is not hardcoded — operators point
``command:`` at ``external-extract-frames`` (or any CLI that writes frames
and prints a JSON manifest / path list).

Frame parts are tagged ``_hermes_ephemeral: video_frame`` so persist/live
strip treat them like native audio: the durable transcript keeps ``[video]``,
and a cached ``AIAgent`` does not re-send the stills on the next turn.
User-sent photos are untagged and stay in live memory.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


HERMES_EPHEMERAL_KEY = "_hermes_ephemeral"
EPHEMERAL_VIDEO_FRAME = "video_frame"
VIDEO_PERSIST_PLACEHOLDER = "[video]"

DEFAULT_FPS = 0.2
DEFAULT_FPS_FALLBACK = 0.2
DEFAULT_FRAME_BUDGET = 20
DEFAULT_MAX_FRAMES_PER_TURN = 20
DEFAULT_TIMEOUT_SECONDS = 180.0

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})


def _load_video_config() -> Dict[str, Any]:
    """Load the ``video`` section from user config."""
    try:
        from hermes_cli.config import load_config

        video = (load_config() or {}).get("video")
        return video if isinstance(video, dict) else {}
    except Exception:
        return {}


def _frame_extract_section(video_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if video_cfg is None:
        video_cfg = _load_video_config()
    if not isinstance(video_cfg, dict):
        return {}
    section = video_cfg.get("frame_extract")
    return section if isinstance(section, dict) else {}


def _is_truthy(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def frame_extract_is_enabled(video_cfg: Optional[Dict[str, Any]] = None) -> bool:
    """True when operators opted into inbound video frame sampling."""
    section = _frame_extract_section(video_cfg)
    return _is_truthy(section.get("enabled"), default=False)


def max_frames_per_turn(video_cfg: Optional[Dict[str, Any]] = None) -> int:
    """Hard cap on sampled stills attached on one user turn."""
    section = _frame_extract_section(video_cfg)
    raw = section.get("max_frames_per_turn", DEFAULT_MAX_FRAMES_PER_TURN)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_FRAMES_PER_TURN
    return value if value > 0 else DEFAULT_MAX_FRAMES_PER_TURN


def is_ephemeral_video_frame_part(part: Any) -> bool:
    """True if ``part`` is a sampled video still tagged for current-turn-only."""
    if not isinstance(part, dict):
        return False
    if str(part.get("type") or "") not in {"image_url", "input_image", "image"}:
        return False
    return part.get(HERMES_EPHEMERAL_KEY) == EPHEMERAL_VIDEO_FRAME


def tag_video_frame_parts(parts: Sequence[Any]) -> None:
    """Mark image parts in *parts* as ephemeral video stills (in place)."""
    for part in parts:
        if isinstance(part, dict) and part.get("type") in {"image_url", "input_image", "image"}:
            part[HERMES_EPHEMERAL_KEY] = EPHEMERAL_VIDEO_FRAME


def strip_ephemeral_metadata_from_content(content: Any) -> Any:
    """Return content with ``_hermes_ephemeral`` keys removed (copy on change).

    Used on the per-call API copy so providers never see the internal tag.
    Live in-memory messages keep the tag for persist / end-of-turn strip.
    """
    if not isinstance(content, list):
        return content
    if not any(
        isinstance(part, dict) and HERMES_EPHEMERAL_KEY in part for part in content
    ):
        return content
    cleaned: List[Any] = []
    for part in content:
        if isinstance(part, dict) and HERMES_EPHEMERAL_KEY in part:
            cleaned.append({k: v for k, v in part.items() if k != HERMES_EPHEMERAL_KEY})
        else:
            cleaned.append(part)
    return cleaned


def replace_ephemeral_video_frame_parts(
    messages: List[Any],
    placeholder: str = VIDEO_PERSIST_PLACEHOLDER,
) -> bool:
    """Replace tagged video-frame image parts with a text placeholder.

    User-sent photos (untagged ``image_url``) are left in place. Returns True
    if any ephemeral frame was removed.
    """
    found = False
    to_delete: List[int] = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if not any(is_ephemeral_video_frame_part(p) for p in content):
            continue
        found = True
        new_parts: List[Any] = []
        for part in content:
            if is_ephemeral_video_frame_part(part):
                new_parts.append({"type": "text", "text": placeholder})
            else:
                new_parts.append(part)
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
                    "[video frames removed — server does not support vision]"
                )
            else:
                to_delete.append(i)
        else:
            msg["content"] = new_parts
    for i in reversed(to_delete):
        del messages[i]
    return found


def _coerce_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _coerce_int(raw: Any, default: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _windows_hide_flags() -> int:
    try:
        from hermes_cli._subprocess_compat import windows_hide_flags

        return int(windows_hide_flags())
    except Exception:
        return 0


def _cache_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home

        dest = get_hermes_home() / "cache" / "video_frames"
    except Exception:
        dest = Path(tempfile.gettempdir()) / "hermes_video_frames"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _read_blocked(path: Path) -> Optional[str]:
    try:
        from agent.file_safety import raise_if_read_blocked

        raise_if_read_blocked(str(path))
    except ValueError as exc:
        return str(exc)
    except Exception:
        return None
    return None


def _provider_config(video_cfg: Dict[str, Any], name: str) -> Dict[str, Any]:
    providers = video_cfg.get("providers")
    if not isinstance(providers, dict):
        return {}
    section = providers.get(name)
    if isinstance(section, dict):
        return section
    # * Case-insensitive lookup (mirrors STT command providers).
    key = name.lower().strip()
    for raw_name, cfg in providers.items():
        if isinstance(raw_name, str) and raw_name.lower().strip() == key:
            return cfg if isinstance(cfg, dict) else {}
    return {}


def _is_command_provider(config: Dict[str, Any]) -> bool:
    if not isinstance(config, dict):
        return False
    ptype = str(config.get("type") or "").strip().lower()
    if ptype and ptype != "command":
        return False
    command = config.get("command")
    return isinstance(command, str) and bool(command.strip())


def _effective_fps(duration_s: float, fps: float, fps_fallback: float, budget: int) -> float:
    """AskVLM-shaped adaptive FPS: target → fallback → budget/duration."""
    if duration_s <= 0 or budget <= 0:
        return fps
    effective = fps
    if math.ceil(duration_s * fps) > budget:
        effective = fps_fallback
    if math.ceil(duration_s * effective) > budget:
        effective = budget / duration_s
    return effective


def _probe_duration_seconds(src: Path) -> float:
    """Best-effort duration via ffprobe; 0.0 when unavailable."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    cmd = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
            creationflags=_windows_hide_flags(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return 0.0
    if result.returncode != 0:
        return 0.0
    raw = (result.stdout or b"").decode("utf-8", errors="ignore").strip()
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    return value if value > 0 else 0.0


def _list_frame_files(output_dir: Path) -> List[str]:
    if not output_dir.is_dir():
        return []
    files = [
        p
        for p in output_dir.iterdir()
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES
    ]
    files.sort(key=lambda p: p.name)
    return [str(p) for p in files]


def _parse_command_stdout(stdout: str, output_dir: Path) -> List[str]:
    """Prefer a JSON manifest; fall back to one path per line; then the dir."""
    text = (stdout or "").strip()
    if text:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            frames = payload.get("frames")
            if isinstance(frames, list):
                paths = [str(p) for p in frames if str(p).strip()]
                existing = [p for p in paths if Path(p).is_file()]
                if existing:
                    return existing
        elif isinstance(payload, list):
            paths = [str(p) for p in payload if str(p).strip()]
            existing = [p for p in paths if Path(p).is_file()]
            if existing:
                return existing
        line_paths = []
        for line in text.splitlines():
            candidate = line.strip()
            if candidate and Path(candidate).is_file():
                line_paths.append(candidate)
        if line_paths:
            return line_paths
    return _list_frame_files(output_dir)


def _extract_frames_ffmpeg(
    src: Path,
    output_dir: Path,
    *,
    fps: float,
    fps_fallback: float,
    frame_budget: int,
    timeout: float,
) -> List[str]:
    if shutil.which("ffmpeg") is None:
        logger.info("video_frame_extract: ffmpeg not on PATH; skip %s", src)
        return []
    duration_s = _probe_duration_seconds(src)
    effective_fps = _effective_fps(duration_s, fps, fps_fallback, frame_budget)
    output_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(output_dir / "frame-%06d.png")
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(src),
        "-vf",
        f"fps={effective_fps}",
        pattern,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=max(timeout, 1.0),
            stdin=subprocess.DEVNULL,
            creationflags=_windows_hide_flags(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.info("video_frame_extract: ffmpeg failed for %s — %s", src, exc)
        return []
    if result.returncode != 0:
        stderr = (result.stderr or b"").decode("utf-8", errors="ignore")[:240]
        logger.info(
            "video_frame_extract: ffmpeg rc=%s for %s — %s",
            result.returncode, src, stderr,
        )
        return []
    frames = _list_frame_files(output_dir)
    if frame_budget > 0:
        frames = frames[:frame_budget]
    return frames


def _extract_frames_command(
    src: Path,
    output_dir: Path,
    *,
    provider_name: str,
    config: Dict[str, Any],
    fps: float,
    fps_fallback: float,
    frame_budget: int,
    timeout: float,
) -> List[str]:
    from tools.transcription_tools import (
        _command_stt_env_passthrough,
        _render_command_stt_template,
        _run_command_stt,
    )

    command_template = str(config.get("command") or "").strip()
    if not command_template:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    raw_timeout = config.get("timeout", config.get("timeout_seconds", timeout))
    try:
        cmd_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        cmd_timeout = timeout
    if cmd_timeout <= 0:
        cmd_timeout = timeout
    placeholders = {
        "input_path": str(src.resolve()),
        "output_dir": str(output_dir),
        "fps": str(fps),
        "fps_fallback": str(fps_fallback),
        "frame_budget": str(frame_budget),
        "timeout": str(cmd_timeout),
    }
    command = _render_command_stt_template(command_template, placeholders)
    logger.info(
        "Extracting frames from %s via command provider '%s'...",
        src.name, provider_name,
    )
    try:
        result = _run_command_stt(
            command,
            cmd_timeout,
            env_passthrough=_command_stt_env_passthrough(config),
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "video_frame_extract: command provider '%s' timed out on %s",
            provider_name, src,
        )
        return _list_frame_files(output_dir)[:frame_budget] if frame_budget else _list_frame_files(output_dir)
    if result.returncode != 0:
        stderr = (result.stderr or "")[:240]
        logger.info(
            "video_frame_extract: command provider '%s' rc=%s — %s",
            provider_name, result.returncode, stderr,
        )
        return _list_frame_files(output_dir)[:frame_budget] if frame_budget else _list_frame_files(output_dir)
    frames = _parse_command_stdout(result.stdout or "", output_dir)
    if frame_budget > 0:
        frames = frames[:frame_budget]
    return frames


def extract_video_frames(
    path: str,
    video_cfg: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Sample stills from *path*. Returns local image paths (possibly empty)."""
    src = Path(path).expanduser()
    blocked = _read_blocked(src)
    if blocked:
        logger.warning("video_frame_extract: blocked %s — %s", src, blocked)
        return []
    if not src.is_file():
        logger.info("video_frame_extract: missing file %s", src)
        return []
    if video_cfg is None:
        video_cfg = _load_video_config()
    if not frame_extract_is_enabled(video_cfg):
        return []
    section = _frame_extract_section(video_cfg)
    fps = _coerce_float(section.get("fps"), DEFAULT_FPS)
    fps_fallback = _coerce_float(section.get("fps_fallback"), DEFAULT_FPS_FALLBACK)
    frame_budget = _coerce_int(section.get("frame_budget"), DEFAULT_FRAME_BUDGET)
    timeout = _coerce_float(section.get("timeout"), DEFAULT_TIMEOUT_SECONDS)
    provider = str(section.get("provider") or "").strip()
    output_dir = _cache_dir() / uuid.uuid4().hex
    try:
        if provider:
            cfg = _provider_config(video_cfg, provider)
            if _is_command_provider(cfg):
                return _extract_frames_command(
                    src,
                    output_dir,
                    provider_name=provider,
                    config=cfg,
                    fps=fps,
                    fps_fallback=fps_fallback,
                    frame_budget=frame_budget,
                    timeout=timeout,
                )
            logger.warning(
                "video_frame_extract: provider %r is not a command type; "
                "falling back to ffmpeg",
                provider,
            )
        return _extract_frames_ffmpeg(
            src,
            output_dir,
            fps=fps,
            fps_fallback=fps_fallback,
            frame_budget=frame_budget,
            timeout=timeout,
        )
    except Exception:
        logger.warning("video_frame_extract: failed for %s", src, exc_info=True)
        return []


__all__ = [
    "EPHEMERAL_VIDEO_FRAME",
    "HERMES_EPHEMERAL_KEY",
    "VIDEO_PERSIST_PLACEHOLDER",
    "extract_video_frames",
    "frame_extract_is_enabled",
    "is_ephemeral_video_frame_part",
    "max_frames_per_turn",
    "replace_ephemeral_video_frame_parts",
    "strip_ephemeral_metadata_from_content",
    "tag_video_frame_parts",
]
