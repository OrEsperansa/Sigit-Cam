from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

from .config import Settings


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
