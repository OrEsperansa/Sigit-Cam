const liveImage = document.querySelector("#live-image");
const saveButton = document.querySelector("#save-button");
const message = document.querySelector("#message");
const captureDot = document.querySelector("#capture-dot");
const captureLabel = document.querySelector("#capture-label");
const buffered = document.querySelector("#buffered");
const storageFree = document.querySelector("#storage-free");
const backupStatus = document.querySelector("#backup-status");
const deviceInfo = document.querySelector("#device-info");
const liveOverlay = document.querySelector("#live-overlay");
const audioMeterTrack = document.querySelector("#audio-meter-track");
const audioMeterFill = document.querySelector("#audio-meter-fill");
const audioMeterValue = document.querySelector("#audio-meter-value");
const audioMeterStatus = document.querySelector("#audio-meter-status");
const presetContainer = document.querySelector("#replay-presets");

let playerStarted = false;
let lastLiveFrameCount = 0;
let unchangedStatusPolls = 0;
let displayedAudioPercent = 0;
let selectedSeconds = Number(presetContainer.dataset.default || 180);

function formatDuration(seconds) {
  if (seconds >= 60 && seconds % 60 === 0) return `${seconds / 60} min`;
  return `${seconds} sec`;
}

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes || 0);
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index > 2 ? 1 : 0)} ${units[index]}`;
}

function renderPresets(presets) {
  presetContainer.innerHTML = "";
  if (!presets.includes(selectedSeconds)) selectedSeconds = presets[presets.length - 1];
  for (const seconds of presets) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "preset-button";
    button.dataset.seconds = seconds;
    button.textContent = formatDuration(seconds);
    button.classList.toggle("active", seconds === selectedSeconds);
    button.addEventListener("click", () => {
      selectedSeconds = seconds;
      renderPresets(presets);
    });
    presetContainer.append(button);
  }
}

function renderAudioLevel(capture) {
  const hasSamples = capture.audio_active && Number.isFinite(capture.audio_peak_db);
  const db = hasSamples ? Math.max(-60, Math.min(0, capture.audio_peak_db)) : -60;
  const targetPercent = hasSamples ? ((db + 60) / 60) * 100 : 0;
  displayedAudioPercent += (targetPercent - displayedAudioPercent) * 0.55;
  if (targetPercent === 0 && displayedAudioPercent < 0.5) displayedAudioPercent = 0;
  audioMeterFill.style.width = `${displayedAudioPercent.toFixed(1)}%`;
  audioMeterTrack.setAttribute("aria-valuenow", db.toFixed(1));
  audioMeterTrack.classList.toggle("receiving", hasSamples && capture.audio_peak_db > -55);
  audioMeterValue.textContent = hasSamples ? `${targetPercent.toFixed(0)}% / ${capture.audio_peak_db.toFixed(1)} dB` : "No signal";
  audioMeterStatus.textContent = !capture.selected_audio_device
    ? "No input selected"
    : !capture.audio_active
      ? "No audio samples are arriving"
      : capture.audio_peak_db > -55 ? "Receiving input" : "Receiving silence";
}

function showStreamStatus(text) {
  liveOverlay.textContent = text;
  liveOverlay.classList.remove("hidden");
}

function resetPlayer() {
  liveImage.removeAttribute("src");
  liveImage.classList.add("hidden");
  playerStarted = false;
  unchangedStatusPolls = 0;
}

function setupPlayer() {
  if (playerStarted) return;
  liveImage.src = `/live.mjpg?ts=${Date.now()}`;
  liveImage.classList.remove("hidden");
  liveOverlay.classList.add("hidden");
  playerStarted = true;
}

liveImage.addEventListener("error", () => {
  resetPlayer();
  showStreamStatus("Live stream disconnected, reconnecting");
  setTimeout(refreshStatus, 1000);
});

async function refreshStatus() {
  try {
    const response = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`status ${response.status}`);
    const status = await response.json();
    const capture = status.capture || {};
    captureDot.classList.toggle("running", status.capture_running);
    captureLabel.textContent = status.settings_apply_in_progress
      ? "Applying settings"
      : status.capture_running ? "Live and recording" : "Offline";
    buffered.textContent = `${Number(status.buffered_seconds_estimate || 0).toFixed(1)} sec`;
    backupStatus.textContent = status.backup?.configured ? "Copy on save" : "Disabled";
    deviceInfo.textContent = `${capture.selected_video_device || "No camera"} / ${capture.selected_audio_device || "No microphone"}`;
    renderAudioLevel(capture);
    renderPresets(status.replay_presets_seconds || [status.default_replay_seconds]);
    saveButton.disabled = status.save_in_progress || status.settings_apply_in_progress || !status.capture_running;
    if (status.save_in_progress) message.textContent = "Saving and validating replay...";

    if (!status.live_ready) {
      resetPlayer();
      showStreamStatus(status.stream_warning || "Waiting for live stream");
      return;
    }
    const frameCount = capture.live_frame_count || 0;
    unchangedStatusPolls = playerStarted && frameCount === lastLiveFrameCount ? unchangedStatusPolls + 1 : 0;
    lastLiveFrameCount = frameCount;
    if (playerStarted && unchangedStatusPolls >= 5) {
      resetPlayer();
      showStreamStatus("Live stream stalled, reconnecting");
      setTimeout(setupPlayer, 500);
      return;
    }
    setupPlayer();
  } catch (error) {
    captureDot.classList.remove("running");
    captureLabel.textContent = "Service unavailable";
    resetPlayer();
    showStreamStatus(`Cannot reach server: ${error.message}`);
  }
}

async function refreshStorage() {
  try {
    const response = await fetch("/api/storage", { cache: "no-store" });
    if (!response.ok) return;
    const storage = await response.json();
    storageFree.textContent = formatBytes(storage.free_bytes);
    storageFree.className = storage.level === "ok" ? "" : `storage-${storage.level}`;
  } catch (_) {
    storageFree.textContent = "Unavailable";
  }
}

async function saveReplay() {
  if (saveButton.disabled) return;
  saveButton.disabled = true;
  message.textContent = `Saving the last ${formatDuration(selectedSeconds)}...`;
  try {
    const response = await window.sigitFetch("/api/replays", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seconds: selectedSeconds }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Save failed");
    const duration = Number(data.duration_seconds || 0).toFixed(1);
    message.textContent = data.partial ? `Saved ${duration} sec of available footage.` : `Saved ${duration} sec.`;
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
    refreshStatus();
  }
}

saveButton.addEventListener("click", saveReplay);
document.addEventListener("keydown", (event) => {
  const tag = event.target?.tagName?.toLowerCase();
  if (event.key.toLowerCase() === "r" && !["input", "textarea", "select"].includes(tag)) {
    event.preventDefault();
    saveReplay();
  }
});

renderPresets(Array.from(presetContainer.querySelectorAll("[data-seconds]")).map((button) => Number(button.dataset.seconds)));
refreshStatus();
refreshStorage();
setInterval(refreshStatus, 2000);
setInterval(refreshStorage, 15000);
