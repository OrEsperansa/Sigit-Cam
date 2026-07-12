const highlightList = document.querySelector("#highlight-list");
const player = document.querySelector("#highlight-player");
const emptyState = document.querySelector("#highlight-empty");
const title = document.querySelector("#highlight-title");
const meta = document.querySelector("#highlight-meta");
const refreshButton = document.querySelector("#refresh-highlights");

let selectedFile = "";

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) {
    return "0 B";
  }
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  return `${value.toFixed(value >= 10 || unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatModified(timestampSeconds) {
  if (!Number.isFinite(timestampSeconds)) {
    return "Unknown time";
  }
  return new Date(timestampSeconds * 1000).toLocaleString();
}

function setSelectedReplay(replay) {
  const replayChanged = selectedFile !== replay.file;
  selectedFile = replay.file;
  // Reassigning src aborts playback. Auto-refresh calls this every 15 seconds,
  // so only load media when the user actually selects a different replay.
  if (replayChanged || !player.getAttribute("src")) {
    player.src = replay.url;
  }
  player.classList.remove("hidden");
  emptyState.classList.add("hidden");
  title.textContent = replay.file;
  meta.textContent = `${formatModified(replay.modified)} - ${formatBytes(replay.bytes)}`;

  for (const item of highlightList.querySelectorAll(".highlight-item")) {
    item.classList.toggle("selected", item.dataset.file === replay.file);
  }
}

function renderEmpty(messageText) {
  highlightList.innerHTML = "";
  const empty = document.createElement("p");
  empty.className = "empty";
  empty.textContent = messageText;
  highlightList.append(empty);
  player.removeAttribute("src");
  player.load();
  selectedFile = "";
  player.classList.add("hidden");
  emptyState.classList.remove("hidden");
  title.textContent = "Highlights";
  meta.textContent = "Saved replays are loaded from this machine.";
}

function renderReplays(replays) {
  highlightList.innerHTML = "";

  if (replays.length === 0) {
    renderEmpty("No replays saved yet.");
    return;
  }

  for (const replay of replays) {
    const item = document.createElement("article");
    item.className = "highlight-item";
    item.dataset.file = replay.file;

    const button = document.createElement("button");
    button.type = "button";
    button.className = "highlight-select";
    button.addEventListener("click", () => setSelectedReplay(replay));

    const name = document.createElement("span");
    name.className = "highlight-name";
    name.textContent = replay.file;

    const info = document.createElement("span");
    info.className = "highlight-info";
    info.textContent = `${formatModified(replay.modified)} - ${formatBytes(replay.bytes)}`;

    button.append(name, info);

    const download = document.createElement("a");
    download.className = "download-link";
    download.href = replay.url;
    download.download = replay.file;
    download.textContent = "Download";

    item.append(button, download);
    highlightList.append(item);
  }

  const selected = replays.find((replay) => replay.file === selectedFile) || replays[0];
  setSelectedReplay(selected);
}

async function refreshHighlights() {
  refreshButton.disabled = true;
  try {
    const response = await fetch(`/api/replays?ts=${Date.now()}`, { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`status ${response.status}`);
    }
    const replays = await response.json();
    renderReplays(replays);
  } catch (error) {
    renderEmpty(`Could not load replays: ${error.message}`);
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener("click", refreshHighlights);
refreshHighlights();
setInterval(refreshHighlights, 30000);