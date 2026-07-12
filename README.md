# Instant Replay Camera System

Local PC-hosted instant replay system with:

- Browser live viewer
- Continuous rolling chunk recorder
- One-click replay saving without stopping capture
- Replay list and downloads

The current implementation uses FastAPI and bundled FFmpeg. The live viewer uses browser-native MJPEG for low-latency LAN viewing with no CDN, plugin, or extra media server. The replay path is independent of the live viewer: saving a replay only concatenates already-recorded chunks with `-c copy`.

## Requirements

- Python 3.11+
- FFmpeg binary in `ffmpeg/` or FFmpeg available on `PATH`
- A camera/audio source supported by FFmpeg

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open the control page:

```text
http://PC-IP:8000
```

Local machine:

```text
http://127.0.0.1:8000
```

## Windows USB Camera Example

List DirectShow devices:

```powershell
ffmpeg -list_devices true -f dshow -i dummy
```

Run with device names:

```powershell
$env:INPUT_MODE="dshow"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

By default the app auto-selects the first detected camera and microphone. Set `VIDEO_DEVICE` and `AUDIO_DEVICE` only when you want to force specific devices.

## RTSP / IP Camera Example

```powershell
$env:INPUT_MODE="rtsp"
$env:RTSP_URL="rtsp://user:pass@camera-ip:554/stream1"
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Main Config

Environment variables:

```text
REPLAY_MINUTES=3
MAX_BUFFER_MINUTES=5
CHUNK_SECONDS=5
REPLAY_FINALIZE_WAIT_SECONDS=7
REPLAY_AUDIO_MODE=repair
LIVE_FPS=5
LIVE_WIDTH=640
LIVE_JPEG_QUALITY=10
DSHOW_RTBUFSIZE=8M
LOW_LATENCY_CAPTURE=1
RTSP_TRANSPORT=tcp
CAMERA_ROTATION_DEGREES=0
VIDEO_RESOLUTION=1280x720
FPS=30
VIDEO_CODEC=libx264
VIDEO_PIXEL_FORMAT=auto
AUDIO_CODEC=aac
AUDIO_SYNC_OFFSET_MS=-120
INPUT_MODE=dshow
AUTO_DETECT_DEVICES=1
VIDEO_DEVICE=
AUDIO_DEVICE=
RTSP_URL=
# Optional override. By default the app uses ./ffmpeg/ffmpeg.exe.
# FFMPEG_PATH=C:\path\to\ffmpeg.exe
```

You can also copy `.env.example` to `.env`; the app loads `.env` automatically on startup.


Optional backup share:

```text
REPLAY_BACKUP_DIR=\\server\share\SigitCamReplays
```

`CAMERA_ROTATION_DEGREES` rotates both the live browser view and newly recorded replay chunks. Use `90`, `180`, `270`, or any degree value; restart the app after changing it.

`AUDIO_SYNC_OFFSET_MS` compensates for a fixed camera/microphone sync offset. Negative values advance late audio and positive values delay early audio; for example, `-120` advances audio by 120 ms.

When `REPLAY_BACKUP_DIR` is set, each replay is first saved locally under `data/replays/` and then copied atomically to the configured backup directory. Failed share copies remain local and are retried every 30 seconds, including after an application restart. Backup health and pending copies are shown on the camera page. For Intel Quick Sync, set `VIDEO_CODEC=h264_qsv`; the default `VIDEO_PIXEL_FORMAT=auto` selects `nv12`, which is compatible with `h264_qsv`. The default `REPLAY_AUDIO_MODE=repair` copies video while rebuilding AAC audio timestamps during replay save; use `REPLAY_AUDIO_MODE=copy` to restore the old no-reencode behavior.
Output folders are created automatically:

```text
data/chunks/
data/replays/
```

## Notes

- Replay saving does not restart the camera.
- Replay saving does not re-encode.
- The capture process is started when the FastAPI app starts.
- If FFmpeg cannot open the configured device, check the server logs and verify device names.
- Live MJPEG video is available at `/live.mjpg`; saved replays are MP4 files with audio.
