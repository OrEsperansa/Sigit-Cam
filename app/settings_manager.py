from __future__ import annotations

import asyncio
import os
import re
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from .config import BASE_DIR, Settings


MANAGED_FIELDS: dict[str, tuple[str, type]] = {
    "video_device": ("VIDEO_DEVICE", str),
    "audio_device": ("AUDIO_DEVICE", str),
    "video_resolution": ("VIDEO_RESOLUTION", str),
    "fps": ("FPS", int),
    "camera_rotation_degrees": ("CAMERA_ROTATION_DEGREES", float),
    "audio_sync_offset_ms": ("AUDIO_SYNC_OFFSET_MS", int),
    "live_fps": ("LIVE_FPS", int),
    "live_width": ("LIVE_WIDTH", int),
    "live_jpeg_quality": ("LIVE_JPEG_QUALITY", int),
    "dshow_rtbufsize": ("DSHOW_RTBUFSIZE", str),
    "replay_presets_seconds": ("REPLAY_PRESETS_SECONDS", tuple),
    "default_replay_seconds": ("DEFAULT_REPLAY_SECONDS", int),
    "max_buffer_minutes": ("MAX_BUFFER_MINUTES", int),
    "audio_startup_grace_seconds": ("AUDIO_STARTUP_GRACE_SECONDS", float),
    "audio_stall_seconds": ("AUDIO_STALL_SECONDS", float),
    "video_stall_seconds": ("VIDEO_STALL_SECONDS", float),
    "restart_max_backoff_seconds": ("RESTART_MAX_BACKOFF_SECONDS", float),
    "replay_backup_dir": ("REPLAY_BACKUP_DIR", Path),
}

CAPTURE_RESET_FIELDS = {
    "video_device",
    "audio_device",
    "video_resolution",
    "fps",
    "camera_rotation_degrees",
    "audio_sync_offset_ms",
    "live_fps",
    "live_width",
    "live_jpeg_quality",
    "dshow_rtbufsize",
}


class SettingsManager:
    def __init__(self, initial: Settings, env_path: Path | None = None) -> None:
        self.current = initial
        self.env_path = env_path or BASE_DIR / ".env"
        self.lock = asyncio.Lock()
        self.applying = False

    def public_values(self) -> dict[str, object]:
        values: dict[str, object] = {}
        for field_name in MANAGED_FIELDS:
            value = getattr(self.current, field_name)
            if isinstance(value, Path):
                values[field_name] = str(value)
            elif isinstance(value, tuple):
                values[field_name] = list(value)
            else:
                values[field_name] = value
        return values

    def candidate(self, payload: dict[str, object]) -> tuple[Settings, set[str]]:
        unknown = set(payload) - set(MANAGED_FIELDS)
        if unknown:
            raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")

        updates: dict[str, object] = {}
        for field_name, raw in payload.items():
            _, expected_type = MANAGED_FIELDS[field_name]
            if expected_type is tuple:
                if not isinstance(raw, list):
                    raise ValueError(f"{field_name} must be a list of seconds")
                updates[field_name] = tuple(sorted(set(int(value) for value in raw)))
            elif expected_type is Path:
                text = str(raw).strip() if raw is not None else ""
                updates[field_name] = Path(text) if text else None
            elif expected_type is str:
                updates[field_name] = str(raw).strip()
            elif expected_type is int:
                if isinstance(raw, bool):
                    raise ValueError(f"{field_name} must be an integer")
                updates[field_name] = int(raw)
            elif expected_type is float:
                if isinstance(raw, bool):
                    raise ValueError(f"{field_name} must be a number")
                updates[field_name] = float(raw)

        candidate = replace(self.current, **updates)
        self._validate(candidate)
        changed = {name for name, value in updates.items() if getattr(self.current, name) != value}
        return candidate, changed

    @staticmethod
    def _validate(candidate: Settings) -> None:
        candidate.validate_capture()
        if not re.fullmatch(r"[1-9][0-9]{1,4}x[1-9][0-9]{1,4}", candidate.video_resolution):
            raise ValueError("video_resolution must look like 1280x720")
        if abs(candidate.audio_sync_offset_ms) > 5000:
            raise ValueError("audio_sync_offset_ms must be between -5000 and 5000")
        if not re.fullmatch(r"[1-9][0-9]*[KMG]?", candidate.dshow_rtbufsize, re.IGNORECASE):
            raise ValueError("dshow_rtbufsize must look like 256M")

    def activate(self, candidate: Settings) -> None:
        self.current = candidate

    def persist(self, candidate: Settings) -> None:
        updates = {
            env_name: self._serialize(getattr(candidate, field_name))
            for field_name, (env_name, _) in MANAGED_FIELDS.items()
        }
        _rewrite_env_atomic(self.env_path, updates)

    @staticmethod
    def _serialize(value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, tuple):
            return ",".join(str(item) for item in value)
        return str(value)


def _rewrite_env_atomic(path: Path, updates: dict[str, str]) -> None:
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    newline = "\r\n" if "\r\n" in original else "\n"
    trailing_newline = original.endswith(("\n", "\r"))
    lines = original.splitlines()
    remaining = dict(updates)
    rewritten: list[str] = []
    managed_seen: set[str] = set()

    for line in lines:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            if key not in managed_seen:
                rewritten.append(f"{key}={remaining.pop(key)}")
                managed_seen.add(key)
        else:
            rewritten.append(line)
    if remaining and rewritten and rewritten[-1] != "":
        rewritten.append("")
    rewritten.extend(f"{key}={value}" for key, value in remaining.items())
    content = newline.join(rewritten)
    if trailing_newline or content:
        content += newline

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
