from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .config import Settings


LOGGER = logging.getLogger("instant_replay.backup")


@dataclass(frozen=True)
class BackupStatus:
    configured: bool
    path: str | None
    healthy: bool | None
    pending_count: int
    last_success: str | None
    last_error: str | None


def copy_replay_atomic(settings: Settings, source: Path) -> Path | None:
    backup_dir = settings.replay_backup_dir
    if backup_dir is None:
        return None

    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / source.name
    if destination.is_file() and destination.stat().st_size == source.stat().st_size:
        return destination

    temporary = backup_dir / f".{source.name}.{uuid4().hex}.partial"
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size != source.stat().st_size:
            raise OSError(f"Backup size verification failed for {source.name}")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


class BackupSynchronizer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        configured = settings.replay_backup_dir is not None
        self._status = BackupStatus(
            configured=configured,
            path=str(settings.replay_backup_dir) if configured else None,
            healthy=None if not configured else False,
            pending_count=0,
            last_success=None,
            last_error=None,
        )

    def status(self) -> dict[str, object]:
        return {
            "configured": self._status.configured,
            "path": self._status.path,
            "healthy": self._status.healthy,
            "pending_count": self._status.pending_count,
            "last_success": self._status.last_success,
            "last_error": self._status.last_error,
        }

    def sync_once(self) -> BackupStatus:
        backup_dir = self.settings.replay_backup_dir
        if backup_dir is None:
            return self._status

        local_files = sorted(self.settings.replay_dir.glob("replay_*.mp4"))
        pending = 0
        last_error: str | None = None
        copied_any = False
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            for source in local_files:
                destination = backup_dir / source.name
                try:
                    if destination.is_file() and destination.stat().st_size == source.stat().st_size:
                        continue
                    pending += 1
                    copy_replay_atomic(self.settings, source)
                    pending -= 1
                    copied_any = True
                except OSError as exc:
                    last_error = f"Failed to copy {source.name} to {backup_dir}: {exc}"
                    LOGGER.warning(last_error)
        except OSError as exc:
            pending = len(local_files)
            last_error = f"Cannot access replay backup directory {backup_dir}: {exc}"
            LOGGER.warning(last_error)

        last_success = self._status.last_success
        if copied_any or (not last_error and local_files):
            last_success = datetime.now(timezone.utc).isoformat()
        self._status = BackupStatus(
            configured=True,
            path=str(backup_dir),
            healthy=last_error is None,
            pending_count=pending,
            last_success=last_success,
            last_error=last_error,
        )
        return self._status