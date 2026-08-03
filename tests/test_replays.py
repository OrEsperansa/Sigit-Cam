from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.ffmpeg import ReplaySaveResult
from app.replays import ReplayCatalog


class ReplayCatalogTests(unittest.TestCase):
    def make_catalog(self, root: Path, backup: bool = False) -> ReplayCatalog:
        settings = replace(
            Settings(),
            data_dir=root,
            chunk_dir=root / "chunks",
            replay_dir=root / "replays",
            work_dir=root / "work",
            trash_dir=root / "trash",
            replay_backup_dir=root / "backup" if backup else None,
        )
        return ReplayCatalog(settings)

    def test_import_metadata_trash_restore_and_permanent_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.make_catalog(root)
            catalog.settings.replay_dir.mkdir(parents=True)
            media = catalog.settings.replay_dir / "replay_20260803_120000_000001.mp4"
            media.write_bytes(b"video")

            with patch("app.replays._probe_duration", return_value=12.5):
                self.assertEqual(catalog.import_existing(), 1)
            record = catalog.update_metadata(
                media.stem,
                {"title": "Winning goal", "tags": ["Goal", "goal", "Final"], "favorite": True},
            )
            self.assertEqual(record["tags"], ["Goal", "Final"])
            self.assertEqual(catalog.list_replays(query="winning")[0]["id"], media.stem)

            catalog.trash(media.stem)
            self.assertFalse(media.exists())
            self.assertEqual(catalog.list_trash()[0]["title"], "Winning goal")
            catalog.restore(media.stem)
            self.assertTrue(media.exists())
            self.assertEqual(catalog.get(media.stem)["trashed_at"], None)

            catalog.trash(media.stem)
            with self.assertRaisesRegex(ValueError, "exactly match"):
                catalog.permanently_delete(media.stem, "wrong")
            catalog.permanently_delete(media.stem, media.stem)
            self.assertEqual(catalog.list_trash(), [])

    def test_new_replay_backs_up_only_mp4_and_initial_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog = self.make_catalog(root, backup=True)
            catalog.settings.replay_dir.mkdir(parents=True)
            media = catalog.settings.replay_dir / "replay_20260803_120000_000002.mp4"
            media.write_bytes(b"validated-video")
            result = ReplaySaveResult(media, 30, 29.8, False, 4)

            record = catalog.register(result)
            status, error = catalog.backup_new_pair(str(record["id"]))
            self.assertEqual((status, error), ("complete", None))
            self.assertEqual((catalog.settings.replay_backup_dir / media.name).read_bytes(), b"validated-video")
            self.assertTrue((catalog.settings.replay_backup_dir / media.with_suffix(".json").name).is_file())


if __name__ == "__main__":
    unittest.main()
