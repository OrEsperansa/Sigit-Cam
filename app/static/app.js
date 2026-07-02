const liveImage = document.querySelector("#live-image");
const saveButton = document.querySelector("#save-button");
const message = document.querySelector("#message");
const replayList = document.querySelector("#replay-list");
const captureDot = document.querySelector("#capture-dot");
const captureLabel = document.querySelector("#capture-label");
const buffered = document.querySelector("#buffered");
const deviceInfo = document.querySelector("#device-info");
const liveOverlay = document.querySelector("#live-overlay");

let playerStarted = false;

function showStreamStatus(text) {
  liveOverlay.textContent = text;
  liveOverlay.classList.remove("hidden");
}

function showLiveImage() {
  liveImage.classList.remove("hidden");
  liveOverlay.classList.add("hidden");
}

function resetPlayer() {
  liveImage.removeAttribute("src");
  liveImage.classList.add("hidden");
  playerStarted = false;
}

function setupPlayer() {
  if (playerStarted) {
    return;
  }

  liveImage.src = `/live.mjpg?ts=${Date.now()}`;
  playerStarted = true;
  showLiveImage();
}

async function refreshStatus() {
  let status;
  try {
    const response = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`status ${response.status}`);
    }
    status = await response.json();
  } catch (error) {
    captureDot.classList.remove("running");
    captureLabel.textContent = "Server unreachable";
    resetPlayer();
    showStreamStatus(`Cannot reach server: ${error.message}`);
    return;
  }

  captureDot.classList.toggle("running", status.capture_running);
  captureLabel.textContent = status.capture_running ? "Capture running" : "Capture stopped";
  buffered.textContent = `${status.buffered_seconds_estimate} sec`;

  const capture = status.capture || {};
  const videoDevice = capture.selected_video_device || "No camera selected";
  const audioDevice = capture.selected_audio_device || "No microphone selected";
  const error = capture.last_error || capture.device_error || "";
  deviceInfo.textContent = error ? `${videoDevice} / ${audioDevice} - ${error}` : `${videoDevice} / ${audioDevice}`;

  if (!status.live_ready) {
    resetPlayer();
    showStreamStatus(status.stream_warning || error || "Waiting for live stream");
    return;
  }

  setupPlayer();
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
    message.textContent = data.deduplicated ? `Already saving, linked ${data.file}` : `Saved ${data.file}`;
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
setInterval(refreshReplays, 10000);
