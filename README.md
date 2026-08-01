# Sigit Live

Sigit Live is a Windows 10 instant-replay camera service. One FFmpeg 6 process opens the camera and microphone together through DirectShow, publishes a browser MJPEG view, and maintains a rolling H.264/AAC buffer. Pressing **Save** snapshots the current tail immediately and publishes a validated MP4 without stopping capture.

## Requirements

- Windows 10
- Python 3.11+
- FFmpeg 6 in `ffmpeg/ffmpeg.exe` or on `PATH`
- A DirectShow camera and microphone

Install dependencies and copy the configuration template:

```powershell
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

List the exact device names:

```powershell
.\ffmpeg\ffmpeg.exe -list_devices true -f dshow -i dummy
```

Set both names in `.env`; automatic selection is intentionally unsupported because Windows device order is unstable.

```text
VIDEO_DEVICE=Creative Live! Cam Sync 1080p V2
AUDIO_DEVICE=Microphone (Example Device)
REPLAY_MINUTES=3
```

Also set `APP_PASSWORD` and a random `SESSION_SECRET` containing at least 32 characters.

## Run

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000` locally or use the PC's LAN address.

## Capture and Recovery

The recorder uses fixed `libx264`, AAC, closed GOPs, and flushed two-second MPEG-TS segments. Saved replays copy finalized segments and packet-align a byte snapshot of the active segment, so the click-time tail does not wait for segment finalization. Audio is rebuilt into the final MP4, then FFmpeg verifies positive video frames and decoded audio samples before the file is atomically published.

The watchdog tracks video frames, audio frames, segment progress, and the FFmpeg process independently. Digital silence is healthy because samples still arrive. Missing audio for more than `AUDIO_STALL_SECONDS` triggers a complete generation reset. Every startup and reset stops the process and its readers, removes all rolling chunks and abandoned snapshots, and starts with a new generation. Saved files under `data/replays/` are never removed by a reset.

## Replay Storage

Runtime files are created under:

```text
data/chunks/   ephemeral rolling MPEG-TS segments
data/work/     temporary immutable replay snapshots
data/replays/  validated MP4 replays
```

`REPLAY_BACKUP_DIR` optionally copies only the replay just saved. It does not scan or synchronize folders. The copy runs only after local validation and uses an atomic replacement; a backup failure does not remove the local replay.

## Verification

```powershell
python -m unittest discover -s tests -v
python -m compileall -q app tests
```

The container image can still be built for static checks, but DirectShow camera capture is Windows-host only and is not available inside the Linux container.
