"use strict";

const byId = (id) => document.getElementById(id);
const loginView = byId("login-view");
const appView = byId("app-view");
const loginForm = byId("login-form");
const configForm = byId("config-form");
const SPECTRUM_HISTORY_MS = 30_000;
const SPECTRUM_HISTORY_COLUMNS = 1801;
const SPECTRUM_MINIMUM_HZ = 20;
const SPECTRUM_MINIMUM_SPAN_HZ = 10;
const SPECTRUM_FREQUENCY_BIAS = 1.6;
const SPECTRUM_DELIVERY_HOLD_MS = 250;

const ui = {
  globalMessage: byId("global-message"),
  headerStatus: byId("header-status"),
  configState: byId("config-state"),
  device: byId("capture-device"),
  mode: byId("capture-mode"),
  modeHelp: byId("capture-mode-help"),
  representation: byId("stream-representation"),
  representationHelp: byId("representation-help"),
  streamEnabled: byId("stream-enabled"),
  destinationHost: byId("destination-host"),
  destinationPort: byId("destination-port"),
  latency: byId("srt-latency"),
  streamId: byId("stream-id"),
  passphrase: byId("srt-passphrase"),
  clearPassphrase: byId("passphrase-clear"),
  passphraseHelp: byId("passphrase-help"),
  bitrateField: byId("opus-bitrate-field"),
  bitrate: byId("opus-bitrate"),
  recordingDirectory: byId("recording-directory"),
  rotationSeconds: byId("rotation-seconds"),
  requiredMountpoint: byId("required-mountpoint"),
  spectrumRate: byId("spectrum-rate"),
  spectrumBands: byId("spectrum-bands"),
  warningPanel: byId("warning-panel"),
  warningList: byId("warning-list"),
  logList: byId("log-list"),
};

const state = {
  setupRequired: false,
  csrfToken: "",
  eventSource: null,
  capabilities: null,
  devicesById: new Map(),
  modesByKey: new Map(),
  representationMetadata: new Map(),
  config: null,
  revision: null,
  passphraseConfigured: false,
  telemetryFrame: null,
  mediaAvailable: null,
  mediaDataLoading: false,
  configSaving: false,
  busyButtons: new Set(),
  status: null,
  statusReceivedAt: 0,
  restartRequired: false,
  route: "dashboard",
  logPage: 0,
  logSignature: "",
  logEvents: [],
  storage: null,
  storagePage: 1,
  storageLoading: false,
  storageLoadedAt: 0,
  storageActionBusy: false,
  playbackId: null,
  maintenance: null,
  maintenanceLoading: false,
  maintenanceLoadedAt: 0,
  maintenancePending: null,
  preferences: {spectrumPalette: "classic", meterPalette: "classic", theme: "dark"},
  lastMeter: null,
  lastSpectrum: null,
  meterSequence: null,
  spectrumSequence: null,
  meterReceivedAt: 0,
  spectrumReceivedAt: 0,
  captureGeneration: null,
  spectrumColumns: [],
  spectrumView: null,
  spectrumViewLimit: null,
  spectrumRaster: null,
  deviceSample: null,
  deviceSequence: null,
  deviceReceivedAt: 0,
  deviceHistory: [],
};

class ApiError extends Error {
  constructor(status, payload) {
    const detail = payload?.error?.message ?? payload?.message ?? payload?.error;
    super(typeof detail === "string" ? detail : `Request failed (${status})`);
    this.status = status;
    this.payload = payload;
  }
}

async function api(path, {method = "GET", body, csrf = false} = {}) {
  const headers = {Accept: "application/json"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = state.csrfToken;
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const payload = response.status === 204 ? null : await response.json().catch(() => null);
  const failedReauthentication = method === "POST" && ["/api/maintenance", "/api/account/password"].includes(path)
    && payload?.error?.code === "invalid_login";
  if (response.status === 401 && path !== "/api/login" && !failedReauthentication) showLogin(state.maintenancePending
    ? "Device is responding again. Sign in to verify maintenance completed." : "Session expired. Sign in again.");
  if (!response.ok) throw new ApiError(response.status, payload);
  return payload;
}

function setMessage(message = "", kind = "status") {
  ui.globalMessage.textContent = message;
  ui.globalMessage.setAttribute("role", kind === "error" ? "alert" : "status");
}

function controlDisabledReason(action) {
  if (state.mediaAvailable !== true) return "Waiting for live media status.";
  const root = statusRoot(state.status);
  const [branch, operation] = action.split(".");
  if ([...state.busyButtons].some((button) => button.dataset.action.split(".")[0] === branch)) {
    return "A command is pending.";
  }
  if (["starting", "stopping"].includes(root[branch]?.state)) return "Waiting for the media transition.";
  if (operation === "start" || operation === "set_opus_bitrate") {
    if (state.restartRequired) return "Saved configuration requires a media restart.";
    if (!state.config) return "Load the configuration first.";
    if (root.service !== "running") return "The media service is not running.";
    if (branch !== "capture" && root.capture?.state !== "running") return "Start capture on the Audio page first.";
    if (branch === "stream" && !state.config.stream.enabled) return "Enable streaming in SRT settings first.";
    if (operation === "start" && root[branch]?.state === "running") return "Already running.";
    if (branch === "capture" && !state.config.capture.mode) return "Choose a validated capture mode first.";
    if (operation === "set_opus_bitrate") {
      const metadata = state.representationMetadata.get(root.stream?.representation_id);
      if (root.stream?.state !== "running" || !String(metadata?.codec).toLowerCase().includes("opus")) {
        return "Live bitrate control requires a running Opus stream.";
      }
      if (root.stream?.pending_opus_bitrate_bps != null) return "A bitrate change is pending.";
    }
  }
  if (operation === "stop") {
    if (["stopped", "blocked"].includes(root[branch]?.state)) return "Already stopped.";
    if (branch === "capture" && [root.stream?.state, root.recording?.state].some((value) => ["starting", "running", "stopping"].includes(value))) {
      return "Stop streaming and recording before stopping capture.";
    }
  }
  return "";
}

function syncControlAvailability() {
  syncSpectrumViewControls();
  const mediaReady = state.mediaAvailable === true;
  const configReady = mediaReady && state.config !== null;
  for (const control of configForm.elements) {
    // Set each control's final state once; enabling a fieldset or action first
    // and disabling it again interrupts native widgets on every snapshot.
    if (control.tagName === "FIELDSET" || control.dataset.action) continue;
    const disabled = !configReady || (control.id === "save-config" && state.configSaving)
      || (control.id === "audio-refresh" && state.audioRefreshing)
      || (control === ui.clearPassphrase && !state.passphraseConfigured)
      || (control === ui.passphrase && ui.clearPassphrase.checked);
    setProperty(control, "disabled", disabled);
  }
  for (const button of document.querySelectorAll("button[data-action]")) {
    const ready = configForm.contains(button) ? configReady : mediaReady;
    const reason = controlDisabledReason(button.dataset.action);
    setProperty(button, "disabled", !ready || Boolean(reason));
    setProperty(button, "title", reason);
  }
}

function renderMediaRequestFailure(error) {
  if (error instanceof ApiError && [502, 503].includes(error.status) && error.payload) {
    renderStatus(error.payload);
    return true;
  }
  return false;
}

function stopEvents() {
  state.eventSource?.close();
  state.eventSource = null;
}

function showLogin(message = "", setupRequired = false) {
  if (byId("confirm-dialog").open) byId("confirm-dialog").close("cancel");
  stopEvents();
  state.config = null;
  state.revision = null;
  state.status = null;
  state.mediaAvailable = false;
  state.statusReceivedAt = 0;
  clearTelemetry();
  clearDeviceTelemetry(true);
  state.csrfToken = "";
  state.storage = null;
  state.maintenance = null;
  state.maintenancePending = null;
  byId("maintenance-overlay").hidden = true;
  stopPlayback();
  for (const id of ["account-current-password", "account-new-password", "account-confirm-password", "confirm-current-password"]) byId(id).value = "";
  appView.hidden = true;
  loginView.hidden = false;
  state.setupRequired = setupRequired;
  byId("login-heading").textContent = setupRequired ? "Create your account" : "Sign in";
  byId("login-help").textContent = setupRequired ? "First-time setup · choose a username and password." : "Use your TinyPiRelay web account.";
  byId("setup-confirm-field").hidden = !setupRequired;
  byId("setup-help").hidden = !setupRequired;
  byId("setup-transport").hidden = !setupRequired || window.location.protocol === "https:";
  const confirm = byId("setup-confirm-password");
  confirm.disabled = !setupRequired;
  confirm.required = setupRequired;
  confirm.value = "";
  byId("password").autocomplete = setupRequired ? "new-password" : "current-password";
  byId("password").minLength = setupRequired ? 12 : 0;
  const submit = loginForm.querySelector("button[type=submit]");
  submit.textContent = setupRequired ? "Create account" : "Sign in";
  submit.disabled = false;
  byId("password").value = "";
  byId("login-message").textContent = message;
  byId("login-message").dataset.error = "false";
  byId("login-message").setAttribute("role", "status");
  byId("username").focus();
}

function showApp(session) {
  state.csrfToken = session.csrf_token;
  byId("password").value = "";
  byId("setup-confirm-password").value = "";
  byId("login-message").textContent = "";
  loginView.hidden = true;
  appView.hidden = false;
  setMessage(`Signed in as ${session.username}.`);
  setRoute();
  syncControlAvailability();
}

function option(value, label, {disabled = false} = {}) {
  const node = document.createElement("option");
  node.value = value;
  node.textContent = label;
  node.disabled = disabled;
  return node;
}

function modeLabel(mode) {
  const channels = mode.channels === 1 ? "Mono" : mode.channels === 2 ? "Stereo" : `${mode.channels} channels`;
  const rate = Number.isFinite(mode.rate_hz) ? `${mode.rate_hz / 1000} kHz` : "Unknown rate";
  const origin = mode.native ? "Native" : "Software converted";
  const evidence = typeof mode.evidence_status === "string" ? ` · ${mode.evidence_status}` : "";
  return `${channels} · ${rate} · ${mode.format ?? "Unknown format"} · ${origin}${evidence}`;
}

function activeDevice() {
  return state.devicesById.get(ui.device.value) ?? null;
}

function activeMode() {
  return state.modesByKey.get(ui.mode.value) ?? null;
}

function activeStreamOption() {
  const mode = activeMode();
  return mode?.stream_options?.find((item) => item.id === ui.representation.value) ?? null;
}

function sameMode(left, right) {
  if (!left || !right) return left === right;
  return left.format === right.format
    && left.rate_hz === right.rate_hz
    && left.channels === right.channels;
}

function populateCapabilities(capabilities) {
  state.capabilities = capabilities;
  state.devicesById.clear();
  state.representationMetadata.clear();
  for (const item of capabilities.stream_representations ?? []) {
    if (item && typeof item.id === "string") state.representationMetadata.set(item.id, item);
  }

  ui.device.replaceChildren(option("", "Set up audio later"));
  for (const device of capabilities.capture_devices ?? []) {
    if (!device || typeof device.id !== "string" || !Array.isArray(device.modes)) continue;
    state.devicesById.set(device.id, device);
    const presence = device.present === false ? " · not connected" : device.present === true ? " · connected" : "";
    ui.device.append(option(device.id, (device.label || device.id) + presence));
  }
  const discovery = capabilities.discovery;
  const count = discovery?.devices?.length ?? 0;
  setValue("audio-discovery-status", discovery?.status === "available"
    ? `${count} input${count === 1 ? "" : "s"} detected · only validated modes are selectable.`
    : "Device discovery unavailable · validated configurations remain listed.");
  renderAbout();
}

async function refreshAudioDevices() {
  if (state.audioRefreshing || !state.mediaAvailable) return;
  state.audioRefreshing = true;
  syncControlAvailability();
  try {
    const capabilities = await api("/api/capabilities");
    // Preserve the current draft, including edits made during the request.
    const device = ui.device.value, mode = selectedModeConfig(), representation = ui.representation.value;
    populateCapabilities(capabilities);
    ui.device.value = device;
    populateModes(mode);
    populateRepresentations(representation);
    setMessage("Audio inputs refreshed. No settings were saved.");
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "Audio refresh failed.", "error");
  } finally {
    state.audioRefreshing = false;
    syncControlAvailability();
  }
}

function populateModes(selectedMode = null) {
  state.modesByKey.clear();
  ui.mode.replaceChildren(option("", "Select a validated mode", {disabled: true}));
  const device = activeDevice();
  if (!device) {
    ui.modeHelp.textContent = "Audio setup can wait. Other settings remain available.";
    populateRepresentations();
    return;
  }

  device.modes.forEach((mode, index) => {
    if (!mode || !Number.isFinite(mode.rate_hz) || !Number.isFinite(mode.channels) || typeof mode.format !== "string") return;
    const key = `${device.id}:${index}`;
    state.modesByKey.set(key, mode);
    ui.mode.append(option(key, modeLabel(mode)));
    if (sameMode(mode, selectedMode)) ui.mode.value = key;
  });
  ui.modeHelp.textContent = "Modes are exact combinations; native capture is identified explicitly.";
  populateRepresentations();
}

function conversionLabel(conversion) {
  if (!conversion) return "No stream conversion reported";
  const label = typeof conversion === "string" ? conversion : conversion.label;
  if (typeof label === "string") {
    if (label.startsWith("Native 24-bit mono is converted to 16-bit stereo at 48 kHz before FLAC encoding.")) {
      return "Capture / recording: native 24-bit mono\nSRT: converted to 16-bit stereo · 48 kHz\nStream does not preserve 24-bit samples\nFinite Pi 4 validation only";
    }
    return label.replace("Native mono is channel-converted to stereo; format and rate are unchanged.", "Mono → stereo · original format and rate");
  }
  if (conversion.required === false) return "No stream conversion required";
  return "Software conversion required";
}

function populateRepresentations(selectedId = undefined) {
  const previous = selectedId === undefined ? ui.representation.value : selectedId;
  ui.representation.replaceChildren(option("", "Select an approved representation", {disabled: true}));
  const mode = activeMode();
  for (const streamOption of mode?.stream_options ?? []) {
    if (!streamOption || typeof streamOption.id !== "string") continue;
    const metadata = state.representationMetadata.get(streamOption.id) ?? {};
    ui.representation.append(option(streamOption.id, streamOption.label || metadata.label || streamOption.id));
  }
  if ([...ui.representation.options].some((item) => item.value === previous)) {
    ui.representation.value = previous;
  } else ui.representation.value = "";
  updateRepresentationDetails();
}

function updateRepresentationDetails() {
  const selected = activeStreamOption();
  const metadata = state.representationMetadata.get(ui.representation.value) ?? {};
  ui.representationHelp.title = "";
  if (!selected) {
    ui.representationHelp.textContent = "Select a capture mode to see its evidence-approved stream combinations.";
    ui.bitrateField.hidden = true;
    return;
  }
  const codecValue = selected.codec || metadata.codec;
  const containerValue = selected.container || metadata.container;
  const codec = typeof codecValue === "string" ? codecValue : codecValue?.label || codecValue?.name || "Unknown codec";
  const container = typeof containerValue === "string" ? containerValue : containerValue?.label || containerValue?.name || "Unknown container";
  const formatLine = document.createElement("span");
  formatLine.textContent = `${codec} in ${container}`;
  const conversionLines = conversionLabel(selected.conversion).split("\n").map(label => {
    const line = document.createElement("span");
    line.textContent = label;
    line.dataset.fit = "";
    return line;
  });
  ui.representationHelp.title = typeof selected.conversion === "string" ? selected.conversion : selected.conversion?.label || "";
  ui.representationHelp.replaceChildren(formatLine, ...conversionLines);

  const isOpus = String(codec).toLowerCase().includes("opus");
  ui.bitrateField.hidden = !isOpus;
  if (isOpus) {
    const current = Number(state.config?.stream?.opus_bitrate_bps) || 128000;
    ui.bitrate.replaceChildren();
    for (const bitrate of [64000, 96000, 128000, 192000]) {
      ui.bitrate.append(option(String(bitrate), `${bitrate / 1000} kbps`));
    }
    ui.bitrate.value = String(current);
  }
  scheduleTextFit();
}

function setNullableValue(element, value) {
  element.value = value === null || value === undefined ? "" : String(value);
}

function hydrateConfig(result) {
  const config = result.config;
  state.config = structuredClone(config);
  state.revision = result.revision;
  state.passphraseConfigured = Boolean(config.stream.passphrase?.configured);
  state.restartRequired = result.restart_required === true;

  ui.device.value = config.capture.device_id ?? "";
  populateModes(config.capture.mode);
  populateRepresentations(config.stream.representation_id ?? "");
  ui.streamEnabled.checked = config.stream.enabled;
  setNullableValue(ui.destinationHost, config.stream.destination_host);
  setNullableValue(ui.destinationPort, config.stream.destination_port);
  setNullableValue(ui.latency, config.stream.latency_ms);
  setNullableValue(ui.streamId, config.stream.stream_id);
  ui.passphrase.value = "";
  ui.passphrase.disabled = false;
  ui.clearPassphrase.checked = false;
  ui.clearPassphrase.disabled = !state.passphraseConfigured;
  ui.passphraseHelp.textContent = state.passphraseConfigured
    ? "Passphrase stored · leave blank to keep it."
    : "Use 10–79 printable ASCII characters.";
  setNullableValue(ui.recordingDirectory, config.recording.directory);
  setNullableValue(ui.rotationSeconds, config.recording.rotation_seconds);
  setNullableValue(ui.requiredMountpoint, config.recording.required_mountpoint);
  setNullableValue(ui.spectrumRate, config.monitoring.spectrum_updates_per_second);
  setNullableValue(ui.spectrumBands, config.monitoring.spectrum_bands);
  syncSpectrumViewControls(true);
  syncStreamRequirements();
  syncControlAvailability();
  ui.configState.textContent = result.restart_required
    ? "Saved configuration is waiting for a media restart."
    : `Configuration revision ${result.revision} loaded.`;
  if (state.status) renderDashboard(state.status, statusRoot(state.status));
}

function syncStreamRequirements() {
  for (const input of [ui.representation, ui.destinationHost, ui.destinationPort]) {
    input.required = ui.streamEnabled.checked;
  }
}

async function loadApplicationData() {
  const [status, capabilities, config] = await Promise.all([
    api("/api/status"),
    api("/api/capabilities"),
    api("/api/config"),
  ]);
  populateCapabilities(capabilities);
  hydrateConfig(config);
  renderStatus(status);
}

function statusRoot(payload) {
  const status = payload?.status ?? payload;
  return status?.state ?? status?.media?.state ?? status ?? {};
}

function stateText(value, fallback = "Unavailable") {
  if (typeof value !== "string" || !value) return fallback;
  return value.replaceAll("_", " ");
}

function errorMessage(value) {
  return typeof value?.message === "string" ? value.message : "";
}

function firstError(payload, root) {
  const candidates = [
    payload?.error,
    payload?.runtime_error,
    root.connection?.last_error,
    root.stream?.last_error,
    root.capture?.last_error,
    root.recording?.last_error,
    root.monitoring?.last_error,
  ];
  return candidates.find((item) => errorMessage(item)) ?? null;
}

const SAFE_SRT_STATS = new Set([
  "send-rate-mbps", "bandwidth-mbps", "rtt-ms", "bytes-sent-total",
  "bytes-retransmitted-total", "bytes-sent-dropped-total", "packets-sent-total",
  "packets-sent-lost", "packets-retransmitted", "packets-ack-received",
  "packets-nack-received", "send-duration-us",
]);

function formatSrtStatistics(statistics) {
  if (!statistics || typeof statistics !== "object") return "Unavailable";
  const parts = [];
  for (const [key, value] of Object.entries(statistics)) {
    if (!SAFE_SRT_STATS.has(key) || !["string", "number", "boolean"].includes(typeof value)) continue;
    parts.push(`${key.replaceAll("_", " ")}: ${value}`);
  }
  return parts.length ? parts.join(" · ") : "No statistics reported";
}

function renderWarnings(payload, root) {
  const warnings = [];
  for (const item of Array.isArray(payload?.warnings) ? payload.warnings : []) {
    const message = typeof item === "string" ? item : errorMessage(item);
    if (message) warnings.push(message);
  }
  for (const item of [payload?.error, root.recording?.warning, root.recording?.last_error, root.connection?.last_error]) {
    const message = errorMessage(item);
    if (message && !warnings.includes(message)) warnings.push(message);
  }
  ui.warningList.replaceChildren(...warnings.slice(0, 20).map((message) => {
    const item = document.createElement("li");
    item.textContent = message;
    return item;
  }));
  ui.warningPanel.hidden = warnings.length === 0;
}

function filteredLogs() {
  const query = (byId("logs-filter")?.value || "").toLowerCase();
  const level = byId("logs-level")?.value || "all";
  return state.logEvents.filter((entry) => {
    const text = typeof entry === "string" ? entry : `${entry?.code || ""} ${entry?.message || ""}`;
    return text.toLowerCase().includes(query) && (level === "all" || entry?.level === level || level === "warning" && entry?.level === "warn");
  });
}

function renderLogs(payload, force = false) {
  const reportedEvents = Array.isArray(payload?.logs)
    ? payload.logs
    : Array.isArray(payload?.diagnostics?.events)
      ? payload.diagnostics.events
      : [];
  state.logEvents = reportedEvents.slice(-32).reverse();
  const logs = filteredLogs();
  const signature = JSON.stringify(logs);
  if (!force && state.logSignature === signature) return;
  state.logSignature = signature;
  const pages = Math.max(1, Math.ceil(logs.length / 4));
  state.logPage = Math.min(state.logPage, pages - 1);
  setValue("logs-page", `${state.logPage + 1} / ${pages}`);
  if (byId("logs-previous")) byId("logs-previous").disabled = state.logPage === 0;
  if (byId("logs-next")) byId("logs-next").disabled = state.logPage >= pages - 1;
  if (!logs.length) {
    const item = document.createElement("li");
    item.textContent = "No events reported.";
    ui.logList.replaceChildren(item);
    return;
  }
  ui.logList.replaceChildren(...logs.slice(state.logPage * 4, state.logPage * 4 + 4).map((entry) => {
    const item = document.createElement("li");
    if (typeof entry === "string") {
      item.textContent = entry;
    } else {
      const timestamp = document.createElement("time");
      const date = Number.isFinite(entry?.timestamp_unix) ? new Date(entry.timestamp_unix * 1000) : null;
      timestamp.textContent = date && Number.isFinite(date.getTime()) ? date.toISOString().slice(11, 19) : "—";
      const code = typeof entry?.code === "string"
        ? `[${entry.code}] `
        : typeof entry?.level === "string"
          ? `[${entry.level}] `
          : "";
      const codeNode = document.createElement("strong");
      codeNode.textContent = code;
      const message = document.createElement("span");
      message.textContent = errorMessage(entry) || "Event";
      message.dataset.fit = "";
      item.append(timestamp, codeNode, message);
    }
    return item;
  }));
  scheduleTextFit();
}

function setValue(id, value) {
  setProperty(byId(id), "textContent", String(value));
}

// Live renderers update leaves, not control subtrees. Reuse these for future
// controls: unchanged DOM writes can reset native popup/caret/press state.
function setProperty(element, property, value) {
  if (element && element[property] !== value) element[property] = value;
}

function setAttribute(element, name, value) {
  if (!element) return;
  const text = value === null ? null : String(value);
  if (element.getAttribute(name) === text) return;
  if (text === null) element.removeAttribute(name);
  else element.setAttribute(name, text);
}

function renderDetailTelemetry() {
  const sample = state.mediaAvailable ? state.deviceSample : null;
  const {system, device, network, storage} = sample || {};
  const percent = system?.memory_total_bytes > 0 && nonnegativeNumber(system?.memory_used_bytes) !== null
    ? 100 * system.memory_used_bytes / system.memory_total_bytes : null;
  const copy = (target, source) => setValue(target, byId(source)?.textContent || "—");
  for (const [target, source] of [
    ["detail-cpu", "system-cpu"], ["detail-temperature", "system-temperature"], ["detail-memory-bytes", "system-memory"],
    ["detail-uptime", "system-uptime"], ["detail-load", "system-load"], ["detail-hostname", "device-hostname"],
    ["detail-model", "device-model"], ["detail-os", "device-os"], ["detail-network-tx", "network-tx"],
    ["detail-network-rx", "network-rx"], ["detail-network-interface", "network-interface"],
    ["detail-network-address", "network-address"], ["detail-network-receiver", "network-receiver"],
    ["library-used", "storage-percent"], ["library-free", "storage-free"], ["library-filesystem", "storage-filesystem"],
  ]) copy(target, source);
  setValue("detail-memory", percent === null ? "—" : `${percent.toFixed(0)}%`);
  if (byId("memory-meter")) {
    byId("memory-meter").value = percent ?? 0;
    byId("memory-meter").hidden = percent === null;
  }
  setValue("detail-network-state", network?.operstate || "—");
  setValue("detail-network-kind", network?.kind === "wifi" ? "Wi-Fi" : network?.kind === "ethernet" ? "Ethernet" : "—");
  setValue("detail-network-speed", nonnegativeNumber(network?.speed_mbps) === null ? "—" : `${network.speed_mbps} Mbps`);
  setValue("detail-network-duplex", network?.duplex || "—");
  setValue("settings-config-state", !state.config ? "Unavailable" : state.restartRequired ? "Restart required" : "Applied");
  setValue("settings-active-revision", state.status?.active_config_revision || "—");
  setValue("settings-saved-revision", state.status?.saved_config_revision || state.revision || "—");
}

function renderAbout() {
  const device = state.deviceSample?.device;
  setValue("about-version", device?.software_version || "—");
  setValue("about-architecture", device?.architecture || "—");
  setValue("about-devices", state.capabilities ? state.devicesById.size : "—");
  setValue("about-representations", state.capabilities ? state.representationMetadata.size : "—");
  const list = byId("about-capabilities");
  if (!list) return;
  const rows = [...state.representationMetadata.values()].slice(0, 8);
  list.replaceChildren();
  if (!rows.length) {
    const term = document.createElement("dt"); term.textContent = "Media capabilities";
    const detail = document.createElement("dd"); detail.textContent = "Unavailable";
    list.append(term, detail);
  }
  for (const item of rows) {
    const term = document.createElement("dt"); term.textContent = item.codec || item.id;
    const detail = document.createElement("dd"); detail.dataset.fit = ""; detail.textContent = item.label || item.id;
    list.append(term, detail);
  }
}

function stopPlayback() {
  const player = byId("recording-player");
  if (player?.pause) player.pause();
  player?.removeAttribute("src");
  if (player?.load) player.load();
  state.playbackId = null;
  setValue("playback-name", "No recording selected");
}

function fileUrl(file, download = false) {
  return `/api/storage/file?id=${encodeURIComponent(file.file_id)}${download ? "&download=1" : ""}`;
}

function fileActionButton(label, icon, callback, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.title = label;
  button.setAttribute("aria-label", label);
  button.disabled = disabled;
  const graphic = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  graphic.setAttribute("class", "icon"); graphic.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use"); use.setAttribute("href", `#icon-${icon}`);
  graphic.append(use); button.append(graphic);
  button.addEventListener("click", callback);
  return button;
}

function renderStorage() {
  const list = byId("file-list");
  const data = state.storage;
  setValue("library-count", Number.isInteger(data?.total) ? data.total : "—");
  const items = Array.isArray(data?.items) ? data.items : [];
  const existing = new Map([...list.children].filter((row) => row._file).map((row) => [row._file.file_id, row]));
  const rows = [];
  for (const file of items) {
    let row = existing.get(file.file_id);
    if (!row) {
      row = document.createElement("li");
      const name = document.createElement("span"); name.className = "file-name";
      const filename = document.createElement("strong"); filename.dataset.fit = "";
      const modified = document.createElement("small"); name.append(filename, modified);
      const duration = document.createElement("span"), size = document.createElement("span");
      const status = document.createElement("span"); status.className = "file-state";
      const actions = document.createElement("div"); actions.className = "file-actions";
      const play = fileActionButton(`Play ${file.name}`, "play", async () => {
        const current = row._file;
        if (!current.can_play || state.storageActionBusy) return;
        const player = byId("recording-player");
        state.playbackId = current.file_id;
        player.src = fileUrl(current);
        setValue("playback-name", current.name);
        setValue("playback-message", "FLAC file · browser playback and seeking · no transcoding.");
        scheduleTextFit();
        try { await player.play(); } catch { setValue("playback-message", "Playback could not start. Try the player controls or download the original file."); }
      });
      const download = document.createElement("a"); download.className = "button secondary"; download.textContent = "↓";
      download.title = `Download ${file.name}`; download.setAttribute("aria-label", download.title);
      const checkButton = document.createElement("button"); checkButton.type = "button"; checkButton.className = "secondary"; checkButton.textContent = "Check";
      checkButton.title = "Decode-check the original file without modifying it";
      checkButton.addEventListener("click", () => runStorageAction("check", row._file));
      const recover = fileActionButton(`Recover ${file.name}`, "restart", () => runStorageAction("recover", row._file));
      const remove = fileActionButton(`Delete ${file.name}`, "trash", () => runStorageAction("delete", row._file));
      remove.className = "danger";
      actions.append(play, download, checkButton, recover, remove);
      row.append(name, duration, size, status, actions);
      row._parts = {name, filename, modified, duration, size, status, play, download, checkButton, recover, remove};
    }
    // Keep each control bound to one file version; refresh metadata without replacing its DOM node.
    row._file = file;
    const {name, filename, modified, duration, size, status, play, download, checkButton, recover, remove} = row._parts;
    setProperty(name, "title", file.name);
    setProperty(filename, "textContent", file.name);
    const date = Number.isFinite(file.modified_unix) ? new Date(file.modified_unix * 1000) : null;
    setProperty(modified, "textContent", date && Number.isFinite(date.getTime()) ? `${date.toISOString().slice(0, 19).replace("T", " ")} UTC` : "Date unavailable");
    setProperty(duration, "textContent", elapsedClock(file.duration_seconds));
    setProperty(size, "textContent", formatBytes(file.size_bytes));
    const check = file.check?.state;
    setAttribute(status, "data-state", check && check !== "unchecked" ? check : file.status);
    setProperty(status, "textContent", file.status === "active" ? "Recording" : check === "passed" ? "Checked ✓" : check === "decoded" ? "Decode only" : check === "checking" ? "Checking…"
      : check === "recovering" ? "Recovering…" : check === "recovered" ? "Copy recovered"
      : ["failed", "timeout", "unavailable", "cancelled"].includes(check) ? stateText(check) : file.status === "needs_check" ? "Needs checking" : "Finalized");
    setProperty(status, "title", [file.check?.message, file.check?.output_name].filter(Boolean).join(" · ") || "Finalized does not mean decode-checked.");
    setProperty(play, "disabled", !file.can_play || state.storageActionBusy);
    setProperty(download, "hidden", !file.can_download);
    setAttribute(download, "href", file.can_download ? fileUrl(file, true) : null);
    const jobBusy = ["checking", "recovering"].includes(check);
    setProperty(checkButton, "disabled", !file.can_check || state.storageActionBusy || jobBusy);
    setProperty(recover, "disabled", !file.can_recover || state.storageActionBusy || jobBusy);
    const recoveryReason = state.storageActionBusy || jobBusy ? "A file command is pending." : file.recovery_unavailable_reason
      || (!file.can_recover ? "Recovery is unavailable for this file." : "Create a separate recovered FLAC copy; the original is kept.");
    setProperty(recover, "title", recoveryReason);
    setAttribute(recover, "aria-description", recoveryReason);
    setProperty(remove, "disabled", !file.can_delete || state.storageActionBusy);
    rows.push(row);
  }
  if (!items.length) {
    const empty = [...list.children].find((row) => !row._file) || document.createElement("li");
    setProperty(empty, "className", "empty-row");
    setProperty(empty, "textContent", data ? "No recording files on this page." : "Recording library unavailable."); rows.push(empty);
  }
  const pagination = byId("storage-pagination");
  const existingPages = new Map([...pagination.children].map((button) => [button._pageKey, button]));
  const pageButtons = [];
  const page = data?.page || 1, pages = Math.max(1, data?.total_pages || 1);
  const addPage = (label, number, disabled = false) => {
    const key = `${label}:${number}`;
    let button = existingPages.get(key);
    if (!button) {
      button = document.createElement("button"); button.type = "button"; button.className = "secondary"; button.textContent = label;
      button._pageKey = key;
      button.setAttribute("aria-label", label === "←" ? "Previous recording page" : label === "→" ? "Next recording page" : `Recording page ${number}`);
      button.addEventListener("click", () => {
        if (state.storageLoading || button.disabled) return;
        state.storagePage = number; loadStorage();
      });
    }
    setProperty(button, "disabled", disabled || state.storageLoading);
    setAttribute(button, "aria-current", label === String(page) ? "page" : null);
    pageButtons.push(button);
  };
  addPage("←", page - 1, page <= 1);
  const first = Math.max(1, Math.min(page - 2, pages - 4));
  for (let number = first; number <= Math.min(pages, first + 4); number += 1) addPage(String(number), number);
  addPage("→", page + 1, page >= pages);
  for (const [parent, children] of [[list, rows], [pagination, pageButtons]]) {
    for (const child of [...parent.children]) if (!children.includes(child)) child.remove();
    children.forEach((child, index) => {
      if (parent.children[index] !== child) parent.insertBefore(child, parent.children[index] || null);
    });
  }
  scheduleTextFit();
}

async function loadStorage() {
  if (!state.csrfToken || state.storageLoading) return;
  state.storageLoading = true;
  state.storageLoadedAt = performance.now();
  const token = state.csrfToken;
  try {
    const data = await api(`/api/storage?page=${state.storagePage}`);
    if (token !== state.csrfToken) return;
    const checks = data.check ? [data.check] : (data.items ?? []).map((file) => ({...file.check, file_id: file.file_id}));
    for (const check of checks) {
      const previous = state.storage?.check?.file_id === check.file_id ? state.storage.check
        : state.storage?.items?.find((item) => item.file_id === check.file_id)?.check;
      if (previous?.state === "recovering" && check.state !== "recovering") {
        setMessage([check.message || "Recovery ended; review the file state.", check.output_name].filter(Boolean).join(" · "),
          check.state === "recovered" ? "status" : "error");
      }
    }
    state.storage = data;
    state.storagePage = data.page || 1;
    state.storageLoadedAt = performance.now();
    setValue("library-message", `${data.total ?? 0} files · originals remain on the Pi`);
  } catch (error) {
    if (token !== state.csrfToken) return;
    state.storage = null;
    setValue("library-message", error instanceof Error ? error.message : "File listing failed.");
  } finally {
    state.storageLoading = false;
    renderStorage();
  }
}

async function runStorageAction(action, file) {
  if (state.storageActionBusy || !file[`can_${action}`]) return;
  const message = action === "delete" ? `Permanently delete ${file.name}? This cannot be undone.`
    : action === "recover" ? `Attempt recovery of ${file.name}?`
    : `Decode-check ${file.name}? This reads the full file and uses Pi resources; the original is not changed.`;
  const details = action === "recover" ? [
    "Creates a separately named FLAC copy; the original is kept unchanged.",
    "Damaged audio may be missing or contain gaps. Recovery can fail.",
    "Uses CPU and disk space. Recording must stay stopped during recovery.",
    "Up to five minutes / 1 GiB; larger files may need desktop recovery.",
  ] : [];
  if (!await confirmAction(message, {details})) return;
  const current = state.storage?.items?.find((item) => item.file_id === file.file_id);
  if (state.storageActionBusy || !current?.[`can_${action}`]) {
    setMessage("File state changed; refresh and review the current file before trying again.", "error");
    return;
  }
  state.storageActionBusy = true; renderStorage();
  try {
    if (state.playbackId === file.file_id) stopPlayback();
    const result = await api("/api/storage/action", {method: "POST", csrf: true, body: {action, file_id: file.file_id}});
    if (action === "recover" && result.check) {
      current.check = result.check;
      state.storage.check = {...result.check, file_id: file.file_id};
    }
    if (result.accepted === false) setMessage(result.check?.message || result.error?.message || "File action unavailable; the original was not changed.", "error");
    else setMessage(action === "delete" ? result.durability_confirmed === false
      ? "Recording deleted, but durable storage was not confirmed. Check device storage; do not retry the deletion."
      : "Recording permanently deleted; it cannot be recovered through TinyPiRelay."
      : action === "recover" ? [result.check?.message || "Recovery requested; its result will appear in the library.", result.check?.output_name].filter(Boolean).join(" · ")
        : "File check requested; its result will appear in the library.");
    if (action === "delete" && state.storage?.items?.length === 1 && state.storagePage > 1) state.storagePage -= 1;
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "File action failed.", "error");
  } finally {
    state.storageActionBusy = false;
    renderStorage();
    await loadStorage();
  }
}

function syncMaintenanceControls() {
  const available = state.maintenance?.available === true;
  const busy = state.maintenance?.busy === true || Boolean(state.maintenancePending);
  for (const button of document.querySelectorAll("button[data-maintenance]")) setProperty(button, "disabled", !available || busy);
  setValue("maintenance-state", !state.maintenance ? "Checking maintenance availability…"
    : !available ? state.maintenance.reason || "Maintenance helper unavailable; commands are disabled."
      : state.maintenance.error?.message || (busy ? `Maintenance: ${stateText(state.maintenance.phase)}` : "Maintenance ready · password confirmation required"));
}

async function loadMaintenance() {
  if (!state.csrfToken || state.maintenanceLoading) return;
  state.maintenanceLoading = true;
  const token = state.csrfToken;
  state.maintenanceLoadedAt = performance.now();
  try {
    const result = await api("/api/maintenance");
    if (token !== state.csrfToken) return;
    state.maintenance = result;
    const pending = state.maintenancePending;
    if (pending) {
      if (result.phase === "failed") {
        state.maintenancePending = null; byId("maintenance-overlay").hidden = true;
        setMessage(result.error?.message || "Maintenance failed; review device state.", "error");
      } else if (pending.action === "system.reboot" && result.boot_id && pending.boot_id && result.boot_id !== pending.boot_id
          || pending.action === "media.restart" && result.phase === "complete" && result.operation_id === pending.operation_id) {
        await loadApplicationData();
        if (state.mediaAvailable && !state.restartRequired) {
          state.maintenancePending = null; byId("maintenance-overlay").hidden = true;
          setMessage("Device reconnected; active media configuration verified.");
        }
      } else {
        setValue("maintenance-progress", pending.action === "system.shutdown" ? "Shutdown requested. The device will remain offline until powered on."
          : `Device reports ${stateText(result.phase)} · waiting for verified reconnect`);
      }
    }
  } catch (error) {
    if (!state.maintenancePending) state.maintenance = {available: false, reason: error instanceof Error ? error.message : "Maintenance unavailable"};
    else setValue("maintenance-progress", "Connection unavailable · waiting for the device to return…");
  } finally {
    state.maintenanceLoading = false;
    syncMaintenanceControls();
  }
}

async function runMaintenance(action) {
  if (!state.maintenance?.available || state.maintenance.busy || state.maintenancePending) return;
  const label = action === "media.restart" ? "Restart media and apply saved settings" : action === "system.reboot" ? "Reboot this device" : "Shut down this device";
  if (!await confirmAction(`${label}? Recording will finalize and stop; saved startup settings determine what resumes.`, {password: true})) {
    byId("confirm-current-password").value = ""; return;
  }
  const current_password = byId("confirm-current-password").value;
  byId("confirm-current-password").value = "";
  const boot_id = state.maintenance.boot_id;
  state.maintenancePending = {action, boot_id, operation_id: null}; syncMaintenanceControls();
  try {
    const result = await api("/api/maintenance", {method: "POST", csrf: true, body: {action, current_password, confirm: true}});
    if (result.accepted !== true) throw new Error(result.error?.message || "Maintenance was not accepted.");
    state.maintenancePending.operation_id = result.operation_id;
    state.maintenance = result;
    stopPlayback();
    byId("maintenance-overlay").hidden = false;
    setValue("maintenance-heading", action === "system.shutdown" ? "Shutting down" : "Pending reconnect");
    setValue("maintenance-progress", "Maintenance accepted · finalizing recording safely…");
  } catch (error) {
    state.maintenancePending = null;
    setMessage(error instanceof Error ? error.message : "Maintenance failed.", "error");
  } finally { syncMaintenanceControls(); }
}

function confirmAction(message, {password = false, details = []} = {}) {
  const dialog = byId("confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  setValue("confirm-message", message);
  byId("confirm-details").replaceChildren(...details.map((text) => {
    const line = document.createElement("p"); line.dataset.fit = ""; line.textContent = text; return line;
  }));
  byId("confirm-details").hidden = details.length === 0;
  byId("confirm-password-field").hidden = !password;
  byId("confirm-current-password").disabled = !password;
  byId("confirm-current-password").required = password;
  byId("confirm-current-password").value = "";
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
    dialog.showModal();
    scheduleTextFit();
  });
}

function setStateValue(id, value, fallback = "Unavailable") {
  const element = byId(id);
  if (!element) return;
  element.textContent = stateText(value, fallback);
  element.dataset.state = value ?? "unavailable";
}

function channelLabel(channels) {
  return channels === 1 ? "Mono" : channels === 2 ? "Stereo" : Number.isInteger(channels) ? `${channels} channels` : "—";
}

function nonnegativeNumber(value) {
  return Number.isFinite(value) && value >= 0 ? value : null;
}

function formatBytes(value, perSecond = false) {
  if (nonnegativeNumber(value) === null) return "—";
  const units = ["B", "kB", "MB", "GB", "TB", "PB"];
  const index = value > 0 ? Math.max(0, Math.min(units.length - 1, Math.floor(Math.log10(value) / 3))) : 0;
  const amount = value / 1000 ** index;
  return `${amount.toFixed(index === 0 ? 0 : amount >= 100 ? 0 : 1)} ${units[index]}${perSecond ? "/s" : ""}`;
}

function elapsedClock(seconds) {
  if (nonnegativeNumber(seconds) === null) return "—";
  const whole = Math.floor(seconds);
  return [Math.floor(whole / 3600), Math.floor(whole % 3600 / 60), whole % 60]
    .map((part) => String(part).padStart(2, "0")).join(":");
}

function compactDuration(seconds) {
  if (nonnegativeNumber(seconds) === null) return "—";
  if (seconds < 60) return "<1m";
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  return hours >= 24 ? `${Math.floor(hours / 24)}d ${hours % 24}h`
    : hours ? `${hours}h ${minutes % 60}m` : `${minutes}m`;
}

function clearDeviceTelemetry(resetSequence = false) {
  state.deviceSample = null;
  state.deviceReceivedAt = 0;
  state.deviceHistory = [];
  if (resetSequence) state.deviceSequence = null;
  renderDeviceTelemetry();
  renderRecordingTelemetry(state.status, statusRoot(state.status));
}

function updateDeviceTelemetry(payload) {
  const sample = payload?.device_telemetry;
  const valid = state.mediaAvailable === true
    && Number.isInteger(sample?.sequence) && sample.sequence >= 0
    && Number.isFinite(sample?.sampled_at_unix)
    && typeof payload?.active_config_revision === "string"
    && sample.config_revision === payload.active_config_revision;
  if (!valid) {
    if (state.deviceSample) clearDeviceTelemetry();
    return;
  }
  if (sample.sequence === state.deviceSequence) {
    checkDeviceTelemetryFreshness();
    return;
  }
  const now = performance.now();
  if (state.deviceSequence !== null && sample.sequence < state.deviceSequence) state.deviceHistory = [];
  state.deviceSequence = sample.sequence;
  state.deviceReceivedAt = now;
  state.deviceSample = sample;
  state.deviceHistory.push({
    time: now,
    cpu: nonnegativeNumber(sample.system?.cpu_percent),
    temperature: Number.isFinite(sample.system?.temperature_c) ? sample.system.temperature_c : null,
    diskRead: nonnegativeNumber(sample.storage?.read_bytes_per_second),
    diskWrite: nonnegativeNumber(sample.storage?.write_bytes_per_second),
    networkRx: nonnegativeNumber(sample.network?.rx_bytes_per_second),
    networkTx: nonnegativeNumber(sample.network?.tx_bytes_per_second),
    storageKey: `${sample.config_revision}:${sample.storage?.device ?? ""}:${sample.storage?.mountpoint ?? ""}`,
    networkKey: sample.network?.interface ?? null,
  });
  // One shared 2 s sample; retain at most 32 points, never an unbounded chart history.
  state.deviceHistory = state.deviceHistory.filter((point) => now - point.time <= 60000).slice(-32);
  renderDeviceTelemetry();
}

function checkDeviceTelemetryFreshness(now = performance.now()) {
  if (state.deviceSample && now - state.deviceReceivedAt > 7000) clearDeviceTelemetry();
}

function renderDeviceTelemetry() {
  const sample = state.mediaAvailable ? state.deviceSample : null;
  const device = sample?.device;
  const system = sample?.system;
  const storage = sample?.storage;
  const network = sample?.network;
  for (const [id, value] of [
    ["device-hostname", device?.hostname], ["device-model", device?.model],
    ["device-os", device?.os], ["device-architecture", device?.architecture],
    ["software-version", device?.software_version],
  ]) setValue(id, typeof value === "string" && value ? value : "—");
  setValue("system-cpu", nonnegativeNumber(system?.cpu_percent) !== null ? `${system.cpu_percent.toFixed(0)}%` : "—");
  setValue("system-temperature", Number.isFinite(system?.temperature_c) ? `${system.temperature_c.toFixed(1)}°C` : "—");
  const load = system?.load_average;
  setValue("system-load", Array.isArray(load) && load.length === 3 && load.every((value) => nonnegativeNumber(value) !== null)
    ? load.map((value) => value.toFixed(2)).join(" · ") : "—");
  setValue("system-memory", nonnegativeNumber(system?.memory_used_bytes) !== null && nonnegativeNumber(system?.memory_total_bytes) !== null
    ? `${formatBytes(system.memory_used_bytes)} / ${formatBytes(system.memory_total_bytes)}` : "—");
  setValue("system-uptime", compactDuration(system?.uptime_seconds));

  const percent = nonnegativeNumber(storage?.used_percent) !== null && storage.used_percent <= 100 ? storage.used_percent : null;
  setValue("storage-percent", percent === null ? "—" : `${percent.toFixed(0)}%`);
  setValue("storage-capacity", nonnegativeNumber(storage?.used_bytes) !== null && nonnegativeNumber(storage?.total_bytes) !== null
    ? `${formatBytes(storage.used_bytes)} / ${formatBytes(storage.total_bytes)}` : "Unavailable");
  setValue("storage-free", formatBytes(storage?.available_bytes));
  setValue("storage-filesystem", typeof storage?.filesystem === "string" && storage.filesystem ? storage.filesystem : "—");
  setValue("storage-recordings", nonnegativeNumber(storage?.recordings_bytes) === null ? "—"
    : `${storage.recordings_complete === true ? "" : "≥ "}${formatBytes(storage.recordings_bytes)}`);
  byId("storage-recordings").title = storage?.recordings_complete === true
    ? "Folder FLAC byte total; refreshed every 30 seconds, not allocated filesystem blocks."
    : "Incomplete folder scan; displayed FLAC bytes are a lower bound. Refreshed every 30 seconds.";
  setValue("storage-read", formatBytes(storage?.read_bytes_per_second, true));
  setValue("storage-write", formatBytes(storage?.write_bytes_per_second, true));
  setValue("storage-state", !sample ? "Unavailable" : storage?.safe === true ? "Writable" : storage?.safe === false ? "Blocked" : "Unavailable");
  byId("storage-state").dataset.state = storage?.safe === false ? "blocked" : "unknown";
  byId("storage-state").title = typeof storage?.reason === "string" ? storage.reason : "Recording filesystem status unavailable";
  byId("storage-ring-progress").setAttribute("stroke-dasharray", `${percent ?? 0} 100`);
  const capacity = byId("storage-usage");
  capacity.style.setProperty("--used-percent", `${percent ?? 0}%`);
  capacity.dataset.available = String(percent !== null);
  capacity.setAttribute("role", percent === null ? "img" : "meter");
  capacity.setAttribute("aria-label", percent === null ? "Recording filesystem usage unavailable" : "Recording filesystem usage");
  if (percent === null) {
    for (const name of ["aria-valuenow", "aria-valuemin", "aria-valuemax"]) capacity.removeAttribute(name);
  } else {
    capacity.setAttribute("aria-valuemin", "0");
    capacity.setAttribute("aria-valuemax", "100");
    capacity.setAttribute("aria-valuenow", String(percent));
  }

  setValue("network-interface", typeof network?.interface === "string" ? network.interface : "—");
  setValue("network-address", typeof network?.address === "string" ? network.address : "—");
  const kind = network?.kind === "wifi" ? "Wi-Fi" : network?.kind === "ethernet" ? "Ethernet" : null;
  const speed = nonnegativeNumber(network?.speed_mbps) !== null ? `${network.speed_mbps} Mbps` : null;
  setValue("network-link", network?.operstate === "down" ? "Down" : [kind, speed].filter(Boolean).join(" · ") || "—");
  byId("network-link").title = [network?.operstate, network?.duplex].filter((part) => typeof part === "string").join(" · ") || "Link information unavailable";
  setValue("network-tx", nonnegativeNumber(network?.tx_bytes_per_second) !== null ? `${(network.tx_bytes_per_second * 8 / 1e6).toFixed(2)} Mbps` : "—");
  setValue("network-rx", nonnegativeNumber(network?.rx_bytes_per_second) !== null ? `${(network.rx_bytes_per_second * 8 / 1e6).toFixed(2)} Mbps` : "—");
  renderDeviceHistories();
  renderDetailTelemetry();
  renderAbout();
  scheduleTextFit();
}

function renderRecordingTelemetry(payload, root) {
  const recording = state.mediaAvailable ? root.recording : null;
  const file = recording?.current_file || recording?.last_finalized_file;
  const sample = state.deviceSample;
  const matches = state.mediaAvailable && sample && typeof file === "string" && typeof payload?.active_config_revision === "string"
    && sample.config_revision === payload.active_config_revision && sample.recording?.file === file;
  const runtimeElapsed = typeof payload?.active_config_revision === "string" && typeof file === "string" && recording?.elapsed_file === file
    ? nonnegativeNumber(recording.elapsed_seconds) : null;
  const elapsed = runtimeElapsed ?? (matches ? nonnegativeNumber(sample.recording.elapsed_seconds) : null);
  setValue("recording-file", typeof file === "string" ? file.split("/").pop() : "—");
  byId("recording-file").title = typeof file === "string" ? `${recording.current_file ? "Current fragment" : "Last finalized fragment"}: ${file}` : "No recording file reported";
  setValue("recording-file-label", recording?.current_file ? "File" : recording?.last_finalized_file ? "Last file" : "File");
  setValue("recording-size", matches ? formatBytes(sample.recording.size_bytes) : "—");
  setValue("recording-duration", elapsedClock(elapsed));
  byId("recording-duration").title = "Elapsed wall time for this fragment; not sample-exact decoded audio duration.";
  setValue("storage-eta", !state.mediaAvailable || !state.deviceSample ? "Unavailable"
    : recording?.state !== "running" ? "Not recording"
      : matches && nonnegativeNumber(sample.storage?.seconds_until_limit) !== null
        ? sample.storage.seconds_until_limit === 0 ? "Limit reached" : `~${compactDuration(sample.storage.seconds_until_limit)}` : "Unavailable");
}

function renderDeviceHistories() {
  if (!["dashboard", "system", "network"].includes(state.route)) return;
  const history = state.deviceSample ? state.deviceHistory : [];
  const now = performance.now();
  for (const [id, fields, fixedMaximum, scope] of [
    ["cpu-history", ["cpu"], 100, null], ["temperature-history", ["temperature"], 100, null],
    ["disk-history", ["diskRead", "diskWrite"], null, "storageKey"],
    ["network-history", ["networkTx", "networkRx"], null, "networkKey"],
    ["system-cpu-history", ["cpu"], 100, null], ["system-temperature-history", ["temperature"], 100, null],
    ["network-detail-history", ["networkTx", "networkRx"], null, "networkKey"],
  ]) {
    const canvas = byId(id);
    if (!canvas || !canvas.clientWidth || !canvas.clientHeight) continue;
    const {width, height, scale} = canvasSize(canvas);
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, width, height);
    if (!history.length) {
      canvas.title = "No fresh telemetry";
      continue;
    }
    const maximum = fixedMaximum ?? Math.max(1, ...history.flatMap((point) => fields.map((field) => point[field] ?? 0))) * 1.1;
    const minimum = id === "temperature-history" ? Math.min(0, ...history.map((point) => point.temperature ?? 0)) : 0;
    canvas.title = `60 seconds · browser receipt time · ${minimum}–${fixedMaximum ?? formatBytes(maximum, true)}${id === "cpu-history" ? "%" : id === "temperature-history" ? "°C" : ""}`;
    fields.forEach((field, index) => {
      const color = index ? "#20c997" : "#12bdd7";
      let segment = [];
      const drawSegment = () => {
        if (!segment.length) return;
        const gradient = context.createLinearGradient(0, 0, 0, height);
        gradient.addColorStop(0, `${color}50`);
        gradient.addColorStop(1, `${color}00`);
        context.beginPath();
        context.moveTo(segment[0][0], height);
        for (const [x, y] of segment) context.lineTo(x, y);
        context.lineTo(segment[segment.length - 1][0], height);
        context.closePath();
        context.fillStyle = gradient;
        context.fill();
        context.beginPath();
        segment.forEach(([x, y], item) => item ? context.lineTo(x, y) : context.moveTo(x, y));
        context.strokeStyle = color;
        context.lineWidth = 1.5 * scale;
        context.lineJoin = "round";
        context.shadowColor = `${color}60`;
        context.shadowBlur = 4 * scale;
        context.stroke();
        if (segment.length === 1) {
          context.fillStyle = color;
          context.fillRect(segment[0][0] - scale, segment[0][1] - scale, 2 * scale, 2 * scale);
        }
        context.shadowBlur = 0;
        segment = [];
      };
      history.forEach((point, position) => {
        const previous = history[position - 1];
        if (point[field] === null || now - point.time > 60000
          || previous && (point.time - previous.time > 4500 || scope && point[scope] !== previous[scope])) drawSegment();
        if (point[field] !== null && now - point.time <= 60000) {
          segment.push([Math.max(0, 1 - (now - point.time) / 60000) * (width - scale), height - scale - Math.min(1, (point[field] - minimum) / (maximum - minimum)) * (height - 2 * scale)]);
        }
      });
      drawSegment();
    });
  }
}

function renderDashboard(payload, root) {
  const connected = state.mediaAvailable && root.stream?.state === "running" && root.connection?.state === "connected";
  const currentConfig = state.mediaAvailable && !state.restartRequired
    && state.config && (!payload.active_config_revision || payload.active_config_revision === state.revision);
  const config = currentConfig ? state.config : null;
  const mode = config?.capture?.mode;
  const device = state.devicesById.get(config?.capture?.device_id);
  const metadata = state.representationMetadata.get(root.stream?.representation_id ?? config?.stream?.representation_id);
  const unknown = state.restartRequired ? "Pending restart" : "—";
  const srtStats = connected ? root.connection?.statistics : null;
  const statistic = (key, suffix, digits = 2) => Number.isFinite(srtStats?.[key])
    ? `${srtStats[key].toFixed(digits)} ${suffix}`.trim() : "—";

  setStateValue("header-recording-state", root.recording?.state);
  setStateValue("header-connection-state", root.connection?.state);
  setValue("srt-host", config?.stream?.destination_host ?? unknown);
  setValue("srt-port", config?.stream?.destination_port ?? unknown);
  setValue("srt-latency-value", config ? `${config.stream.latency_ms} ms` : unknown);
  setValue("srt-encryption", config ? (config.stream.passphrase?.configured ? "Configured" : "Not configured") : unknown);
  setValue("srt-representation", state.mediaAvailable && metadata ? `${metadata.codec} / Matroska` : unknown);
  setValue("srt-tx", statistic("send-rate-mbps", "Mbps"));
  setValue("srt-rtt", statistic("rtt-ms", "ms", 1));
  setValue("srt-retransmits", statistic("packets-retransmitted", "", 0));
  setValue("network-receiver", config?.stream?.destination_host ?? unknown);
  setValue("recording-format", "FLAC · lossless");
  setValue("recording-rate", mode ? `${mode.rate_hz / 1000} kHz` : unknown);
  setValue("recording-channels", mode ? channelLabel(mode.channels) : unknown);
  renderRecordingTelemetry(payload, root);
  setValue("audio-device-name", device?.label ?? config?.capture?.device_id ?? unknown);
  setValue("audio-input", mode ? `${channelLabel(mode.channels)} · ${mode.native === false ? "Converted" : "Capture"}` : unknown);
  setValue("audio-capture", mode ? `${mode.rate_hz / 1000} kHz · ${mode.format}` : unknown);
  setValue("audio-output", state.mediaAvailable && metadata ? metadata.codec : unknown);
  setValue("spectrum-rate-label", mode ? `${mode.rate_hz / 1000} kHz` : "— kHz");
  setValue("spectrum-channels-label", mode ? channelLabel(mode.channels) : "—");

  for (const button of document.querySelectorAll("button[data-toggle]")) {
    const branch = button.dataset.toggle;
    const value = root[branch]?.state;
    const running = value === "running" || value === "stopping";
    const noun = branch === "stream" ? "stream" : "recording";
    setProperty(button.dataset, "action", `${branch}.${running ? "stop" : "start"}`);
    setProperty(button.dataset, "state", value ?? "unavailable");
    setProperty(button.dataset, "confirm", running
      ? branch === "stream" ? "Stop the active SRT stream?" : "Stop and finalize the active recording?"
      : "");
    const label = value === "starting" ? `Starting ${noun}` : value === "stopping" ? `Stopping ${noun}` : `${running ? "Stop" : "Start"} ${noun}`;
    const labelNode = button.querySelector(".action-label");
    setProperty(labelNode, "textContent", label);
    setAttribute(button, "aria-label", label);
    setAttribute(button, "aria-pressed", String(running));
    setAttribute(button.querySelector("use"), "href", running ? "#icon-stop" : "#icon-play");
  }
  scheduleTextFit();
}

function renderStatus(payload) {
  state.status = payload;
  state.statusReceivedAt = performance.now();
  state.restartRequired = payload?.restart_required === true
    || Boolean(payload?.active_config_revision && payload?.saved_config_revision
      && payload.active_config_revision !== payload.saved_config_revision);
  const reportedRoot = statusRoot(payload);
  const unavailable = payload?.available === false
    || payload?.media_available === false
    || !reportedRoot.service;
  const root = unavailable ? {} : reportedRoot;
  const service = stateText(root.service);
  const capture = stateText(root.capture?.state);
  const stream = stateText(root.stream?.state);
  const connection = stateText(root.connection?.state);
  const recording = stateText(root.recording?.state);
  setStateValue("service-state", root.service);
  setStateValue("capture-state", root.capture?.state);
  setStateValue("stream-state", root.stream?.state);
  setStateValue("connection-state", root.connection?.state);
  setStateValue("recording-state", root.recording?.state);

  state.mediaAvailable = !unavailable;
  const preview = payload?.runtime_error?.code === "simulated_preview";
  setValue("data-source", preview ? "SIMULATED LOCAL PREVIEW · NO PI" : "AUTHENTICATED CONTROL");
  if (byId("data-source")) byId("data-source").dataset.preview = String(preview);
  if (unavailable) {
    // Keep the editable draft AND its original revision across reconnects.
    // Availability still gates actions; the server validates modes and catches
    // concurrent edits. Only login, explicit save/reload or maintenance hydrates.
    clearTelemetry();
    ui.configState.textContent = "Media service unavailable; configuration controls are disabled.";
  } else if (state.config) {
    ui.configState.textContent = state.restartRequired ? "Saved changes need a media restart; starts are paused."
      : payload.saved_config_revision && payload.saved_config_revision !== state.revision
        ? "Configuration changed elsewhere; reload before editing."
        : "Configuration current · changes are saved explicitly";
  }
  if (root.capture?.state !== "running" || state.captureGeneration !== root.capture?.generation) {
    clearTelemetry();
    state.captureGeneration = root.capture?.generation;
  }
  updateDeviceTelemetry(payload);
  renderDashboard(payload, root);
  syncControlAvailability();
  const error = firstError(payload, root);
  const degraded = error || ["failed", "blocked", "retry_wait"].includes(root.connection?.state)
    || ["failed", "blocked"].includes(root.recording?.state);
  const headerState = unavailable ? "unavailable" : degraded ? "degraded" : root.service;
  ui.headerStatus.dataset.state = headerState || "unknown";
  ui.headerStatus.textContent = unavailable ? "Unavailable" : degraded ? "Attention"
    : root.stream?.state === "running" ? (root.connection?.state === "connected" ? "Streaming" : "Connecting")
    : root.capture?.state === "running" ? "Capturing" : "Ready";

  byId("diagnostic-media").textContent = unavailable ? "Unavailable" : service;
  byId("diagnostic-error").textContent = error ? errorMessage(error) : "None reported";
  byId("diagnostic-reconnects").textContent = String(root.connection?.reconnect_count ?? 0);
  byId("diagnostic-srt").textContent = root.stream?.state === "running" && root.connection?.state === "connected"
    ? formatSrtStatistics(root.connection?.statistics) : "No live connection";
  byId("diagnostic-route").textContent = typeof payload?.network?.route === "string"
    ? payload.network.route
    : "Unavailable";
  renderWarnings(payload, root);
  renderLogs(payload);
  renderDetailTelemetry();
  syncMaintenanceControls();
}

function canvasSize(canvas) {
  const scale = Math.min(window.devicePixelRatio || 1, 2);
  const width = Math.max(1, Math.round(canvas.clientWidth * scale));
  const height = Math.max(1, Math.round(canvas.clientHeight * scale));
  const changed = canvas.width !== width || canvas.height !== height;
  if (changed) {
    canvas.width = width;
    canvas.height = height;
  }
  return {width, height, scale, changed};
}

function finiteDb(value, fallback = -120) {
  return Number.isFinite(value) ? Math.max(-120, Math.min(0, value)) : fallback;
}

const SPECTRUM_COLORS = Array.from({length: 121}, (_, value) => {
  const hue = (235 - value / 120 * 210) / 30;
  const lightness = (5 + value / 120 * 57) / 100;
  const amplitude = 0.95 * Math.min(lightness, 1 - lightness);
  return [0, 8, 4].map((offset) => {
    const k = (offset + hue) % 12;
    return Math.round(255 * (lightness - amplitude * Math.max(-1, Math.min(k - 3, 9 - k, 1))));
  });
});
const SPECTRUM_PALETTES = {
  classic: SPECTRUM_COLORS,
  ocean: Array.from({length: 121}, (_, value) => [Math.round(value * 1.1), Math.round(value * 2.1), Math.round(35 + value * 1.83)]),
  ember: Array.from({length: 121}, (_, value) => [Math.round(Math.min(255, value * 3)), Math.round(value * value / 60), Math.round(value * value / 120)]),
  gray: Array.from({length: 121}, (_, value) => Array(3).fill(Math.round(value / 120 * 255))),
};

function loadVisualPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem("tinypirelay.visual.v1") || "null");
    if (!saved || typeof saved !== "object") return;
    if (Object.hasOwn(SPECTRUM_PALETTES, saved.spectrumPalette)) state.preferences.spectrumPalette = saved.spectrumPalette;
    if (["classic", "cyan", "mono"].includes(saved.meterPalette)) state.preferences.meterPalette = saved.meterPalette;
    if (["dark", "light"].includes(saved.theme)) state.preferences.theme = saved.theme;
    const view = saved.frequencyView;
    if (view && Number.isFinite(view.minHz) && Number.isFinite(view.maxHz) && view.minHz >= 20 && view.maxHz <= 192000 && view.maxHz - view.minHz >= 10 - 1e-6) state.spectrumView = {minHz: view.minHz, maxHz: view.maxHz};
  } catch { /* Browser storage can be disabled; live monitoring must still work. */ }
  byId("spectrum-palette").value = state.preferences.spectrumPalette;
  byId("meter-palette").value = state.preferences.meterPalette;
  byId("display-theme").value = state.preferences.theme;
  document.body.dataset.theme = state.preferences.theme;
}

function saveVisualPreferences() {
  if (!state.preferencesDirty) return;
  state.preferencesDirty = false;
  try {
    localStorage.setItem("tinypirelay.visual.v1", JSON.stringify({...state.preferences, frequencyView: state.spectrumView}));
  } catch { setValue("spectrum-view-message", "Preferences apply now, but this browser did not allow saving them."); }
}

function spectrumTimePosition(ageMs) {
  const age = Math.max(0, Math.min(SPECTRUM_HISTORY_MS, ageMs));
  return 1 - age / SPECTRUM_HISTORY_MS;
}

function spectrumFrequencyPosition(hz, maximumHz, minimumHz = SPECTRUM_MINIMUM_HZ) {
  const frequency = Math.max(minimumHz, Math.min(maximumHz, hz));
  const logarithmic = Math.log(frequency / minimumHz) / Math.log(maximumHz / minimumHz);
  return Math.pow(logarithmic, SPECTRUM_FREQUENCY_BIAS);
}

function spectrumNyquist() {
  const rate = !state.restartRequired ? state.config?.capture?.mode?.rate_hz : null;
  return Number.isFinite(rate) && rate / 2 >= SPECTRUM_MINIMUM_HZ + SPECTRUM_MINIMUM_SPAN_HZ ? rate / 2 : null;
}

function spectrumViewBounds() {
  const nyquist = spectrumNyquist();
  if (!nyquist) return null;
  const view = state.spectrumView;
  if (view && view.maxHz <= nyquist) return view;
  state.spectrumView = null;
  return {minHz: SPECTRUM_MINIMUM_HZ, maxHz: Math.min(24_000, nyquist)};
}

function syncSpectrumViewControls(force = false) {
  const nyquist = spectrumNyquist();
  const refresh = force || state.spectrumViewLimit !== nyquist;
  state.spectrumViewLimit = nyquist;
  const view = spectrumViewBounds();
  for (const [id, key] of [["spectrum-min-hz", "minHz"], ["spectrum-max-hz", "maxHz"]]) {
    const input = byId(id);
    if (!input) continue;
    setProperty(input, "disabled", !view);
    setProperty(input, "max", String(nyquist ?? 24_000));
    if (refresh) setProperty(input, "value", view ? String(view[key]) : "");
    if (refresh) input.setCustomValidity("");
  }
  setProperty(byId("spectrum-view-reset"), "disabled", !view);
}

function setSpectrumView(minHz, maxHz) {
  const nyquist = spectrumNyquist();
  const error = !nyquist ? "Frequency bounds need an active, known capture sample rate."
    : !Number.isFinite(minHz) || !Number.isFinite(maxHz)
      || minHz < SPECTRUM_MINIMUM_HZ || maxHz > nyquist || maxHz - minHz < SPECTRUM_MINIMUM_SPAN_HZ - 1e-6
      ? `Use 20–${nyquist} Hz with at least 10 Hz between bounds.` : "";
  for (const id of ["spectrum-min-hz", "spectrum-max-hz"]) byId(id)?.setCustomValidity(error);
  if (byId("spectrum-view-message")) byId("spectrum-view-message").textContent = error || "Display only · capture, recording and streaming are unchanged.";
  if (error) return false;
  const previous = spectrumViewBounds();
  const changed = previous?.minHz !== minHz || previous?.maxHz !== maxHz;
  state.spectrumView = {minHz, maxHz};
  if (changed) state.spectrumRaster = null;
  if (changed) state.preferencesDirty = true;
  syncSpectrumViewControls(true);
  if (changed) scheduleTelemetryFrame();
  return true;
}

function resetSpectrumView() {
  const nyquist = spectrumNyquist();
  if (nyquist) setSpectrumView(SPECTRUM_MINIMUM_HZ, Math.min(24_000, nyquist));
  state.spectrumView = null;
}

function clampSpectrumLogView(low, span, nyquist) {
  const lower = Math.log(SPECTRUM_MINIMUM_HZ);
  const upper = Math.log(nyquist);
  span = Math.max(0, Math.min(upper - lower, span));
  low = Math.max(lower, Math.min(upper - span, low));
  const minHz = Math.max(SPECTRUM_MINIMUM_HZ, Math.min(nyquist - SPECTRUM_MINIMUM_SPAN_HZ, Math.exp(low)));
  return {minHz, maxHz: Math.min(nyquist, Math.max(minHz + SPECTRUM_MINIMUM_SPAN_HZ, Math.exp(low + span)))};
}

function zoomSpectrumView(view, anchorRatio, factor, nyquist) {
  const anchor = Math.pow(Math.max(0, Math.min(1, anchorRatio)), 1 / SPECTRUM_FREQUENCY_BIAS);
  const span = Math.log(view.maxHz / view.minHz);
  const nextSpan = Math.min(Math.log(nyquist / SPECTRUM_MINIMUM_HZ), span * factor);
  return clampSpectrumLogView(Math.log(view.minHz) + anchor * (span - nextSpan), nextSpan, nyquist);
}

function panSpectrumView(view, deltaRatio, nyquist) {
  const span = Math.log(view.maxHz / view.minHz);
  return clampSpectrumLogView(Math.log(view.minHz) + deltaRatio * span, span, nyquist);
}

function compactSpectrumColumns(columns, now) {
  const valid = columns.filter((column) => Number.isFinite(column.time) && column.time <= now && Array.isArray(column.values));
  const first = valid.findIndex((column) => now - column.time <= SPECTRUM_HISTORY_MS);
  // One preceding sample can still cover the leftmost pixel after a resize.
  const hold = Math.max(SPECTRUM_DELIVERY_HOLD_MS, 2000 / Math.max(0.01, state.config?.monitoring?.spectrum_updates_per_second ?? 5));
  let start = first < 0 ? valid.length : first;
  if (start > 0 && valid[start - 1].time + hold > now - SPECTRUM_HISTORY_MS) start -= 1;
  return valid.slice(start).slice(-SPECTRUM_HISTORY_COLUMNS);
}

function renderMeters(meter) {
  const canvas = byId("level-canvas");
  const {width, height, scale} = canvasSize(canvas);
  const context = canvas.getContext("2d");
  const channels = Array.isArray(meter?.channels) ? meter.channels.slice(0, 8) : [];
  byId("level-values").style.setProperty("--meter-channels", String(Math.max(1, Math.min(2, channels.length))));
  context.fillStyle = "#081723";
  context.fillRect(0, 0, width, height);
  if (!channels.length) {
    byId("signal-summary").textContent = telemetryMessage();
    byId("level-values").replaceChildren();
    return;
  }

  const left = 27 * scale;
  const top = 23 * scale;
  const bottom = height - 12 * scale;
  const chartHeight = Math.max(1, bottom - top);
  const columnWidth = (width - left - 8 * scale) / channels.length;
  const barWidth = Math.min(32 * scale, columnWidth * 0.55);
  context.font = `${10 * scale}px system-ui`;
  context.textAlign = "right";
  for (const db of [0, -6, -12, -18, -24, -36, -48, -60]) {
    const y = top + (-db / 60) * chartHeight;
    context.fillStyle = "#9caebd";
    context.fillText(String(db), left - 6 * scale, y + 3 * scale);
    context.fillStyle = "#203543";
    context.fillRect(left, y, width - left, scale / 2);
  }
  const values = [];
  channels.forEach((channel, index) => {
    const peak = finiteDb(channel?.peak_dbfs);
    const rms = finiteDb(channel?.rms_dbfs);
    const x = left + columnWidth * (index + 0.5) - barWidth / 2;
    const label = channels.length === 1 ? "MONO" : channels.length === 2 ? ["LEFT", "RIGHT"][index] : `CH ${index + 1}`;
    context.textAlign = "center";
    context.fillStyle = "#dce8f0";
    context.fillText(label, x + barWidth / 2, 12 * scale);
    for (let segment = 0; segment < 30; segment += 1) {
      const db = -60 + segment * 2;
      context.fillStyle = db <= rms
        ? db >= -6 ? "#ff625b" : db >= -18 ? "#f6bd4f" : state.preferences.meterPalette === "cyan" ? "#12bdd7" : state.preferences.meterPalette === "mono" ? "#dce8f0" : "#18c6a0"
        : "#19313c";
      context.fillRect(x, bottom - (segment + 1) * chartHeight / 30, barWidth, Math.max(1, chartHeight / 30 - 2 * scale));
    }
    context.fillStyle = channel?.clipping === true ? "#ff625b" : "#e7f6ff";
    context.fillRect(x - 2 * scale, top + Math.min(60, -peak) / 60 * chartHeight, barWidth + 4 * scale, 2 * scale);

    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = label;
    const peakValue = document.createElement("span");
    peakValue.className = "meter-peak";
    peakValue.textContent = `${peak.toFixed(1)} PEAK`;
    const rmsValue = document.createElement("span");
    rmsValue.className = "meter-rms";
    rmsValue.textContent = `${rms.toFixed(1)} RMS`;
    item.append(name, peakValue, rmsValue);
    values.push(item);
  });
  byId("level-values").replaceChildren(...values);
  byId("signal-summary").textContent = channels.some((channel) => channel?.clipping === true) ? "CLIPPING"
    : channels.every((channel) => channel?.no_signal === true) ? "NO SIGNAL" : `${channelLabel(channels.length)} · dBFS`;
}

function renderSpectrum(spectrum) {
  const canvas = byId("spectrum-canvas");
  const {width, height, scale} = canvasSize(canvas);
  const context = canvas.getContext("2d");
  const magnitudes = Array.isArray(spectrum?.magnitudes_db)
    ? spectrum.magnitudes_db.slice(0, 2048)
    : [];
  context.fillStyle = "#081723";
  context.fillRect(0, 0, width, height);
  if (!magnitudes.length) {
    state.spectrumRaster = null;
    byId("spectrum-summary").textContent = state.config?.monitoring?.spectrum_updates_per_second === 0
      ? "Spectrum disabled in Audio settings" : telemetryMessage();
    return;
  }

  const now = performance.now();
  const nyquist = spectrumNyquist();
  const view = spectrumViewBounds();
  const left = Math.round(42 * scale);
  const top = Math.round(12 * scale);
  const plotWidth = Math.max(1, width - left - Math.round(8 * scale));
  const plotHeight = Math.max(1, height - top - Math.round(23 * scale));
  const key = `${plotWidth}:${plotHeight}:${nyquist}:${view?.minHz}:${view?.maxHz}:${magnitudes.length}:${state.preferences.spectrumPalette}`;
  let raster = state.spectrumRaster;
  const rebuild = !raster || raster.key !== key;
  if (rebuild) {
    const plot = document.createElement("canvas");
    plot.width = plotWidth;
    plot.height = plotHeight;
    const strip = document.createElement("canvas");
    strip.width = 1;
    strip.height = plotHeight;
    const stripContext = strip.getContext("2d");
    const bands = Array.from({length: plotHeight}, (_, row) => {
      const bandAt = (position) => view
        ? view.minHz * Math.pow(view.maxHz / view.minHz, Math.pow(position, 1 / SPECTRUM_FREQUENCY_BIAS)) / nyquist * magnitudes.length
        : position * magnitudes.length;
      const first = Math.min(magnitudes.length - 1, Math.max(0, Math.floor(bandAt(1 - (row + 1) / plotHeight))));
      const last = Math.min(magnitudes.length - 1, Math.max(first, Math.ceil(bandAt(1 - row / plotHeight)) - 1));
      return [first, last];
    });
    raster = {key, plot, strip, stripContext, image: stripContext.createImageData(1, plotHeight), bands, time: now};
    state.spectrumRaster = raster;
  }
  const plotContext = raster.plot.getContext("2d");
  plotContext.imageSmoothingEnabled = false;
  const millisecondsPerPixel = SPECTRUM_HISTORY_MS / plotWidth;
  // Keep subpixel time in the clock, not repeated fractional bitmap copies (which blur).
  const shift = rebuild ? plotWidth : Math.max(0, Math.floor((now - raster.time) / millisecondsPerPixel));
  const rightTime = rebuild ? now : raster.time + shift * millisecondsPerPixel;
  const paintFrom = Math.max(0, plotWidth - shift);
  if (shift > 0) {
    if (shift < plotWidth) {
      plotContext.drawImage(raster.plot, shift, 0, plotWidth - shift, plotHeight, 0, 0, plotWidth - shift, plotHeight);
    }
    plotContext.fillStyle = "#04121f";
    plotContext.fillRect(paintFrom, 0, plotWidth - paintFrom, plotHeight);
  }
  // Only the exposed strip is painted each frame; raw history is replayed on zoom/resize.
  // ponytail: redraw this bounded 30 s history on view changes; cache an atlas if profiling warrants it.
  const columns = state.spectrumColumns;
  const spanMs = Math.max(SPECTRUM_DELIVERY_HOLD_MS, 2000 / Math.max(0.01, state.config?.monitoring?.spectrum_updates_per_second ?? 5));
  if (shift > 0) {
    const leftTime = rightTime - SPECTRUM_HISTORY_MS;
    const earliestPaint = leftTime + paintFrom * millisecondsPerPixel;
    columns.forEach((column, index) => {
      const endTime = Math.min(columns[index + 1]?.time ?? rightTime, column.time + spanMs, rightTime);
      if (endTime <= earliestPaint || column.time >= rightTime || endTime <= column.time) return;
      const x = Math.max(paintFrom, Math.floor((column.time - leftTime) / millisecondsPerPixel));
      const endX = Math.min(plotWidth, Math.ceil((endTime - leftTime) / millisecondsPerPixel));
      if (endX <= x) return;
      raster.bands.forEach(([first, last], row) => {
        let db = -120;
        for (let band = first; band <= last; band += 1) db = Math.max(db, finiteDb(column.values[band]));
        const color = SPECTRUM_PALETTES[state.preferences.spectrumPalette][Math.round(db + 120)];
        const offset = row * 4;
        raster.image.data[offset] = color[0];
        raster.image.data[offset + 1] = color[1];
        raster.image.data[offset + 2] = color[2];
        raster.image.data[offset + 3] = 255;
      });
      raster.stripContext.putImageData(raster.image, 0, 0);
      plotContext.drawImage(raster.strip, 0, 0, 1, plotHeight, x, 0, endX - x, plotHeight);
    });
    raster.time = rightTime;
  }
  context.imageSmoothingEnabled = false;
  context.drawImage(raster.plot, left, top);
  context.font = `${10 * scale}px system-ui`;
  context.textAlign = "right";
  const ticks = view ? [view.maxHz, ...[20, 60, 125, 250, 500, 1000, 2000, 4000, 8000, 12000, 16000, 20000, 24000, 32000, 48000]
    .filter((hz) => hz > view.minHz && hz < view.maxHz).reverse(), view.minHz] : [1, 0.75, 0.5, 0.25, 0];
  let lastLabelY = -Infinity;
  for (const tick of ticks) {
    const ratio = view ? spectrumFrequencyPosition(tick, view.maxHz, view.minHz) : tick;
    const y = top + (1 - ratio) * plotHeight;
    if (y - lastLabelY < 15 * scale || (view && tick !== view.minHz && top + plotHeight - y < 15 * scale)) continue;
    lastLabelY = y;
    context.fillStyle = "#bdd0dc";
    context.fillText(view ? (tick >= 1000 ? `${Number((tick / 1000).toFixed(1))}k` : String(Math.round(tick))) : String(Math.round(tick * magnitudes.length)), left - 6 * scale, y + 3 * scale);
    context.fillStyle = "#9ad8e51c";
    context.fillRect(left, y, plotWidth, scale / 2);
  }
  context.textAlign = "center";
  context.fillStyle = "#9caebd";
  for (const seconds of [30, 25, 20, 15, 10, 5, 0]) {
    const x = left + spectrumTimePosition(seconds * 1000) * plotWidth;
    context.fillStyle = "#9ad8e51c";
    context.fillRect(x, top, scale / 2, plotHeight);
    context.fillStyle = "#9caebd";
    context.textAlign = seconds === 30 ? "left" : seconds === 0 ? "right" : "center";
    context.fillText(seconds ? `−${seconds}s` : "NOW", x, height - 3 * scale);
  }
  byId("spectrum-summary").textContent = `${view ? `${Math.round(view.minHz)}–${Math.round(view.maxHz)} Hz · high-detail log` : "Frequency bands"} · ${magnitudes.length} bins · 30 s linear`;
}

function telemetryMessage() {
  if (!state.mediaAvailable) return "Media unavailable";
  return statusRoot(state.status).capture?.state === "running" ? "Waiting for telemetry" : "Capture stopped";
}

function clearSpectrumHistory() {
  state.spectrumColumns = [];
  state.spectrumRaster = null;
}

function clearTelemetry() {
  if (state.telemetryFrame !== null) cancelAnimationFrame(state.telemetryFrame);
  state.telemetryFrame = null;
  state.lastMeter = null;
  state.lastSpectrum = null;
  state.meterSequence = null;
  state.spectrumSequence = null;
  state.meterReceivedAt = 0;
  state.spectrumReceivedAt = 0;
  clearSpectrumHistory();
  renderMeters(null);
  renderSpectrum(null);
}

function queueTelemetry(telemetry) {
  if (!state.mediaAvailable || statusRoot(state.status).capture?.state !== "running") return;
  const now = performance.now();
  for (const kind of ["meter", "spectrum"]) {
    const sample = telemetry?.[kind];
    const sequenceKey = `${kind}Sequence`;
    const timeKey = `${kind}ReceivedAt`;
    const sampleKey = kind === "meter" ? "lastMeter" : "lastSpectrum";
    if (sample && Number.isFinite(sample.sequence)) {
      if (sample.sequence !== state[sequenceKey]) {
        // A process restart may reset a sequence; accept it, but discard the old trace.
        if (sample.sequence < state[sequenceKey] && kind === "spectrum") clearSpectrumHistory();
        state[sequenceKey] = sample.sequence;
        state[timeKey] = now;
        state[sampleKey] = sample;
        if (kind === "spectrum" && Array.isArray(sample.magnitudes_db)) {
          const values = sample.magnitudes_db.slice(0, 2048).map((value) => finiteDb(value));
          if (state.spectrumColumns.length && state.spectrumColumns.at(-1).values.length !== values.length) clearSpectrumHistory();
          state.spectrumColumns.push({time: now, values});
          state.spectrumColumns = compactSpectrumColumns(state.spectrumColumns, now);
        }
      }
    } else {
      state[sampleKey] = null;
      state[sequenceKey] = null;
      if (kind === "spectrum") clearSpectrumHistory();
    }
  }
  checkTelemetryFreshness();
  scheduleTelemetryFrame();
}

function scheduleTelemetryFrame() {
  if (state.telemetryFrame !== null) return;
  state.telemetryFrame = requestAnimationFrame(() => {
    state.telemetryFrame = null;
    saveVisualPreferences();
    if (!state.mediaAvailable || statusRoot(state.status).capture?.state !== "running") return;
    checkTelemetryFreshness();
    if (state.route === "dashboard") {
      renderMeters(state.lastMeter);
      renderSpectrum(state.lastSpectrum);
    }
  });
}

function checkTelemetryFreshness() {
  const now = performance.now();
  if (state.lastMeter && now - state.meterReceivedAt > 3000) {
    state.lastMeter = null;
    renderMeters(null);
  }
  const spectrumLimit = Math.max(3000, 3000 / Math.max(0.01, state.config?.monitoring?.spectrum_updates_per_second ?? 5));
  if (state.lastSpectrum && now - state.spectrumReceivedAt > spectrumLimit) {
    state.lastSpectrum = null;
    clearSpectrumHistory();
    renderSpectrum(null);
  }
}

function startEvents() {
  stopEvents();
  const events = new EventSource("/api/events");
  state.eventSource = events;
  events.addEventListener("snapshot", (event) => {
    if (state.eventSource !== events) return;
    try {
      const snapshot = JSON.parse(event.data);
      renderStatus(snapshot.status);
      queueTelemetry(snapshot.telemetry);
      if (state.mediaAvailable && state.config === null && !state.mediaDataLoading) {
        state.mediaDataLoading = true;
        loadApplicationData()
          .catch(() => setMessage("Media returned, but configuration could not be loaded.", "error"))
          .finally(() => { state.mediaDataLoading = false; });
      }
    } catch {
      setMessage("A malformed live update was ignored.", "error");
    }
  });
  events.addEventListener("telemetry", (event) => {
    if (state.eventSource !== events) return;
    try {
      queueTelemetry(JSON.parse(event.data));
    } catch {
      setMessage("A malformed live update was ignored.", "error");
    }
  });
  events.addEventListener("open", () => setMessage("Live updates connected."));
  events.addEventListener("error", () => {
    if (state.eventSource !== events) return;
    markLiveDisconnected("Live updates disconnected; reconnecting automatically.");
    api("/api/session")
      .then((session) => {
        if (!session.authenticated) showLogin("Session expired. Sign in again.");
      })
      .catch(() => {});
  });
}

function markLiveDisconnected(message) {
  state.mediaAvailable = false;
  state.statusReceivedAt = 0;
  clearTelemetry();
  clearDeviceTelemetry();
  for (const id of ["service-state", "capture-state", "stream-state", "connection-state", "recording-state"]) {
    setStateValue(id, null);
  }
  renderDashboard({}, {});
  syncControlAvailability();
  ui.headerStatus.dataset.state = "degraded";
  ui.headerStatus.textContent = "Reconnecting";
  setValue("diagnostic-media", "Unavailable");
  setValue("diagnostic-srt", "No live connection");
  ui.configState.textContent = "Live connection lost; controls are paused.";
  setMessage(message, "error");
}

let textFitFrame = null;
const textFitCache = new WeakMap();

function interactionOwns(element) {
  const owner = element.closest?.("input, select, textarea, button, a[href], [contenteditable]") || element;
  return owner === document.activeElement || owner.contains(document.activeElement) || owner.matches?.(":active");
}

function needsTextFit(element, text) {
  if (!element.clientWidth || interactionOwns(element)) return false;
  const key = JSON.stringify([text, element.clientWidth, window.innerWidth, window.innerHeight,
    document.body.dataset.theme, element.closest?.(".card-actions")?.style.getPropertyValue("--button-font")]);
  if (textFitCache.get(element) === key) return false;
  textFitCache.set(element, key);
  return true;
}

function scheduleTextFit() {
  if (textFitFrame !== null) return;
  textFitFrame = requestAnimationFrame(() => {
    textFitFrame = null;
    const labels = [...document.querySelectorAll(".card-actions .action-label")].filter((element) => element.clientWidth > 0);
    if (labels.length) {
      const context = byId("level-canvas").getContext("2d");
      let sharedSize = 14;
      for (const label of labels) {
        const style = getComputedStyle(label);
        context.font = `${style.fontWeight} 14px ${style.fontFamily}`;
        const branch = label.closest("[data-toggle]")?.dataset.toggle;
        const texts = branch
          ? [`Start ${branch}`, `Stop ${branch}`, `Starting ${branch}`, `Stopping ${branch}`]
          : [label.textContent];
        const widest = Math.max(...texts.map((text) => context.measureText(text).width));
        sharedSize = Math.min(sharedSize, 14 * Math.max(1, label.clientWidth - 1) / Math.max(1, widest));
      }
      for (const group of document.querySelectorAll(".card-actions")) {
        const size = `${Math.max(8, Math.floor(sharedSize * 10) / 10)}px`;
        if (!interactionOwns(group) && group.style.getPropertyValue("--button-font") !== size) group.style.setProperty("--button-font", size);
      }
    }
    for (const element of document.querySelectorAll("[data-fit], .help, .settings-note, .action-label, #global-message, #config-state")) {
      if (!needsTextFit(element, element.textContent)) continue;
      setProperty(element.style, "fontSize", "");
      const maximum = parseFloat(getComputedStyle(element).fontSize);
      const ratio = element.clientWidth / Math.max(element.clientWidth, element.scrollWidth);
      if (ratio < 1) setProperty(element.style, "fontSize", `${Math.max(7, Math.floor(maximum * ratio * 10) / 10)}px`);
    }
    for (const select of document.querySelectorAll("select")) {
      if (!select.selectedOptions.length) continue;
      if (!interactionOwns(select)) setProperty(select, "title", select.selectedOptions[0].textContent);
      // Fit the option set once, not the selection on every live tick. Native
      // menus keep one font size while open, including keyboard selection.
      const labels = [...select.options].map(option => option.textContent);
      if (!needsTextFit(select, JSON.stringify(labels))) continue;
      setProperty(select.style, "fontSize", "");
      const context = byId("level-canvas").getContext("2d");
      const style = getComputedStyle(select);
      context.font = style.font;
      const measured = Math.max(...labels.map(text => context.measureText(text).width));
      const ratio = (select.clientWidth - 34) / Math.max(1, measured);
      if (ratio < 1) setProperty(select.style, "fontSize", `${Math.max(7, parseFloat(style.fontSize) * ratio)}px`);
    }
  });
}

// Deferred presentation work resumes after interaction, without pausing live
// telemetry or delaying real safety/availability changes.
document.addEventListener("focusout", scheduleTextFit);
document.addEventListener("pointerup", scheduleTextFit);
document.addEventListener("change", (event) => {
  if (event.target.tagName === "SELECT") {
    scheduleTextFit();
  }
});

function setRoute() {
  const requested = window.location.hash.slice(1) || "dashboard";
  const route = requested === "logs" ? "diagnostics" : requested;
  const pages = [...document.querySelectorAll(".page-section[data-page]")];
  const previousRoute = state.route;
  state.route = pages.some((page) => page.dataset.page === route) ? route : "dashboard";
  if (previousRoute === "storage" && state.route !== "storage") stopPlayback();
  for (const page of pages) page.hidden = page.dataset.page !== state.route;
  for (const link of document.querySelectorAll(".section-nav a")) {
    const active = link.getAttribute("href") === `#${state.route}`;
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (byId("config-save-bar")) byId("config-save-bar").hidden = !["audio", "stream", "recording"].includes(state.route);
  if (state.route === "dashboard") {
    renderMeters(state.lastMeter);
    renderSpectrum(state.lastSpectrum);
    checkDeviceTelemetryFreshness();
    renderDeviceHistories();
  }
  if (state.route === "diagnostics") renderLogs(state.status, true);
  if (state.route === "storage") loadStorage();
  if (["settings", "system"].includes(state.route)) loadMaintenance();
  if (["system", "network"].includes(state.route)) { renderDetailTelemetry(); renderDeviceHistories(); }
  if (state.route === "about") renderAbout();
  scheduleTextFit();
}

window.addEventListener("hashchange", setRoute);
window.addEventListener("resize", () => {
  if (state.route === "dashboard") {
    renderMeters(state.lastMeter);
    renderSpectrum(state.lastSpectrum);
    renderDeviceHistories();
  }
  scheduleTextFit();
});
document.addEventListener("visibilitychange", () => {
  if (!state.csrfToken) return;
  if (document.hidden) {
    stopEvents();
    markLiveDisconnected("Live view paused while this tab is hidden; capture continues independently.");
  } else {
    startEvents();
  }
});
setInterval(() => {
  if (!state.csrfToken || document.hidden) return;
  checkTelemetryFreshness();
  checkDeviceTelemetryFreshness();
  const now = performance.now();
  if (state.route === "storage" && now - state.storageLoadedAt > (["checking", "recovering"].includes(state.storage?.check?.state)
      || state.storage?.items?.some((file) => ["checking", "recovering"].includes(file.check?.state)) ? 2000 : 15000)) loadStorage();
  if ((state.maintenancePending || ["system", "settings"].includes(state.route)) && now - state.maintenanceLoadedAt > 2000) loadMaintenance();
  if (state.mediaAvailable && performance.now() - state.statusReceivedAt > 5000) {
    markLiveDisconnected("No fresh status received; waiting to reconnect.");
  }
}, 1000);

for (const [id, delta] of [["logs-previous", -1], ["logs-next", 1]]) {
  byId(id)?.addEventListener("click", () => {
    state.logPage = Math.max(0, state.logPage + delta);
    renderLogs(state.status, true);
  });
}

async function enterApplication(session) {
  showApp(session);
  try {
    await loadApplicationData();
  } catch (error) {
    if (!(error instanceof ApiError) || ![502, 503].includes(error.status)) throw error;
    state.config = null;
    renderStatus(error.payload);
    setMessage("The media service is unavailable; live status will retry automatically.", "error");
  }
  startEvents();
  if (["settings", "system"].includes(state.route)) loadMaintenance();
}

function selectedModeConfig() {
  const mode = activeMode();
  if (!mode) return null;
  return {format: mode.format, rate_hz: mode.rate_hz, channels: mode.channels};
}

function nullableText(value) {
  const trimmed = value.trim();
  return trimmed ? trimmed : null;
}

function integerValue(input) {
  return Number.parseInt(input.value, 10);
}

function editableConfig() {
  const config = structuredClone(state.config);
  config.capture.device_id = ui.device.value || null;
  config.capture.mode = selectedModeConfig();
  config.stream.enabled = ui.streamEnabled.checked;
  config.stream.representation_id = ui.representation.value || null;
  config.stream.destination_host = nullableText(ui.destinationHost.value);
  config.stream.destination_port = ui.destinationPort.value ? integerValue(ui.destinationPort) : null;
  config.stream.latency_ms = integerValue(ui.latency);
  config.stream.stream_id = nullableText(ui.streamId.value);
  config.stream.opus_bitrate_bps = ui.bitrate.value ? integerValue(ui.bitrate) : config.stream.opus_bitrate_bps;
  if (ui.clearPassphrase.checked) {
    config.stream.passphrase = {action: "clear"};
  } else if (ui.passphrase.value) {
    config.stream.passphrase = {action: "replace", value: ui.passphrase.value};
  } else {
    config.stream.passphrase = {action: "keep"};
  }
  config.recording.directory = ui.recordingDirectory.value.trim();
  config.recording.rotation_seconds = integerValue(ui.rotationSeconds);
  config.recording.required_mountpoint = nullableText(ui.requiredMountpoint.value);
  config.monitoring.spectrum_updates_per_second = Number(ui.spectrumRate.value);
  config.monitoring.spectrum_bands = integerValue(ui.spectrumBands);
  return config;
}

function formatChangeRequiresRestart(next) {
  return state.config.capture.device_id !== next.capture.device_id
    || !sameMode(state.config.capture.mode, next.capture.mode)
    || state.config.stream.representation_id !== next.stream.representation_id;
}

async function saveConfig(config, confirmRestart, expectedRevision = state.revision) {
  return api("/api/config", {
    method: "PUT",
    csrf: true,
    body: {config, confirm_restart: confirmRestart, expected_revision: expectedRevision},
  });
}

function isRevisionConflict(error) {
  return error instanceof ApiError
    && error.payload?.error?.code === "revision_conflict";
}

async function handleConfigSubmit(event) {
  event.preventDefault();
  if (!state.config || !state.mediaAvailable || state.configSaving) return;
  if (!configForm.checkValidity()) {
    const invalid = configForm.querySelector(":invalid");
    const page = invalid?.closest("[data-page]");
    if (page) {
      window.location.hash = page.dataset.page;
      setRoute();
    }
    configForm.reportValidity();
    return;
  }
  const config = editableConfig();
  const expectedRevision = state.revision;
  let confirmed = false;
  if (formatChangeRequiresRestart(config)) {
    confirmed = await confirmAction("This change requires a media restart. Save it as restart-required configuration?");
    if (!confirmed) return;
  }

  state.configSaving = true;
  syncControlAvailability();
  try {
    let result;
    try {
      result = await saveConfig(config, confirmed, expectedRevision);
    } catch (error) {
      const restartRequired = error instanceof ApiError
        && (error.payload?.error?.code === "confirmation_required"
          || error.payload?.restart_required === true);
      if (!restartRequired) throw error;
      if (!await confirmAction("The media service requires a restart for these changes. Save it as restart-required configuration?")) return;
      result = await saveConfig(config, true, expectedRevision);
    }
    ui.configState.textContent = result.restart_required
      ? "Configuration saved; a media restart is required."
      : "Configuration saved.";
    setMessage("Configuration saved.");
    hydrateConfig(await api("/api/config"));
    renderStatus(await api("/api/status"));
  } catch (error) {
    if (isRevisionConflict(error)) {
      const reload = await confirmAction("Configuration changed in another session. Reload the latest values and replace your unsaved edits?");
      if (reload) {
        try {
          hydrateConfig(await api("/api/config"));
          setMessage("Latest configuration loaded. Review it before saving again.", "error");
        } catch (reloadError) {
          renderMediaRequestFailure(reloadError);
          setMessage(reloadError instanceof Error ? reloadError.message : "Latest configuration could not be loaded.", "error");
        }
      } else {
        setMessage("Save cancelled because the configuration changed elsewhere.", "error");
      }
    } else {
      renderMediaRequestFailure(error);
      setMessage(error instanceof Error ? error.message : "Configuration save failed.", "error");
    }
  } finally {
    state.configSaving = false;
    syncControlAvailability();
  }
}

async function runControl(button) {
  const requestedAction = button.dataset.action;
  const intendedAction = controlIntents.get(button);
  controlIntents.delete(button);
  if (intendedAction && intendedAction !== requestedAction) {
    setMessage("Media state changed while pressing the button; review its current action and try again.", "error");
    return;
  }
  const reason = controlDisabledReason(requestedAction);
  if (reason) {
    setMessage(reason, "error");
    return;
  }
  const message = button.dataset.confirm;
  if (message && !await confirmAction(message)) return;
  const updatedReason = controlDisabledReason(requestedAction);
  if (button.dataset.action !== requestedAction || updatedReason) {
    setMessage(updatedReason || "Media state changed; review the current control before trying again.", "error");
    return;
  }
  state.busyButtons.add(button);
  syncControlAvailability();
  try {
    const body = {action: button.dataset.action};
    if (body.action === "stream.set_opus_bitrate") body.value = integerValue(ui.bitrate);
    const result = await api("/api/control", {
      method: "POST",
      csrf: true,
      body,
    });
    if (result?.state) renderStatus({...state.status, state: result.state});
    if (result?.ok === false || result?.accepted === false) {
      const message = errorMessage(result?.error)
        || (typeof result?.message === "string" ? result.message : "Control request was not applied.");
      setMessage(message, "error");
    } else if (result?.changed === false) {
      setMessage("No media state change was needed.");
    } else {
      setMessage("Control request applied.");
    }
    renderStatus(await api("/api/status"));
  } catch (error) {
    renderMediaRequestFailure(error);
    setMessage(error instanceof Error ? error.message : "Control request failed.", "error");
  } finally {
    state.busyButtons.delete(button);
    syncControlAvailability();
  }
}

loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = loginForm.querySelector("button[type=submit]");
  button.disabled = true;
  byId("login-message").textContent = "";
  try {
    if (state.setupRequired) {
      const result = await api("/api/setup", {
        method: "POST",
        body: {username: byId("username").value, password: byId("password").value,
          confirm_password: byId("setup-confirm-password").value},
      });
      showLogin(result.durability_confirmed === false
        ? "Account created. Sign in, then check device storage: durability was not confirmed."
        : "Account created. Sign in to continue.");
      return;
    }
    await api("/api/login", {
      method: "POST",
      body: {username: byId("username").value, password: byId("password").value},
    });
    const session = await api("/api/session");
    if (!session.authenticated) throw new Error("Sign-in did not create a session.");
    await enterApplication(session);
  } catch (error) {
    if (error instanceof ApiError && error.payload?.error?.code === "setup_complete") {
      showLogin("An account has already been created. Sign in to continue.");
      return;
    }
    if (error instanceof ApiError && error.payload?.error?.code === "setup_required") {
      showLogin("", true);
      return;
    }
    byId("login-message").textContent = error instanceof Error ? error.message : "Sign-in failed.";
    byId("login-message").dataset.error = "true";
    byId("login-message").setAttribute("role", "alert");
    byId("password").value = "";
    byId("setup-confirm-password").value = "";
    byId("password").focus();
  } finally {
    button.disabled = false;
  }
});

byId("logout-button").addEventListener("click", async () => {
  try {
    await api("/api/logout", {method: "POST", csrf: true});
    showLogin("Signed out.");
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "Sign-out failed.", "error");
  }
});

function commitSpectrumBounds() {
  return setSpectrumView(Number(byId("spectrum-min-hz").value), Number(byId("spectrum-max-hz").value));
}

for (const id of ["spectrum-min-hz", "spectrum-max-hz"]) {
  byId(id)?.addEventListener("change", commitSpectrumBounds);
  byId(id)?.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    commitSpectrumBounds();
  });
}
byId("spectrum-view-reset")?.addEventListener("click", resetSpectrumView);

const spectrumCanvas = byId("spectrum-canvas");
let spectrumDrag = null;

function spectrumAxisPoint(event) {
  const rect = spectrumCanvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const height = Math.max(1, rect.height - 35);
  return {inside: x >= 0 && x <= 42 && y >= 12 && y <= rect.height - 23,
    ratio: Math.max(0, Math.min(1, 1 - (y - 12) / height)), height};
}

function applySpectrumGesture(view) {
  setSpectrumView(Math.round(view.minHz * 10) / 10, Math.round(view.maxHz * 10) / 10);
}

spectrumCanvas.addEventListener("wheel", (event) => {
  const point = spectrumAxisPoint(event);
  const view = spectrumViewBounds();
  if (!point.inside || event.ctrlKey || event.metaKey || !view) return;
  event.preventDefault();
  const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? point.height : 1);
  const factor = Math.exp(Math.max(-600, Math.min(600, pixels)) * 0.002);
  applySpectrumGesture(zoomSpectrumView(view, point.ratio, factor, spectrumNyquist()));
}, {passive: false});
spectrumCanvas.addEventListener("pointerdown", (event) => {
  const point = spectrumAxisPoint(event);
  const view = spectrumViewBounds();
  if (!point.inside || event.button !== 0 || !view) return;
  event.preventDefault();
  spectrumCanvas.focus();
  spectrumCanvas.setPointerCapture(event.pointerId);
  spectrumDrag = {id: event.pointerId, y: event.clientY, ratio: point.ratio, height: point.height, view};
  spectrumCanvas.style.cursor = "grabbing";
});
spectrumCanvas.addEventListener("pointermove", (event) => {
  if (spectrumDrag?.id === event.pointerId) {
    event.preventDefault();
    // Drag the axis like paper: pulling downward brings higher frequencies into view.
    const ratio = Math.max(0, Math.min(1, spectrumDrag.ratio - (event.clientY - spectrumDrag.y) / spectrumDrag.height));
    const delta = Math.pow(spectrumDrag.ratio, 1 / SPECTRUM_FREQUENCY_BIAS) - Math.pow(ratio, 1 / SPECTRUM_FREQUENCY_BIAS);
    applySpectrumGesture(panSpectrumView(spectrumDrag.view, delta, spectrumNyquist()));
  } else {
    spectrumCanvas.style.cursor = spectrumAxisPoint(event).inside && spectrumViewBounds() ? "grab" : "";
  }
});
for (const type of ["pointerup", "pointercancel", "lostpointercapture"]) {
  spectrumCanvas.addEventListener(type, (event) => {
    if (spectrumDrag?.id !== event.pointerId) return;
    spectrumDrag = null;
    if (spectrumCanvas.hasPointerCapture(event.pointerId)) spectrumCanvas.releasePointerCapture(event.pointerId);
    spectrumCanvas.style.cursor = "";
  });
}
spectrumCanvas.addEventListener("dblclick", (event) => {
  if (!spectrumAxisPoint(event).inside) return;
  event.preventDefault();
  resetSpectrumView();
});

ui.device.addEventListener("change", () => {
  populateModes();
  if (!ui.device.value) {
    ui.streamEnabled.checked = false;
    syncStreamRequirements();
  }
});
byId("audio-refresh")?.addEventListener("click", refreshAudioDevices);
ui.mode.addEventListener("change", () => populateRepresentations());
ui.representation.addEventListener("change", updateRepresentationDetails);
ui.streamEnabled.addEventListener("change", syncStreamRequirements);
ui.passphrase.addEventListener("input", () => {
  if (ui.passphrase.value) ui.clearPassphrase.checked = false;
});
ui.clearPassphrase.addEventListener("change", () => {
  ui.passphrase.disabled = ui.clearPassphrase.checked;
  if (ui.clearPassphrase.checked) ui.passphrase.value = "";
});
configForm.addEventListener("submit", handleConfigSubmit);
const controlIntents = new WeakMap();
for (const type of ["pointerdown", "keydown"]) document.addEventListener(type, (event) => {
  if (type === "keydown" && (event.repeat || !["Enter", " "].includes(event.key))) return;
  const button = event.target.closest?.("button[data-action]");
  if (button) controlIntents.set(button, button.dataset.action);
}, true);
for (const button of document.querySelectorAll("button[data-action]")) {
  button.addEventListener("click", () => runControl(button));
}
byId("refresh-button").addEventListener("click", async () => {
  try {
    renderStatus(await api("/api/status"));
    setMessage("Status refreshed.");
  } catch (error) {
    renderMediaRequestFailure(error);
    setMessage(error instanceof Error ? error.message : "Status refresh failed.", "error");
  }
});

for (const [id, key, allowed] of [["spectrum-palette", "spectrumPalette", Object.keys(SPECTRUM_PALETTES)], ["meter-palette", "meterPalette", ["classic", "cyan", "mono"]], ["display-theme", "theme", ["dark", "light"]]]) {
  byId(id).addEventListener("change", () => {
    if (!allowed.includes(byId(id).value)) return;
    state.preferences[key] = byId(id).value;
    document.body.dataset.theme = state.preferences.theme;
    state.preferencesDirty = true;
    state.spectrumRaster = null;
    scheduleTelemetryFrame();
  });
}
byId("storage-refresh").addEventListener("click", loadStorage);
byId("recording-player").addEventListener("error", () => {
  if (state.playbackId) setValue("playback-message", "Browser playback failed. Refresh the file list, try Check, or download the original FLAC.");
});
for (const id of ["logs-filter", "logs-level"]) byId(id).addEventListener(id === "logs-filter" ? "input" : "change", () => {
  state.logPage = 0;
  renderLogs(state.status, true);
});
byId("logs-export").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify({exported_at: new Date().toISOString(), scope: "Filtered redacted runtime events; latest reported 32", events: filteredLogs()}, null, 2)], {type: "application/json"});
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a"); link.href = url; link.download = "tinypirelay-events.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
for (const button of document.querySelectorAll("button[data-maintenance]")) button.addEventListener("click", () => runMaintenance(button.dataset.maintenance));
byId("maintenance-retry").addEventListener("click", loadMaintenance);
byId("password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = byId("password-form");
  const button = form.querySelector("button[type=submit]");
  if (!form.reportValidity() || button.disabled) return;
  const current = byId("account-current-password"), password = byId("account-new-password"), confirm = byId("account-confirm-password");
  if (password.value !== confirm.value) { setValue("account-message", "The new passwords do not match."); return; }
  button.disabled = true;
  try {
    const result = await api("/api/account/password", {method: "POST", csrf: true, body: {current_password: current.value, new_password: password.value, confirm_password: confirm.value}});
    if (!result.changed) throw new Error("Password change was not confirmed.");
    showLogin(result.durability_confirmed === false
      ? "Password changed, but durable storage was not confirmed. Sign in with the new password and check device storage."
      : "Password changed. All web sessions are signed out; use your new password.");
  } catch (error) {
    setValue("account-message", error instanceof Error ? error.message : "Password change failed.");
  } finally {
    current.value = ""; password.value = ""; confirm.value = "";
    button.disabled = false;
  }
});

loadVisualPreferences();

async function bootstrap() {
  try {
    const session = await api("/api/session");
    if (!session.authenticated) {
      showLogin("", session.setup_required === true);
      return;
    }
    await enterApplication(session);
  } catch (error) {
    showLogin(error instanceof ApiError && error.status === 401 ? "" : "The web service is unavailable.");
  }
}

bootstrap();
