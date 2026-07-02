const video = document.querySelector("#live-video");
const saveButton = document.querySelector("#save-button");
const message = document.querySelector("#message");
const replayList = document.querySelector("#replay-list");
const captureDot = document.querySelector("#capture-dot");
const captureLabel = document.querySelector("#capture-label");
const buffered = document.querySelector("#buffered");
const deviceInfo = document.querySelector("#device-info");

function setupPlayer() {
  const source = "/static/hls/live.m3u8";
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = source;
    return;
  }

  if (window.Hls?.isSupported()) {
    const hls = new Hls({
      liveSyncDurationCount: 2,
      lowLatencyMode: true,
    });
    hls.loadSource(source);
    hls.attachMedia(video);
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

setupPlayer();
refreshStatus();
refreshReplays();
setInterval(refreshStatus, 2000);
setInterval(refreshReplays, 10000);
