# Instant Replay Camera System

Local PC-hosted instant replay system with:

- Browser live viewer
- Continuous rolling chunk recorder
- One-click replay saving without stopping capture
- Replay list and downloads

The current implementation uses FastAPI, FFmpeg, and bundled MediaMTX. The live viewer uses WebRTC for low latency. The replay path is independent of the live viewer: saving a replay only concatenates already-recorded chunks with `-c copy`.

## Requirements

- Python 3.11+
- FFmpeg binary in `ffmpeg/` or FFmpeg available on `PATH`
- MediaMTX binary in `tools/mediamtx/`
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

The WebRTC player is embedded from MediaMTX on:

```text
http://PC-IP:8889/live/
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
HLS_SEGMENT_SECONDS=1
VIDEO_RESOLUTION=1920x1080
FPS=30
VIDEO_CODEC=libx264
AUDIO_CODEC=aac
INPUT_MODE=dshow
AUTO_DETECT_DEVICES=1
VIDEO_DEVICE=
AUDIO_DEVICE=
RTSP_URL=
# Optional override. By default the app uses ./ffmpeg/ffmpeg.exe.
# FFMPEG_PATH=C:\path\to\ffmpeg.exe
WEBRTC_HTTP_PORT=8889
WEBRTC_STREAM_PATH=live
RTSP_PUBLISH_URL=rtsp://127.0.0.1:8554/live
```

You can also copy `.env.example` to `.env`; the app loads `.env` automatically on startup.

Output folders are created automatically:

```text
data/chunks/
data/replays/
app/static/hls/
```

## Notes

- Replay saving does not restart the camera.
- Replay saving does not re-encode.
- The capture process is started when the FastAPI app starts.
- If FFmpeg cannot open the configured device, check the server logs and verify device names.
- WebRTC can be added by publishing the same FFmpeg capture to MediaMTX while leaving the rolling segment recorder unchanged.
