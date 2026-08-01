from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from time import monotonic
from unittest.mock import AsyncMock, patch

from app.config import Settings
from app.ffmpeg import CaptureProcess, discover_ffmpeg_path, save_replay, validate_media


class CaptureTests(unittest.TestCase):
    def make_settings(self, root: Path, **overrides: object) -> Settings:
        values = {
            "data_dir": root,
            "chunk_dir": root / "chunks",
            "replay_dir": root / "replays",
            "work_dir": root / "work",
            "video_device": "Test Camera",
            "audio_device": "Test Microphone",
            "video_resolution": "320x180",
            "fps": 10,
            "live_fps": 2,
            "live_width": 160,
            "chunk_seconds": 1,
            "audio_startup_grace_seconds": 10.0,
            "audio_stall_seconds": 3.0,
        }
        values.update(overrides)
        return replace(Settings(), **values)

    def test_dshow_command_uses_one_combined_exact_device_input(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data")))
        capture.session_id = "g000001_test"
        with patch("app.ffmpeg.require_ffmpeg_path", return_value="ffmpeg"):
            command = capture._build_command()

        source = command[command.index("-i") + 1]
        self.assertEqual(source, "video=Test Camera:audio=Test Microphone")
        self.assertEqual(command.count("-i"), 1)
        self.assertNotIn("-fflags", command)
        self.assertNotIn("-reset_timestamps", command)

    def test_recording_is_closed_gop_libx264_flushed_mpegts(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data"), fps=30, chunk_seconds=2))
        capture.session_id = "g000001_test"
        with patch("app.ffmpeg.require_ffmpeg_path", return_value="ffmpeg"):
            command = capture._build_command(
                input_args=["-f", "lavfi", "-i", "testsrc2"],
                audio_map="0:a:0",
            )

        self.assertIn("libx264", command)
        self.assertEqual(command[command.index("-g") + 1], "60")
        self.assertIn("+cgop", command)
        self.assertIn("mpegts", command)
        self.assertIn("mpegts_flags=+resend_headers", command)
        self.assertEqual(command[command.index("-flush_packets") + 1], "1")
        self.assertTrue(command[-1].endswith(".ts"))

    def test_audio_meter_treats_digital_silence_as_fresh_audio(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data")))
        self.assertTrue(capture._consume_audio_meter_line("[Parsed_ametadata_3 @ 01] frame:0 pts:0"))
        self.assertTrue(capture._consume_audio_meter_line("lavfi.astats.Overall.Peak_level=-inf"))
        self.assertEqual(capture.audio_peak_db, -96.0)
        self.assertIsNotNone(capture.last_audio_frame_at)

    def test_watchdog_resets_for_missing_audio_but_not_silence(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data")))
        capture.started_at = monotonic() - 20
        capture.last_video_frame_at = monotonic()
        capture.last_chunk_progress_at = monotonic()
        with patch.object(capture, "is_running", return_value=True):
            self.assertIn("no audio frames", capture.health_problem() or "")
            capture._consume_audio_meter_line("lavfi.astats.Overall.Peak_level=-inf")
            self.assertIsNone(capture.health_problem())

    def test_reset_removes_chunks_and_abandoned_work_but_preserves_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.make_settings(root)
            settings.chunk_dir.mkdir()
            settings.work_dir.mkdir()
            settings.replay_dir.mkdir()
            (settings.chunk_dir / "chunk_old_000000.ts").write_bytes(b"old")
            abandoned = settings.work_dir / "snapshot_abandoned"
            abandoned.mkdir()
            (abandoned / "piece.ts").write_bytes(b"old")
            replay = settings.replay_dir / "replay_saved.mp4"
            replay.write_bytes(b"keep")
            capture = CaptureProcess(settings)
            capture._spawn_process = AsyncMock()  # type: ignore[method-assign]

            asyncio.run(capture.restart("test reset"))

            self.assertEqual(list(settings.chunk_dir.iterdir()), [])
            self.assertEqual(list(settings.work_dir.iterdir()), [])
            self.assertEqual(replay.read_bytes(), b"keep")
            self.assertEqual(capture.generation, 1)

    def test_snapshot_hardlink_fallback_and_packet_aligned_active_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = self.make_settings(root, chunk_seconds=2)
            settings.chunk_dir.mkdir()
            capture = CaptureProcess(settings)
            capture.generation = 3
            capture.session_id = "g000003_test"
            capture.started_at = monotonic() - 5
            closed = settings.chunk_dir / "chunk_g000003_test_000000.ts"
            active = settings.chunk_dir / "chunk_g000003_test_000001.ts"
            closed.write_bytes(b"A" * 376)
            active.write_bytes(b"B" * 381)

            async def snapshot_test() -> None:
                with patch("app.ffmpeg.os.link", side_effect=OSError("cross-device")):
                    snapshot = await capture.snapshot_buffer(4)
                try:
                    self.assertEqual(len(snapshot.chunks), 2)
                    self.assertEqual(snapshot.chunks[0].read_bytes(), closed.read_bytes())
                    self.assertEqual(snapshot.chunks[1].stat().st_size, 376)
                    self.assertEqual(snapshot.chunks[1].stat().st_size % 188, 0)
                finally:
                    await capture.release_snapshot(snapshot)

            asyncio.run(snapshot_test())

    def test_recoverable_mjpeg_messages_are_classified_without_reset(self) -> None:
        capture = CaptureProcess(self.make_settings(Path("test-data")))
        self.assertTrue(capture._is_corrupt_frame_message("error dc"))
        self.assertTrue(capture._is_corrupt_frame_message("error y=8 x=20"))
        self.assertTrue(capture._is_benign_pixel_format_message(
            "deprecated pixel format used, make sure you did set range correctly"
        ))
        self.assertFalse(capture._is_corrupt_frame_message("Error opening output file"))


class SyntheticFFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ffmpeg = discover_ffmpeg_path()
        if not cls.ffmpeg:
            raise unittest.SkipTest("FFmpeg is not installed")

    def test_non_boundary_active_tail_saves_h264_aac_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = CaptureTests().make_settings(root, chunk_seconds=1)
            settings.chunk_dir.mkdir()
            capture = CaptureProcess(settings)
            capture.generation = 1
            capture.session_id = "g000001_synthetic"
            capture.started_at = monotonic() - 6
            command = capture._build_command(
                input_args=[
                    "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=10:duration=5.3",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=5.3",
                ],
                audio_map="1:a:0",
            )
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                completed.stderr.decode("utf-8", errors="replace")[-3000:],
            )
            chunks = capture.current_chunks()
            self.assertGreaterEqual(len(chunks), 4)
            self.assertTrue(all(path.stat().st_size % 188 == 0 for path in chunks))

            result = asyncio.run(save_replay(settings, capture, seconds=4))
            media = validate_media(result.output)
            self.assertTrue(media.valid, media.error)
            self.assertGreater(media.video_frames, 0)
            self.assertGreater(media.audio_samples, 0)
            self.assertGreater(result.actual_seconds, 3.0)
            self.assertLessEqual(result.actual_seconds, 5.5)
            self.assertEqual(result.reset_generation, 1)


if __name__ == "__main__":
    unittest.main()
