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
let setupInFlight = false;
let lastStatus = null;

async function livePlaylistExists() {
  try {
    const response = await fetch(`/live/live.m3u8?ts=${Date.now()}`, { cache: "no-store" });
    return response.ok;
  } catch {
    return false;
  }
}

function showStreamStatus(text) {
  liveOverlay.textContent = text;
  liveOverlay.classList.remove("hidden");
}

function showVideo() {
  video.classList.remove("hidden");
  liveOverlay.classList.add("hidden");
}

function hideVideo() {
  video.classList.add("hidden");
}

function resetPlayer() {
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
  video.removeAttribute("src");
  video.load();
  playerStarted = false;
  hideVideo();
}

async function setupPlayer() {
  if (playerStarted || setupInFlight) {
    return;
  }
  setupInFlight = true;

  try {
    if (!(await livePlaylistExists())) {
      showStreamStatus("Waiting for live stream");
      hideVideo();
      return;
    }

    const source = `/live/live.m3u8?ts=${Date.now()}`;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = source;
      playerStarted = true;
      showVideo();
      video.play().catch(() => {});
      return;
    }

    if (window.Hls?.isSupported()) {
      hlsPlayer = new Hls({
        liveSyncDurationCount: 2,
        lowLatencyMode: true,
      });
      hlsPlayer.on(Hls.Events.MANIFEST_PARSED, () => {
        showVideo();
        video.play().catch(() => {});
      });
      hlsPlayer.on(Hls.Events.ERROR, (_event, data) => {
        if (!data.fatal) {
          return;
        }
        resetPlayer();
        showStreamStatus("Reconnecting live stream");
        setTimeout(setupPlayer, 1500);
      });
      hlsPlayer.loadSource(source);
      hlsPlayer.attachMedia(video);
      playerStarted = true;
      return;
    }

    showStreamStatus("Local HLS player failed to load");
  } finally {
    setupInFlight = false;
  }
}

async function refreshStatus() {
  const response = await fetch("/api/status");
  const status = await response.json();
  lastStatus = status;
  captureDot.classList.toggle("running", status.capture_running);
  captureLabel.textContent = status.capture_running ? "Capture running" : "Capture stopped";
  buffered.textContent = `${status.buffered_seconds_estimate} sec`;
  const capture = status.capture || {};
  const videoDevice = capture.selected_video_device || "No camera selected";
  const audioDevice = capture.selected_audio_device || "No microphone selected";
  const error = capture.last_error || capture.device_error || "";
  deviceInfo.textContent = error ? `${videoDevice} / ${audioDevice} - ${error}` : `${videoDevice} / ${audioDevice}`;
  const warning = status.stream_warning || "";
  if (!status.capture_running) {
    resetPlayer();
    showStreamStatus(error || "Waiting for camera");
  } else if (!status.live_hls_ready) {
    resetPlayer();
    showStreamStatus(warning || "Camera detected, waiting for video");
  }
  if (status.live_hls_ready) {
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
