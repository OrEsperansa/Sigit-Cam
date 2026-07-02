const video = document.querySelector("#live-video");
const saveButton = document.querySelector("#save-button");
const message = document.querySelector("#message");
const replayList = document.querySelector("#replay-list");
const captureDot = document.querySelector("#capture-dot");
const captureLabel = document.querySelector("#capture-label");
const buffered = document.querySelector("#buffered");
const deviceInfo = document.querySelector("#device-info");
const liveOverlay = document.querySelector("#live-overlay");
let hlsPlayer = null;
let playerStarted = false;

async function livePlaylistExists() {
  try {
    const response = await fetch(`/static/hls/live.m3u8?ts=${Date.now()}`, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

async function setupPlayer() {
  if (playerStarted) {
    return;
  }

  if (!(await livePlaylistExists())) {
    liveOverlay.textContent = "Waiting for live stream";
    liveOverlay.classList.remove("hidden");
    return;
  }

  const source = `/static/hls/live.m3u8?ts=${Date.now()}`;
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = source;
    playerStarted = true;
    liveOverlay.classList.add("hidden");
    return;
  }

  if (window.Hls?.isSupported()) {
    hlsPlayer = new Hls({
      liveSyncDurationCount: 2,
      lowLatencyMode: true,
    });
    hlsPlayer.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) {
        return;
      }
      hlsPlayer.destroy();
      hlsPlayer = null;
      playerStarted = false;
      liveOverlay.textContent = "Reconnecting live stream";
      liveOverlay.classList.remove("hidden");
      setTimeout(setupPlayer, 1500);
    });
    hlsPlayer.loadSource(source);
    hlsPlayer.attachMedia(video);
    playerStarted = true;
    liveOverlay.classList.add("hidden");
  } else {
    message.textContent = "This browser cannot play the live stream.";
  }
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  const status = await response.json();
  captureDot.classList.toggle("running", status.capture_running);
  captureLabel.textContent = status.capture_running ? "Capture running" : "Capture stopped";
  buffered.textContent = `${status.buffered_seconds_estimate} sec`;
  const capture = status.capture || {};
  const videoDevice = capture.selected_video_device || "No camera selected";
  const audioDevice = capture.selected_audio_device || "No microphone selected";
  const error = capture.last_error || capture.device_error || "";
  deviceInfo.textContent = error ? `${videoDevice} / ${audioDevice} - ${error}` : `${videoDevice} / ${audioDevice}`;
  if (!status.capture_running) {
    liveOverlay.textContent = error || "Waiting for camera";
    liveOverlay.classList.remove("hidden");
  } else if (!status.live_hls_ready) {
    liveOverlay.textContent = "Starting live stream";
    liveOverlay.classList.remove("hidden");
  }
  if (status.capture_running || status.live_hls_ready) {
    setupPlayer();
  }
}

async function refreshReplays() {
  const response = await fetch("/api/replays");
  const replays = await response.json();
  replayList.innerHTML = "";

  if (replays.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "No replays saved yet.";
    replayList.append(empty);
    return;
  }

  for (const replay of replays) {
    const row = document.createElement("a");
    row.href = replay.url;
    row.className = "replay";
    row.download = replay.file;
    row.textContent = replay.file;
    replayList.append(row);
  }
}

saveButton.addEventListener("click", async () => {
  saveButton.disabled = true;
  message.textContent = "Saving replay...";
  try {
    const response = await fetch("/api/replays", { method: "POST" });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.detail || "Replay save failed");
    }
    message.textContent = `Saved ${data.file}`;
    await refreshReplays();
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
});

refreshStatus();
refreshReplays();
setInterval(refreshStatus, 2000);
setInterval(setupPlayer, 3000);
setInterval(refreshReplays, 10000);
