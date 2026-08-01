const liveImage = document.querySelector("#live-image");
const saveButton = document.querySelector("#save-button");
const message = document.querySelector("#message");
const captureDot = document.querySelector("#capture-dot");
const captureLabel = document.querySelector("#capture-label");
const buffered = document.querySelector("#buffered");
const backupStatus = document.querySelector("#backup-status");
const deviceInfo = document.querySelector("#device-info");
const liveOverlay = document.querySelector("#live-overlay");
const audioMeterTrack = document.querySelector("#audio-meter-track");
const audioMeterFill = document.querySelector("#audio-meter-fill");
const audioMeterValue = document.querySelector("#audio-meter-value");
const audioMeterStatus = document.querySelector("#audio-meter-status");

let playerStarted = false;
let lastLiveFrameCount = 0;
let unchangedStatusPolls = 0;
let displayedAudioPercent = 0;

function renderAudioLevel(level) {
  if (!level) {
    displayedAudioPercent = 0;
    audioMeterFill.style.width = "0%";
    audioMeterTrack.setAttribute("aria-valuetext", "Microphone level unavailable");
    audioMeterValue.textContent = "Unavailable";
    audioMeterStatus.textContent = "Cannot read input level";
    return;
  }

  const hasSamples = level.active && Number.isFinite(level.peakDb);
  const db = hasSamples ? Math.max(-60, Math.min(0, level.peakDb)) : -60;
  const targetPercent = hasSamples ? ((db + 60) / 60) * 100 : 0;
  displayedAudioPercent += (targetPercent - displayedAudioPercent) * 0.55;
  if (targetPercent === 0 && displayedAudioPercent < 0.5) displayedAudioPercent = 0;

  audioMeterFill.style.width = `${displayedAudioPercent.toFixed(1)}%`;
  audioMeterTrack.setAttribute("aria-valuenow", db.toFixed(1));
  audioMeterTrack.setAttribute(
    "aria-valuetext",
    hasSamples ? `${targetPercent.toFixed(0)} percent, ${level.peakDb.toFixed(1)} decibels` : "No microphone signal",
  );
  audioMeterTrack.classList.toggle("receiving", hasSamples && level.peakDb > -55);
  audioMeterValue.textContent = hasSamples
    ? `${targetPercent.toFixed(0)}% · ${level.peakDb.toFixed(1)} dB`
    : "No signal";
  audioMeterStatus.textContent = !level.microphone
    ? "No input selected"
    : !hasSamples
      ? "Input detected, but no samples are arriving"
      : level.peakDb > -55
        ? "Receiving input"
        : "Receiving silence";
}

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
  unchangedStatusPolls = 0;
}

function setupPlayer() {
  if (playerStarted) return;
  liveImage.src = `/live.mjpg?ts=${Date.now()}`;
  playerStarted = true;
  showLiveImage();
}

liveImage.addEventListener("error", () => {
  resetPlayer();
  showStreamStatus("Live stream disconnected, reconnecting");
  setTimeout(refreshStatus, 1000);
});

async function refreshStatus() {
  let status;
  try {
    const response = await fetch(`/api/status?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`status ${response.status}`);
    status = await response.json();
  } catch (error) {
    captureDot.classList.remove("running");
    captureLabel.textContent = "Service unavailable";
    renderAudioLevel(null);
    resetPlayer();
    showStreamStatus(`Cannot reach server: ${error.message}`);
    return;
  }

  const capture = status.capture || {};
  captureDot.classList.toggle("running", status.capture_running);
  captureLabel.textContent = status.capture_running ? "Live" : "Offline";
  buffered.textContent = `${Number(status.buffered_seconds_estimate || 0).toFixed(1)} sec`;
  renderAudioLevel({
    peakDb: capture.audio_peak_db,
    active: capture.audio_active,
    microphone: capture.selected_audio_device,
  });

  const backup = status.backup || {};
  if (!backup.configured) {
    backupStatus.textContent = "Disabled";
    backupStatus.title = "Secondary storage is not configured";
  } else {
    backupStatus.textContent = "Copy on save";
    backupStatus.title = backup.path || "";
  }

  const videoDevice = capture.selected_video_device || "No video source";
  const audioDevice = capture.selected_audio_device || "No audio source";
  const error = capture.recording_warning || capture.last_error || capture.device_error || "";
  deviceInfo.textContent = error ? `${videoDevice} / ${audioDevice} - ${error}` : `${videoDevice} / ${audioDevice}`;
  if (capture.recording_warning) {
    captureLabel.textContent = "Recording warning";
    captureDot.classList.remove("running");
  }

  if (!status.live_ready) {
    resetPlayer();
    showStreamStatus(status.stream_warning || error || "Waiting for live stream");
    return;
  }

  const liveFrameCount = capture.live_frame_count || 0;
  unchangedStatusPolls = playerStarted && liveFrameCount === lastLiveFrameCount
    ? unchangedStatusPolls + 1
    : 0;
  lastLiveFrameCount = liveFrameCount;

  if (playerStarted && unchangedStatusPolls >= 4) {
    resetPlayer();
    showStreamStatus("Live stream stalled, reconnecting");
    setTimeout(setupPlayer, 500);
    return;
  }
  setupPlayer();
}

saveButton.addEventListener("click", async () => {
  saveButton.disabled = true;
  message.textContent = "Saving...";
  try {
    const response = await fetch("/api/replays", { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Save failed");
    const duration = Number.isFinite(data.actual_seconds) ? `${data.actual_seconds.toFixed(1)} sec` : "replay";
    const localMessage = data.deduplicated
      ? "A save was already in progress"
      : data.partial
        ? `Saved ${duration} of available footage`
        : `Saved ${duration}`;
    const backupMessage = data.backup_error ? " Secondary copy failed." : "";
    message.textContent = `${localMessage}${backupMessage}`;
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
});

refreshStatus();
setInterval(refreshStatus, 2000);
