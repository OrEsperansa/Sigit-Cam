const listElement = document.querySelector("#highlight-list");
const player = document.querySelector("#highlight-player");
const emptyState = document.querySelector("#highlight-empty");
const metadataForm = document.querySelector("#metadata-form");
const metadataMessage = document.querySelector("#metadata-message");
const metadataSummary = document.querySelector("#highlight-meta");
const titleInput = document.querySelector("#replay-title");
const tagsInput = document.querySelector("#replay-tags");
const notesInput = document.querySelector("#replay-notes");
const favoriteInput = document.querySelector("#replay-favorite");
const downloadLink = document.querySelector("#download-replay");
const trashButton = document.querySelector("#trash-replay");
const refreshButton = document.querySelector("#refresh-highlights");
const searchInput = document.querySelector("#replay-search");
const favoriteFilter = document.querySelector("#favorite-filter");
const storageSummary = document.querySelector("#storage-summary");

let mode = "library";
let records = [];
let selectedId = "";
let searchTimer;

function formatBytes(bytes) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(bytes) || 0;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return `${value.toFixed(index > 0 && value < 10 ? 1 : 0)} ${units[index]}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function errorDetail(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  return fallback;
}

function clearSelection() {
  selectedId = "";
  player.pause();
  player.removeAttribute("src");
  player.load();
  player.classList.add("hidden");
  metadataForm.classList.add("hidden");
  emptyState.classList.remove("hidden");
  emptyState.textContent = mode === "trash" ? "Restore a replay before viewing it." : "Select a replay to view and edit it.";
}

function selectRecord(record) {
  selectedId = record.id;
  for (const item of listElement.querySelectorAll(".highlight-item")) {
    item.classList.toggle("selected", item.dataset.id === selectedId);
  }
  if (mode === "trash") return;
  if (player.getAttribute("src") !== record.url) {
    player.src = record.url;
  }
  player.classList.remove("hidden");
  emptyState.classList.add("hidden");
  metadataForm.classList.remove("hidden");
  titleInput.value = record.title || "";
  tagsInput.value = (record.tags || []).join(", ");
  notesInput.value = record.notes || "";
  favoriteInput.checked = Boolean(record.favorite);
  downloadLink.href = record.url;
  downloadLink.download = record.filename;
  metadataSummary.textContent = `${formatDate(record.created_at)} · ${formatDuration(record.duration_seconds)} · ${formatBytes(record.bytes)}`;
  metadataMessage.textContent = record.backup_status === "failed" ? `Backup failed: ${record.backup_error}` : "";
}

function makeReplayCard(record) {
  const item = document.createElement("article");
  item.className = "highlight-item";
  item.dataset.id = record.id;
  const body = document.createElement("button");
  body.type = "button";
  body.className = "highlight-select";
  if (record.thumbnail_url) {
    const image = document.createElement("img");
    image.className = "replay-thumbnail";
    image.src = record.thumbnail_url;
    image.alt = "";
    image.loading = "lazy";
    body.append(image);
  }
  const copy = document.createElement("span");
  copy.className = "replay-card-copy";
  const name = document.createElement("span");
  name.className = "highlight-name";
  name.textContent = `${record.favorite ? "★ " : ""}${record.title}`;
  const info = document.createElement("span");
  info.className = "highlight-info";
  info.textContent = `${formatDuration(record.duration_seconds)} · ${formatDate(record.created_at)} · ${formatBytes(record.bytes)}`;
  copy.append(name, info);
  body.append(copy);

  const actions = document.createElement("div");
  actions.className = "card-actions";
  if (mode === "trash") {
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "small-button";
    restore.textContent = "Restore";
    restore.addEventListener("click", () => restoreReplay(record));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "small-button danger-text";
    remove.textContent = "Delete";
    remove.addEventListener("click", () => deleteReplay(record));
    actions.append(restore, remove);
  } else {
    body.addEventListener("click", () => selectRecord(record));
  }
  item.append(body, actions);
  return item;
}

function renderRecords() {
  listElement.replaceChildren();
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = mode === "trash" ? "Trash is empty." : "No matching replays.";
    listElement.append(empty);
    clearSelection();
    return;
  }
  records.forEach((record) => listElement.append(makeReplayCard(record)));
  const selected = records.find((record) => record.id === selectedId);
  if (selected && mode === "library") selectRecord(selected);
  else clearSelection();
}

async function refreshStorage() {
  try {
    const response = await fetch("/api/storage", { cache: "no-store" });
    const storage = await response.json();
    if (!response.ok) throw new Error("storage unavailable");
    storageSummary.className = `storage-summary ${storage.level}`;
    storageSummary.textContent = `${formatBytes(storage.free_bytes)} free · ${formatBytes(storage.replay_bytes)} in Library · ${formatBytes(storage.trash_bytes)} in Trash`;
  } catch (error) {
    storageSummary.textContent = `Storage status unavailable: ${error.message}`;
  }
}

async function refreshRecords() {
  refreshButton.disabled = true;
  try {
    const params = new URLSearchParams({ ts: Date.now().toString() });
    if (mode === "library") {
      if (searchInput.value.trim()) params.set("q", searchInput.value.trim());
      if (favoriteFilter.checked) params.set("favorite", "true");
    }
    const endpoint = mode === "trash" ? "/api/trash" : "/api/replays";
    const response = await fetch(`${endpoint}?${params}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(errorDetail(payload, `status ${response.status}`));
    records = payload;
    renderRecords();
    await refreshStorage();
  } catch (error) {
    records = [];
    renderRecords();
    listElement.querySelector(".empty").textContent = `Could not load replays: ${error.message}`;
  } finally {
    refreshButton.disabled = false;
  }
}

async function restoreReplay(record) {
  const response = await window.sigitFetch(`/api/trash/${encodeURIComponent(record.id)}/restore`, { method: "POST" });
  if (!response.ok) {
    const payload = await response.json();
    window.alert(errorDetail(payload, "Restore failed"));
  }
  await refreshRecords();
}

async function deleteReplay(record) {
  const confirmation = window.prompt(`Permanently delete this replay? Type ${record.id} to confirm.`);
  if (confirmation === null) return;
  const response = await window.sigitFetch(`/api/trash/${encodeURIComponent(record.id)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: confirmation }),
  });
  if (!response.ok) {
    const payload = await response.json();
    window.alert(errorDetail(payload, "Delete failed"));
  }
  await refreshRecords();
}

metadataForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  metadataMessage.textContent = "Saving";
  const response = await window.sigitFetch(`/api/replays/${encodeURIComponent(selectedId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      title: titleInput.value,
      tags: tagsInput.value.split(",").map((tag) => tag.trim()).filter(Boolean),
      notes: notesInput.value,
      favorite: favoriteInput.checked,
    }),
  });
  const payload = await response.json();
  if (!response.ok) {
    metadataMessage.textContent = errorDetail(payload, "Could not save details");
    return;
  }
  metadataMessage.textContent = "Details saved";
  selectedId = payload.id;
  await refreshRecords();
});

trashButton.addEventListener("click", async () => {
  const record = records.find((item) => item.id === selectedId);
  if (!record || !window.confirm(`Move “${record.title}” to Trash?`)) return;
  const response = await window.sigitFetch(`/api/replays/${encodeURIComponent(selectedId)}/trash`, { method: "POST" });
  if (!response.ok) {
    const payload = await response.json();
    metadataMessage.textContent = errorDetail(payload, "Could not move replay to Trash");
    return;
  }
  await refreshRecords();
});

document.querySelectorAll(".library-tab").forEach((tab) => tab.addEventListener("click", () => {
  mode = tab.dataset.mode;
  document.querySelectorAll(".library-tab").forEach((item) => item.classList.toggle("active", item === tab));
  document.querySelector(".library-filters").classList.toggle("hidden", mode === "trash");
  clearSelection();
  refreshRecords();
}));

searchInput.addEventListener("input", () => {
  window.clearTimeout(searchTimer);
  searchTimer = window.setTimeout(refreshRecords, 250);
});
favoriteFilter.addEventListener("change", refreshRecords);
refreshButton.addEventListener("click", refreshRecords);
refreshRecords();
window.setInterval(refreshRecords, 30000);
