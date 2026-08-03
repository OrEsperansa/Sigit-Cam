const form = document.querySelector("#settings-form");
const message = document.querySelector("#settings-message");
const saveButton = document.querySelector("#save-settings");
const refreshDevicesButton = document.querySelector("#refresh-devices");
const testBackupButton = document.querySelector("#test-backup");
const backupTestMessage = document.querySelector("#backup-test-message");
const presetInput = document.querySelector("#replay_presets_seconds");
const defaultPreset = document.querySelector("#default_replay_seconds");
const numericFields = new Set([
  "fps", "camera_rotation_degrees", "audio_sync_offset_ms", "live_fps", "live_width",
  "live_jpeg_quality", "default_replay_seconds", "max_buffer_minutes", "audio_startup_grace_seconds",
  "audio_stall_seconds", "video_stall_seconds", "restart_max_backoff_seconds",
]);

function detailText(payload, fallback) {
  if (typeof payload?.detail === "string") return payload.detail;
  return fallback;
}

function syncDefaultOptions(preferred) {
  const presets = presetInput.value.split(",").map((value) => Number.parseInt(value.trim(), 10)).filter((value) => value > 0);
  const selected = Number(preferred || defaultPreset.value || presets[0]);
  defaultPreset.replaceChildren();
  [...new Set(presets)].sort((a, b) => a - b).forEach((seconds) => {
    const option = document.createElement("option");
    option.value = String(seconds);
    option.textContent = `${seconds} seconds`;
    option.selected = seconds === selected;
    defaultPreset.append(option);
  });
}

function setSelectOptions(select, items, selected) {
  select.replaceChildren();
  const values = items.includes(selected) ? items : [selected, ...items].filter(Boolean);
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.append(option);
  });
  select.value = selected;
}

async function loadDevices(values = null) {
  refreshDevicesButton.disabled = true;
  try {
    const response = await fetch("/api/devices", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(detailText(payload, "Could not detect devices"));
    const videoSelect = document.querySelector("#video_device");
    const audioSelect = document.querySelector("#audio_device");
    setSelectOptions(videoSelect, payload.video || [], values?.video_device || videoSelect.value);
    setSelectOptions(audioSelect, payload.audio || [], values?.audio_device || audioSelect.value);
  } catch (error) {
    message.textContent = error.message;
  } finally {
    refreshDevicesButton.disabled = false;
  }
}

function populate(values) {
  document.querySelectorAll("[data-setting]").forEach((field) => {
    if (field.id === "replay_presets_seconds") field.value = (values[field.id] || []).join(", ");
    else if (field.id !== "video_device" && field.id !== "audio_device" && field.id !== "default_replay_seconds") field.value = values[field.id] ?? "";
  });
  syncDefaultOptions(values.default_replay_seconds);
}

async function loadSettings() {
  saveButton.disabled = true;
  message.textContent = "Loading settings";
  try {
    const response = await fetch("/api/settings", { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok) throw new Error(detailText(payload, "Could not load settings"));
    populate(payload.values);
    await loadDevices(payload.values);
    message.textContent = "";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

function collectValues() {
  const values = {};
  document.querySelectorAll("[data-setting]").forEach((field) => {
    if (field.id === "replay_presets_seconds") {
      values[field.id] = field.value.split(",").map((value) => Number.parseInt(value.trim(), 10)).filter(Number.isFinite);
    } else if (numericFields.has(field.id)) {
      values[field.id] = Number(field.value);
    } else {
      values[field.id] = field.value.trim();
    }
  });
  return values;
}

async function submitSettings(confirmBufferReset = false) {
  saveButton.disabled = true;
  message.textContent = confirmBufferReset ? "Testing capture settings; this can take up to 20 seconds" : "Validating settings";
  try {
    const response = await window.sigitFetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ values: collectValues(), confirm_buffer_reset: confirmBufferReset }),
    });
    const payload = await response.json();
    if (response.status === 409 && payload.detail?.code === "buffer_reset_required") {
      const fields = payload.detail.fields.join(", ");
      if (window.confirm(`These changes reset the rolling buffer: ${fields}. Continue?`)) {
        await submitSettings(true);
      } else {
        message.textContent = "Changes were not applied";
      }
      return;
    }
    if (!response.ok) throw new Error(detailText(payload, "Settings could not be applied"));
    populate(payload.values);
    message.textContent = payload.capture_reset ? "Settings tested and saved. A fresh rolling buffer is now recording." : "Settings saved.";
  } catch (error) {
    message.textContent = error.message;
  } finally {
    saveButton.disabled = false;
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  submitSettings(false);
});
presetInput.addEventListener("change", () => syncDefaultOptions());
refreshDevicesButton.addEventListener("click", () => loadDevices());
testBackupButton.addEventListener("click", async () => {
  backupTestMessage.textContent = "Testing saved path";
  const response = await window.sigitFetch("/api/settings/test-backup", { method: "POST" });
  const payload = await response.json();
  backupTestMessage.textContent = response.ok ? `Write test passed: ${payload.path}` : detailText(payload, "Write test failed");
});
loadSettings();
