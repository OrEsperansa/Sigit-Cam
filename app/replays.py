from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .backup import copy_replay_atomic
from .config import Settings
from .ffmpeg import ReplaySaveResult, require_ffmpeg_path


SCHEMA_VERSION = 1
REPLAY_ID_PATTERN = re.compile(r"^replay_[A-Za-z0-9_]+$")


class ReplayCatalog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()

    def update_settings(self, settings: Settings) -> None:
        self.settings = settings

    def import_existing(self) -> int:
        with self._lock:
            self._ensure_directories()
            imported = 0
            for media in sorted(self.settings.replay_dir.glob("replay_*.mp4")):
                sidecar = media.with_suffix(".json")
                if not sidecar.exists():
                    self._write_record(self._default_record(media))
                    imported += 1
                else:
                    self._load_record(media)
            return imported

    def list_replays(
        self,
        query: str = "",
        tag: str = "",
        favorite: bool | None = None,
    ) -> list[dict[str, object]]:
        with self._lock:
            self._ensure_directories()
            records = [self._load_record(path) for path in self.settings.replay_dir.glob("replay_*.mp4")]
            needle = query.strip().casefold()
            tag_needle = tag.strip().casefold()
            if needle:
                records = [
                    record for record in records
                    if needle in str(record.get("title", "")).casefold()
                    or needle in str(record.get("notes", "")).casefold()
                    or any(needle in str(item).casefold() for item in record.get("tags", []))
                ]
            if tag_needle:
                records = [
                    record for record in records
                    if any(tag_needle == str(item).casefold() for item in record.get("tags", []))
                ]
            if favorite is not None:
                records = [record for record in records if bool(record.get("favorite")) is favorite]
            records.sort(key=lambda item: str(item.get("created_at", "")), reverse=True)
            return [self._public_record(record) for record in records]

    def get(self, replay_id: str) -> dict[str, object]:
        with self._lock:
            media = self._media_path(replay_id)
            if not media.is_file():
                raise FileNotFoundError(replay_id)
            return self._public_record(self._load_record(media))

    def register(self, result: ReplaySaveResult) -> dict[str, object]:
        with self._lock:
            media = result.output
            stat = media.stat()
            created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            record: dict[str, object] = {
                "schema_version": SCHEMA_VERSION,
                "id": media.stem,
                "filename": media.name,
                "title": self._default_title(created),
                "notes": "",
                "tags": [],
                "favorite": False,
                "created_at": created,
                "updated_at": created,
                "duration_seconds": round(result.actual_seconds, 3),
                "bytes": stat.st_size,
                "requested_seconds": result.requested_seconds,
                "partial": result.partial,
                "reset_generation": result.reset_generation,
                "thumbnail": None,
                "backup_status": "pending" if self.settings.replay_backup_dir else "disabled",
                "backup_error": None,
                "trashed_at": None,
            }
            self._write_record(record)
            return self._public_record(record)

    def backup_new_pair(self, replay_id: str) -> tuple[str, str | None]:
        with self._lock:
            media = self._media_path(replay_id)
            record = self._load_record(media)
            if self.settings.replay_backup_dir is None:
                record["backup_status"] = "disabled"
                record["backup_error"] = None
                self._write_record(record)
                return "disabled", None
            try:
                copy_replay_atomic(self.settings, media)
                record["backup_status"] = "complete"
                record["backup_error"] = None
                self._write_record(record)
                copy_replay_atomic(self.settings, media.with_suffix(".json"))
                return "complete", None
            except OSError as exc:
                record["backup_status"] = "failed"
                record["backup_error"] = str(exc)
                self._write_record(record)
                return "failed", str(exc)

    def update_metadata(self, replay_id: str, payload: dict[str, object]) -> dict[str, object]:
        allowed = {"title", "notes", "tags", "favorite"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unknown metadata: {', '.join(sorted(unknown))}")
        with self._lock:
            media = self._media_path(replay_id)
            if not media.is_file():
                raise FileNotFoundError(replay_id)
            record = self._load_record(media)
            if "title" in payload:
                title = str(payload["title"]).strip()
                if not title or len(title) > 200:
                    raise ValueError("title must contain 1 to 200 characters")
                record["title"] = title
            if "notes" in payload:
                notes = str(payload["notes"]).strip()
                if len(notes) > 5000:
                    raise ValueError("notes cannot exceed 5000 characters")
                record["notes"] = notes
            if "tags" in payload:
                raw_tags = payload["tags"]
                if not isinstance(raw_tags, list):
                    raise ValueError("tags must be a list")
                tags: list[str] = []
                seen: set[str] = set()
                for raw_tag in raw_tags:
                    value = str(raw_tag).strip()
                    if not value:
                        continue
                    if len(value) > 40:
                        raise ValueError("tags cannot exceed 40 characters")
                    key = value.casefold()
                    if key not in seen:
                        tags.append(value)
                        seen.add(key)
                if len(tags) > 20:
                    raise ValueError("a replay cannot have more than 20 tags")
                record["tags"] = tags
            if "favorite" in payload:
                if not isinstance(payload["favorite"], bool):
                    raise ValueError("favorite must be true or false")
                record["favorite"] = payload["favorite"]
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_record(record)
            return self._public_record(record)

    def trash(self, replay_id: str) -> dict[str, object]:
        with self._lock:
            media = self._media_path(replay_id)
            if not media.is_file():
                raise FileNotFoundError(replay_id)
            record = self._load_record(media)
            destination = self._trash_item_path(replay_id)
            if destination.exists():
                raise FileExistsError(f"{replay_id} already exists in Trash")
            destination.mkdir(parents=True)
            record["trashed_at"] = datetime.now(timezone.utc).isoformat()
            self._write_record(record)
            moved: list[tuple[Path, Path]] = []
            try:
                for source in self._record_files(media):
                    if source.exists():
                        target = destination / source.name
                        os.replace(source, target)
                        moved.append((target, source))
            except Exception:
                for source, target in reversed(moved):
                    os.replace(source, target)
                shutil.rmtree(destination, ignore_errors=True)
                record["trashed_at"] = None
                self._write_record(record)
                raise
            return self._public_record(record, trashed=True)

    def list_trash(self) -> list[dict[str, object]]:
        with self._lock:
            self._ensure_directories()
            records: list[dict[str, object]] = []
            for directory in self.settings.trash_dir.iterdir():
                if not directory.is_dir() or not REPLAY_ID_PATTERN.fullmatch(directory.name):
                    continue
                media = directory / f"{directory.name}.mp4"
                if media.is_file():
                    records.append(self._public_record(self._load_record(media), trashed=True))
            records.sort(key=lambda item: str(item.get("trashed_at", "")), reverse=True)
            return records

    def restore(self, replay_id: str) -> dict[str, object]:
        with self._lock:
            directory = self._trash_item_path(replay_id)
            media = directory / f"{replay_id}.mp4"
            if not media.is_file():
                raise FileNotFoundError(replay_id)
            destination_media = self._media_path(replay_id)
            if destination_media.exists():
                raise FileExistsError(f"{replay_id} already exists in the library")
            record = self._load_record(media)
            record["trashed_at"] = None
            record["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._write_record(record, media.with_suffix(".json"))
            moved: list[tuple[Path, Path]] = []
            try:
                for source in self._record_files(media):
                    if source.exists():
                        target = self.settings.replay_dir / source.name
                        os.replace(source, target)
                        moved.append((target, source))
            except Exception:
                for source, target in reversed(moved):
                    os.replace(source, target)
                raise
            directory.rmdir()
            return self._public_record(record)

    def permanently_delete(self, replay_id: str, confirmation: str) -> None:
        if confirmation != replay_id:
            raise ValueError("confirmation must exactly match the replay id")
        with self._lock:
            directory = self._trash_item_path(replay_id)
            if not directory.is_dir():
                raise FileNotFoundError(replay_id)
            shutil.rmtree(directory)

    def storage_status(self) -> dict[str, object]:
        with self._lock:
            self._ensure_directories()
            replay_bytes = sum(path.stat().st_size for path in self.settings.replay_dir.glob("*.mp4"))
            trash_bytes = sum(path.stat().st_size for path in self.settings.trash_dir.rglob("*.mp4"))
            usage = shutil.disk_usage(self.settings.data_dir)
            free_percent = usage.free / usage.total * 100 if usage.total else 0.0
            if usage.free < 2 * 1024**3 or free_percent < 5:
                level = "critical"
            elif usage.free < 10 * 1024**3 or free_percent < 10:
                level = "warning"
            else:
                level = "ok"
            return {
                "replay_bytes": replay_bytes,
                "trash_bytes": trash_bytes,
                "free_bytes": usage.free,
                "total_bytes": usage.total,
                "free_percent": round(free_percent, 2),
                "level": level,
                "automatic_deletion": False,
            }

    def generate_missing_thumbnail(self) -> str | None:
        with self._lock:
            candidates = [
                (media, float(self._load_record(media).get("duration_seconds", 0) or 0))
                for media in sorted(
                    self.settings.replay_dir.glob("replay_*.mp4"),
                    key=lambda path: path.stat().st_mtime,
                )
                if not media.with_suffix(".jpg").exists()
            ]
        for media, duration in candidates:
            thumbnail = media.with_suffix(".jpg")
            if not _generate_thumbnail(media, thumbnail, duration):
                continue
            with self._lock:
                if not media.is_file():
                    thumbnail.unlink(missing_ok=True)
                    continue
                record = self._load_record(media)
                record["thumbnail"] = thumbnail.name
                self._write_record(record)
                return media.stem
        return None

    def _ensure_directories(self) -> None:
        self.settings.replay_dir.mkdir(parents=True, exist_ok=True)
        self.settings.trash_dir.mkdir(parents=True, exist_ok=True)

    def _media_path(self, replay_id: str) -> Path:
        self._validate_id(replay_id)
        return self.settings.replay_dir / f"{replay_id}.mp4"

    def _trash_item_path(self, replay_id: str) -> Path:
        self._validate_id(replay_id)
        return self.settings.trash_dir / replay_id

    @staticmethod
    def _validate_id(replay_id: str) -> None:
        if not REPLAY_ID_PATTERN.fullmatch(replay_id):
            raise ValueError("Invalid replay id")

    @staticmethod
    def _record_files(media: Path) -> tuple[Path, Path, Path]:
        return media, media.with_suffix(".json"), media.with_suffix(".jpg")

    def _load_record(self, media: Path) -> dict[str, object]:
        sidecar = media.with_suffix(".json")
        if sidecar.is_file():
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or data.get("id") != media.stem:
                    raise ValueError("sidecar identity mismatch")
                return self._refresh_file_fields(data, media)
            except (OSError, ValueError, json.JSONDecodeError):
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                corrupt = sidecar.with_name(f"{sidecar.stem}.corrupt.{stamp}.json")
                os.replace(sidecar, corrupt)
        record = self._default_record(media)
        self._write_record(record, sidecar)
        return record

    def _default_record(self, media: Path) -> dict[str, object]:
        stat = media.stat()
        created = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        duration = _probe_duration(media)
        return {
            "schema_version": SCHEMA_VERSION,
            "id": media.stem,
            "filename": media.name,
            "title": self._default_title(created),
            "notes": "",
            "tags": [],
            "favorite": False,
            "created_at": created,
            "updated_at": created,
            "duration_seconds": round(duration, 3),
            "bytes": stat.st_size,
            "requested_seconds": None,
            "partial": False,
            "reset_generation": None,
            "thumbnail": media.with_suffix(".jpg").name if media.with_suffix(".jpg").exists() else None,
            "backup_status": "unknown",
            "backup_error": None,
            "trashed_at": None,
        }

    @staticmethod
    def _default_title(created_at: str) -> str:
        return f"Replay {created_at[:19].replace('T', ' ')}"

    @staticmethod
    def _refresh_file_fields(record: dict[str, object], media: Path) -> dict[str, object]:
        record["schema_version"] = SCHEMA_VERSION
        record["id"] = media.stem
        record["filename"] = media.name
        record["bytes"] = media.stat().st_size
        thumbnail = media.with_suffix(".jpg")
        record["thumbnail"] = thumbnail.name if thumbnail.exists() else None
        return record

    def _write_record(self, record: dict[str, object], destination: Path | None = None) -> None:
        path = destination or self.settings.replay_dir / f"{record['id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _public_record(record: dict[str, object], trashed: bool = False) -> dict[str, object]:
        public = dict(record)
        filename = str(record["filename"])
        public["file"] = filename
        public["url"] = None if trashed else f"/replays/{filename}"
        public["thumbnail_url"] = (
            None if trashed or not record.get("thumbnail")
            else f"/thumbnails/{record['thumbnail']}"
        )
        try:
            public["modified"] = datetime.fromisoformat(str(record["updated_at"])).timestamp()
        except (ValueError, TypeError):
            public["modified"] = 0
        return public


def _probe_duration(media: Path) -> float:
    try:
        result = subprocess.run(
            [require_ffmpeg_path(), "-hide_banner", "-i", str(media)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not match:
        return 0.0
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _generate_thumbnail(media: Path, destination: Path, duration: float) -> bool:
    seek = min(max(duration / 2, 0.25), 10.0) if duration > 0 else 0.25
    temporary = destination.parent / f".{destination.stem}.{uuid4().hex}.partial.jpg"
    command = [
        require_ffmpeg_path(),
        "-hide_banner", "-loglevel", "error",
        "-ss", f"{seek:.3f}",
        "-i", str(media),
        "-frames:v", "1",
        "-vf", "scale=480:-2",
        "-q:v", "4",
        "-y", str(temporary),
    ]
    try:
        result = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False)
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            return False
        os.replace(temporary, destination)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False
    finally:
        temporary.unlink(missing_ok=True)
