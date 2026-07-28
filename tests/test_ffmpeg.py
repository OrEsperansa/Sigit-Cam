from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.ffmpeg import (
    CaptureProcess,
    ChunkValidation,
    discover_ffmpeg_path,
    recent_chunks,
    save_replay,
    validated_completed_chunks,
)


class CaptureCommandTests(unittest.TestCase):
    def make_settings(self, root: Path, **overrides: object) -> Settings:
        values = {
            "data_dir": root,
            "chunk_dir": root / "chunks",
            "replay_dir": root / "replays",
            "video_resolution": "320x180",
            "fps": 10,
            "live_fps": 2,
            "live_width": 160,
            "chunk_seconds": 1,
            "video_codec": "libx264",
            "replay_finalize_wait_seconds": 0,
        }
        values.update(overrides)
        return replace(Settings(), **values)

    def test_default_capture_resolution_is_720p(self) -> None:
        self.assertEqual(Settings().video_resolution, "1280x720")

    def test_ffmpeg_6_command_uses_consistent_per_output_timing(self) -> None:
        settings = self.make_settings(Path("test-data"))
        capture = CaptureProcess(settings)
        capture.session_id = "session_a"
        with patch("app.ffmpeg.require_ffmpeg_path", return_value="ffmpeg"):
            command = capture._build_command(
                input_args=["-f", "lavfi", "-i", "testsrc2"],
                audio_map="0:a:0?",
            )

        self.assertNotIn("-vsync", command)
        self.assertNotIn("-r", command)
        self.assertNotIn("-strftime", command)
        self.assertIn("setpts=PTS-STARTPTS", command)
        self.assertTrue(any(
            item.startswith("aresample=async=1000:first_pts=0,atrim=start=0.120,asetpts=N/SR/TB,")
            for item in command
        ))
        self.assertTrue(any("astats=metadata=1:reset=1" in item for item in command))
        self.assertIn("+nobuffer+discardcorrupt", CaptureProcess(settings)._low_latency_input_args())
        modes = [command[index + 1] for index, item in enumerate(command) if item == "-fps_mode"]
        self.assertEqual(modes, ["passthrough", "cfr"])
        self.assertTrue(command[-1].endswith("chunk_session_a_%06d.mp4"))

    def test_audio_sync_offset_supports_advance_and_delay(self) -> None:
        root = Path("test-data")
        advanced = CaptureProcess(self.make_settings(root, audio_sync_offset_ms=-120))
        delayed = CaptureProcess(self.make_settings(root, audio_sync_offset_ms=80))
        neutral = CaptureProcess(self.make_settings(root, audio_sync_offset_ms=0))

        self.assertEqual(
            advanced._recording_audio_filter(),
            "aresample=async=1000:first_pts=0,atrim=start=0.120,asetpts=N/SR/TB,astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file='pipe\\:2'",
        )
        self.assertEqual(
            delayed._recording_audio_filter(),
            "aresample=async=1000:first_pts=0,adelay=80:all=1,asetpts=N/SR/TB,astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file='pipe\\:2'",
        )
        self.assertEqual(
            neutral._recording_audio_filter(),
            "aresample=async=1000:first_pts=0,asetpts=N/SR/TB,astats=metadata=1:reset=1,ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file='pipe\\:2'",
        )

    def test_audio_meter_parses_samples_and_silence(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data")))
        self.assertTrue(capture._consume_audio_meter_line("frame:0 pts:0"))
        self.assertTrue(capture._consume_audio_meter_line("lavfi.astats.Overall.Peak_level=-18.5"))
        self.assertEqual(capture.audio_peak_db, -18.5)
        self.assertIsNotNone(capture.audio_peak_at)
        self.assertTrue(capture._consume_audio_meter_line("lavfi.astats.Overall.Peak_level=-inf"))
        self.assertEqual(capture.audio_peak_db, -96.0)
        self.assertFalse(capture._consume_audio_meter_line("ordinary ffmpeg warning"))
    def test_session_prefixes_prevent_chunk_name_collisions(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = self.make_settings(root)
            settings.chunk_dir.mkdir()
            first = settings.chunk_dir / "chunk_first_000000.mp4"
            second = settings.chunk_dir / "chunk_second_000000.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            os.utime(first, (1000, 1000))
            os.utime(second, (1001, 1001))

            found = recent_chunks(settings, seconds=10**10)
            self.assertEqual(found, [first, second])

    def test_validation_includes_finalized_newest_and_skips_bad_closed_chunk(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = self.make_settings(root)
            settings.chunk_dir.mkdir()
            bad = settings.chunk_dir / "chunk_session_000000.mp4"
            newest = settings.chunk_dir / "chunk_session_000001.mp4"
            bad.write_bytes(b"bad")
            newest.write_bytes(b"good")
            os.utime(bad, (1000, 1000))
            os.utime(newest, (1001, 1001))

            def validation(_: Settings, path: Path) -> ChunkValidation:
                return ChunkValidation(path == newest, None if path == newest else "bad audio")

            with patch("app.ffmpeg.validate_chunk", side_effect=validation):
                chunks, skipped = asyncio.run(validated_completed_chunks(settings, 10**10))

            self.assertEqual(chunks, [newest])
            self.assertEqual(skipped, (bad.name,))

    def test_active_only_chunk_is_not_saved_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = self.make_settings(root)
            settings.chunk_dir.mkdir()
            active = settings.chunk_dir / "chunk_session_000000.mp4"
            active.write_bytes(b"still being written")

            with patch("app.ffmpeg.validate_chunk", return_value=ChunkValidation(False, "no moov")):
                chunks, skipped = asyncio.run(validated_completed_chunks(settings, 60))

            self.assertEqual(chunks, [])
            self.assertEqual(skipped, ())

    def test_bad_finalized_chunk_sets_warning_and_requests_restart(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = self.make_settings(root)
            settings.chunk_dir.mkdir()
            closed = settings.chunk_dir / "chunk_session_000000.mp4"
            active = settings.chunk_dir / "chunk_session_000001.mp4"
            closed.write_bytes(b"bad")
            active.write_bytes(b"active")
            now = time.time()
            os.utime(closed, (now - 1, now - 1))
            os.utime(active, (now, now))
            capture = CaptureProcess(settings)

            with patch("app.ffmpeg.validate_chunk", return_value=ChunkValidation(False, "bad audio")):
                healthy = asyncio.run(capture.check_finalized_chunks())

            self.assertFalse(healthy)
            self.assertEqual(capture.invalid_chunk_count, 1)
            self.assertIn(closed.name, capture.recording_warning or "")


class MockedFFmpegIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = discover_ffmpeg_path()
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is not installed")

    def run_mock_session(self, settings: Settings, session_id: str) -> list[Path]:
        capture = CaptureProcess(settings)
        capture.session_id = session_id
        command = capture._build_command(
            input_args=[
                "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=2.2",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=2.2",
            ],
            audio_map="1:a:0",
        )
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=20,
            check=False,
        )
        stderr = result.stderr.decode("utf-8", errors="replace")
        self.assertNotIn("Non-monotonous DTS", stderr)
        self.assertNotIn("Non-monotonic DTS", stderr)
        self.assertEqual(
            result.returncode,
            0,
            stderr,
        )
        return sorted(settings.chunk_dir.glob(f"chunk_{session_id}_*.mp4"))

    def test_mock_video_audio_channels_create_chunks_across_sessions(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            settings = CaptureCommandTests().make_settings(root)
            settings.chunk_dir.mkdir()

            first = self.run_mock_session(settings, "first")
            second = self.run_mock_session(settings, "second")

            self.assertGreaterEqual(len(first), 2)
            self.assertGreaterEqual(len(second), 2)
            self.assertTrue(set(first).isdisjoint(second))
            self.assertTrue(all(path.stat().st_size > 0 for path in first + second))
            self.assertEqual(recent_chunks(settings, seconds=60), first + second)
            replay = asyncio.run(save_replay(settings, seconds=60))
            self.assertTrue(replay.output.is_file())
            self.assertGreater(replay.output.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
