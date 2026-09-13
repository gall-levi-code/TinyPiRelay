"use strict";

// Native, offline checks for the dashboard's state handling; no browser package needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const nodes = new Map();
const actionButtons = [];
const toggleButtons = [];
const pages = [];
const links = [];
const frames = new Map();
const intervals = [];
let frameId = 0;
let now = 10_000;
let networkCalls = 0;
const canvasOperations = [];
function canvasContext(canvas) {
  return new Proxy({
    canvas,
    measureText: (text) => ({width: String(text).length * 6}),
    createLinearGradient: () => ({addColorStop() {}}),
    createImageData: (width, height) => ({width, height, data: new Uint8ClampedArray(width * height * 4)}),
    putImageData(image, x, y) { canvasOperations.push({kind: "pixels", canvas, image, x, y}); },
    drawImage(image, ...args) { canvasOperations.push({kind: "image", canvas, image, args}); },
    fillRect(x, y, width, height) { canvasOperations.push({kind: "fill", canvas, color: this.fillStyle, x, y, width, height}); },
    clearRect(x, y, width, height) { canvasOperations.push({kind: "clear", canvas, x, y, width, height}); },
  }, {get: (target, key) => key in target ? target[key] : () => {}});
}

function node(id = "") {
  return {
    id, textContent: "", value: "", checked: false, hidden: false,
    disabled: false, dataset: {}, children: [], elements: [], options: [],
    clientWidth: 400, clientHeight: 240, width: 400, height: 240,
    style: {setProperty() {}, removeProperty() {}},
    classList: {add() {}, remove() {}, toggle() {}},
    attributes: new Map(),
    queries: new Map(),
    listeners: new Map(),
    setAttribute(key, value) { this.attributes.set(key, String(value)); },
    getAttribute(key) { return this.attributes.get(key) ?? null; },
    removeAttribute(key) { this.attributes.delete(key); },
    append(...children) { for (const child of children) this.insertBefore(child, null); },
    replaceChildren(...children) {
      for (const child of this.children) child.parentNode = null;
      this.children = []; this.options = this.children; this.append(...children);
    },
    insertBefore(child, reference) {
      child.remove();
      this.children.splice(reference === null ? this.children.length : this.children.indexOf(reference), 0, child);
      child.parentNode = this; this.options = this.children;
    },
    remove() {
      if (!this.parentNode) return;
      const siblings = this.parentNode.children;
      siblings.splice(siblings.indexOf(this), 1); this.parentNode = null;
    },
    querySelector(selector) {
      if (!this.queries.has(selector)) this.queries.set(selector, node());
      return this.queries.get(selector);
    },
    querySelectorAll() { return []; },
    contains(child) { return this.elements.includes(child); },
    addEventListener(type, callback, options) {
      if (!this.listeners.has(type)) this.listeners.set(type, []);
      this.listeners.get(type).push({callback, once: options?.once});
    },
    removeEventListener(type, callback) {
      this.listeners.set(type, (this.listeners.get(type) ?? []).filter((entry) => entry.callback !== callback));
    },
    dispatchEvent(event) {
      event.target ??= this;
      event.currentTarget = this;
      event.preventDefault ??= function () { this.defaultPrevented = true; };
      for (const entry of [...this.listeners.get(event.type) ?? []]) {
        entry.callback(event);
        if (entry.once) this.removeEventListener(event.type, entry.callback);
      }
    },
    showModal() { this.open = true; },
    close(value) {
      if (value !== undefined) this.returnValue = String(value);
      this.open = false;
      this.dispatchEvent({type: "close"});
    },
    focus() {}, reportValidity() { return true; },
    setCustomValidity(value) { this.validationMessage = value; },
    setPointerCapture(id) { this.pointerCapture = id; },
    hasPointerCapture(id) { return this.pointerCapture === id; },
    releasePointerCapture() { this.pointerCapture = null; },
    getContext() { return this.context ??= canvasContext(this); },
    getBoundingClientRect() { return {left: 0, top: 0, width: this.clientWidth, height: this.clientHeight}; },
  };
}

function byId(id) {
  if (!nodes.has(id)) nodes.set(id, node(id));
  return nodes.get(id);
}

for (const branch of ["stream", "recording"]) {
  const button = byId(`${branch}-toggle`);
  button.dataset = {toggle: branch, action: `${branch}.start`};
  toggleButtons.push(button);
  actionButtons.push(button);
}
const secondaryStreamButton = byId("stream-secondary");
secondaryStreamButton.dataset.action = "stream.start";
actionButtons.push(secondaryStreamButton);
for (const name of ["dashboard", "audio", "stream", "recording", "storage", "system", "network", "diagnostics", "settings", "about"]) {
  const page = byId(name);
  page.dataset.page = name;
  pages.push(page);
  const link = node();
  link.setAttribute("href", `#${name}`);
  links.push(link);
}

const sandbox = {
  assert, structuredClone, console, intervals,
  localStorage: {values: new Map(), getItem(key) { return this.values.get(key) ?? null; }, setItem(key, value) { this.values.set(key, String(value)); }},
  Date: class extends Date { static now() { return now; } },
  performance: {now: () => now},
  document: {
    getElementById: byId,
    createElement: () => node(),
    createElementNS: () => node(),
    querySelectorAll: (selector) => ({
      "button[data-action]": actionButtons,
      "button[data-toggle]": toggleButtons,
      ".page-section[data-page]": pages,
      ".section-nav a": links,
    })[selector] ?? [],
    addEventListener() {}, hidden: false,
    documentElement: node("document"), body: node("body"),
  },
  window: {
    devicePixelRatio: 1, addEventListener() {}, confirm: () => true,
    location: {hash: "#dashboard"},
  },
  location: {hash: "#dashboard"},
  requestAnimationFrame(callback) { frames.set(++frameId, callback); return frameId; },
  cancelAnimationFrame(id) { frames.delete(id); },
  setInterval(callback) { intervals.push(callback); return intervals.length; }, clearInterval() {},
  setTimeout() { return 1; }, clearTimeout() {},
  ResizeObserver: class { observe() {} },
  fetch() { networkCalls += 1; throw new Error("The dashboard checks must not access the network."); },
  EventSource: class { constructor() { throw new Error("No network permitted."); } },
};
const context = vm.createContext(sandbox);
const source = fs.readFileSync(path.join(__dirname, "../src/tinypirelay/web_assets/app.js"), "utf8");
assert.match(source, /\nbootstrap\(\);\s*$/);
vm.runInContext(source.replace(/\nbootstrap\(\);\s*$/, "\n"), context, {filename: "app.js"});
const check = (code) => vm.runInContext(code, context, {filename: "dashboard-check"});
const flushFrames = () => {
  const pending = [...frames.values()];
  frames.clear();
  for (const callback of pending) callback(now);
};

check(`
  window.location.protocol = "http:";
  showLogin("", true);
  assert.equal(state.setupRequired, true);
  assert.equal(byId("login-heading").textContent, "Create your account");
  assert.equal(byId("setup-confirm-field").hidden, false);
  assert.equal(byId("setup-confirm-password").disabled, false);
  assert.equal(byId("setup-confirm-password").required, true);
  assert.equal(byId("password").autocomplete, "new-password");
  assert.equal(byId("password").minLength, 12);
  assert.equal(byId("setup-transport").hidden, false);
  assert.equal(loginForm.querySelector("button[type=submit]").textContent, "Create account");
  window.location.protocol = "https:";
  showLogin("", true);
  assert.equal(byId("setup-transport").hidden, true);
  byId("setup-confirm-password").value = "not-retained";
  showLogin("Account created. Sign in.");
  assert.equal(state.setupRequired, false);
  assert.equal(byId("setup-confirm-password").value, "");
  assert.equal(byId("setup-confirm-password").disabled, true);
  assert.equal(byId("setup-confirm-password").required, false);
  assert.equal(byId("password").autocomplete, "current-password");
  assert.equal(byId("password").minLength, 0);
  assert.equal(loginForm.querySelector("button[type=submit]").textContent, "Sign in");
`);
flushFrames();

const longConversion = JSON.parse(fs.readFileSync(path.join(__dirname, "../evidence/audio_capabilities.json"), "utf8"))
  .devices.flatMap(device => device.modes).flatMap(mode => mode.stream_options)
  .find(option => option.conversion.startsWith("Native 24-bit mono is converted")).conversion;
check(`
  const compactConversion = conversionLabel(${JSON.stringify(longConversion)});
  assert.equal(compactConversion.split("\\n").length, 4);
  assert.ok(compactConversion.split("\\n").every(line => line.length <= 44));
  assert.match(compactConversion, /recording: native 24-bit mono/);
  assert.match(compactConversion, /16-bit stereo/);
  assert.match(compactConversion, /does not preserve 24-bit samples/);
  assert.equal(conversionLabel({label: ${JSON.stringify(longConversion)}}), compactConversion);
`);

check(`
  state.mediaAvailable = true;
  state.config = {
    capture: {device_id: "fixture", mode: {format: "S16LE", rate_hz: 48000, channels: 1}},
    stream: {enabled: true, representation_id: "fixture-opus", destination_host: "receiver.local",
      destination_port: 9000, latency_ms: 120, passphrase: {configured: false}},
    recording: {directory: "/recordings"},
    monitoring: {spectrum_updates_per_second: 5, spectrum_bands: 128},
  };
  state.status = {state: {
    service: "running", capture: {state: "running", generation: 1},
    stream: {state: "stopped"}, recording: {state: "stopped"},
    connection: {state: "disconnected", statistics: {}},
  }};
  assert.equal(controlDisabledReason("stream.start"), "");
  state.restartRequired = true;
  assert.match(controlDisabledReason("stream.start"), /restart/i);
  assert.match(controlDisabledReason("recording.start"), /restart/i);
  state.status.state.stream.state = "running";
  state.status.state.recording.state = "running";
  assert.equal(controlDisabledReason("stream.stop"), "");
  assert.equal(controlDisabledReason("recording.stop"), "");
  assert.notEqual(controlDisabledReason("capture.stop"), "");
  state.restartRequired = false;
  state.busyButtons.add({dataset: {action: "stream.stop"}});
  assert.match(controlDisabledReason("stream.stop"), /pending/i);
  assert.match(controlDisabledReason("stream.start"), /pending/i);
  assert.equal(controlDisabledReason("recording.stop"), "");
  state.busyButtons.clear();
  state.status.state.stream.state = "stopping";
  assert.notEqual(controlDisabledReason("stream.start"), "");
  state.mediaAvailable = false;
  assert.notEqual(controlDisabledReason("recording.stop"), "");
`);

check(`
  state.mediaAvailable = true;
  state.route = "dashboard";
  const sample = (sequence) => ({
    meter: {sequence, channels: [{peak_dbfs: -6, rms_dbfs: -20}]},
    spectrum: {sequence, magnitudes_db: [-80, -40, -60]},
  });
  queueTelemetry(sample(1));
  queueTelemetry(sample(2));
`);
assert.equal(frames.size, 1, "Live updates should share one animation frame.");
flushFrames();
check(`
  assert.equal(state.lastMeter.sequence, 2);
  assert.equal(state.lastSpectrum.sequence, 2);
  assert.equal(state.spectrumColumns.length, 2);
  assert.equal(document.getElementById("level-values").children.length, 1);
`);
now += 2500;
check("queueTelemetry(sample(2));");
flushFrames();
check("assert.equal(state.meterReceivedAt, 10000);");
now += 1000;
check("queueTelemetry(sample(2));");
flushFrames();
check(`
  assert.equal(state.lastMeter, null, "Repeated stale samples must not look live.");
  assert.equal(state.lastSpectrum, null);
  assert.equal(state.spectrumColumns.length, 0);
  assert.equal(document.getElementById("level-values").children.length, 0);
  queueTelemetry(sample(3));
`);
flushFrames();
check(`
  assert.equal(state.lastMeter.sequence, 3);
  queueTelemetry(null);
`);
flushFrames();
check(`
  assert.equal(state.lastMeter, null);
  assert.equal(state.lastSpectrum, null);
  queueTelemetry(sample(4));
  clearTelemetry();
  assert.equal(state.telemetryFrame, null);
  assert.equal(state.meterSequence, null);
  assert.equal(state.spectrumColumns.length, 0);
  renderMeters({channels: Array.from({length: 20}, () => ({peak_dbfs: -6, rms_dbfs: -20}))});
  assert.equal(document.getElementById("level-values").children.length, 8);
  renderSpectrum({magnitudes_db: Array(3000).fill(-80)});
  assert.match(document.getElementById("spectrum-summary").textContent, /2048 bins/);
  clearTelemetry();
`);
assert.equal(frames.size, 0, "Clearing telemetry should cancel queued rendering.");

check(`
  state.mediaAvailable = true;
  state.route = "dashboard";
  state.config.monitoring.spectrum_updates_per_second = 60;
`);
for (let sequence = 1; sequence <= 120; sequence += 1) {
  now += 1000 / 60;
  check(`queueTelemetry(sample(${sequence}));`);
  flushFrames();
}
check(`
  assert.equal(state.spectrumColumns.length, 120, "The linear history should retain distinct 60 Hz samples.");
  assert.equal(spectrumTimePosition(0), 1);
  assert.equal(spectrumTimePosition(15000), 0.5);
  assert.equal(spectrumTimePosition(30000), 0);
  assert.equal(spectrumTimePosition(-1000), 1);
  assert.equal(spectrumTimePosition(120000), 0);
  assert.ok(Math.abs(spectrumTimePosition(5000) - spectrumTimePosition(10000)
    - (spectrumTimePosition(20000) - spectrumTimePosition(25000))) < 1e-12,
    "Equal time intervals must occupy equal widths.");
  assert.ok(1 - spectrumFrequencyPosition(4000, 24000) > 0.35);
  assert.ok(1 - spectrumFrequencyPosition(4000, 24000) < 0.40);
  const rawColumns = [
    {time: 209999, values: [-10, -10]},
    {time: 210000, values: [-100, -65]},
    {time: 234000, values: [-80, -70]},
    {time: 234100, values: [-50, -90]},
    {time: 239900, values: [-80, -70]},
    {time: 239910, values: [-60, -90]},
  ];
  const originalColumns = JSON.stringify(rawColumns);
  const compacted = compactSpectrumColumns(rawColumns, 240000);
  assert.equal(compacted.length, 6, "Keep one boundary predecessor when it can cover the first displayed pixel.");
  assert.equal(compacted[0].time, 209999);
  assert.equal(compacted[2].values.join(","), "-80,-70", "Older samples must not be merged into changing peak buckets.");
  assert.equal(compacted[3].values.join(","), "-50,-90");
  assert.equal(JSON.stringify(rawColumns), originalColumns, "Trimming must not mutate retained samples.");
  assert.equal(compactSpectrumColumns([
    {time: 230000, values: [-80, -70]},
    {time: 230100, values: [-60]},
  ], 240000).length, 2, "A spectrum band-count change must start a new column.");
  const bounded = compactSpectrumColumns(Array.from({length: 7201}, (_, index) => ({
    time: index * 1000 / 60,
    values: [-100 + index % 20, -90],
  })), 120000);
  assert.equal(bounded.length, 1801);
  assert.ok(bounded.every((column) => 120000 - column.time <= 30000));
  assert.equal(compactSpectrumColumns([
    {time: NaN, values: [-20]}, {time: 240001, values: [-20]},
    {time: 240000, values: null}, {time: 240000, values: [-30]},
  ], 240000).length, 1);
  clearTelemetry();
`);

const historyStartedAt = now;
for (let sequence = 1; sequence <= 1861; sequence += 1) {
  now = historyStartedAt + sequence * 1000 / 60;
  check(`queueTelemetry(sample(${sequence}));`);
}
check(`
  assert.ok(state.spectrumColumns.length <= 1801 && state.spectrumColumns.length >= 1800);
  assert.ok(state.spectrumColumns.filter((column) => performance.now() - column.time > 30000).length <= 1);
  assert.ok(state.spectrumColumns.every((column) => performance.now() - column.time <= 30250));
  assert.equal(state.spectrumColumns.at(-1).time, performance.now());
`);
flushFrames();
check(`
  const savedConfigForView = JSON.stringify(state.config);
  assert.equal(setSpectrumView(22.3, 32.3), true, "Decimal bounds separated by exactly 10 Hz must survive floating-point rounding.");
  assert.equal(setSpectrumView(1000, 8000), true);
  assert.equal(spectrumFrequencyPosition(1000, 8000, 1000), 0);
  assert.equal(spectrumFrequencyPosition(8000, 8000, 1000), 1);
  assert.equal(spectrumFrequencyPosition(500, 8000, 1000), 0);
  assert.equal(spectrumFrequencyPosition(12000, 8000, 1000), 1);
  const validView = JSON.stringify(state.spectrumView);
  for (const bounds of [[-1, 8000], [20, 20], [8000, 1000], [20, 24001], [NaN, 4000], [20, Infinity], [20, 25]]) {
    assert.equal(setSpectrumView(...bounds), false, "Invalid bounds must be rejected.");
    assert.equal(JSON.stringify(state.spectrumView), validView, "Invalid bounds must preserve the current view.");
  }
  const view = {minHz: 1000, maxHz: 8000};
  const zoomed = zoomSpectrumView(view, 0.5, 0.5, 24000);
  assert.ok(zoomed.minHz > view.minHz && zoomed.maxHz < view.maxHz);
  const anchorHz = view.minHz * Math.pow(view.maxHz / view.minHz, Math.pow(0.5, 1 / SPECTRUM_FREQUENCY_BIAS));
  assert.ok(Math.abs(spectrumFrequencyPosition(anchorHz, zoomed.maxHz, zoomed.minHz) - 0.5) < 1e-10,
    "Zooming must preserve the frequency under the cursor.");
  assert.ok(Math.abs(zoomSpectrumView(view, 0, 0.5, 24000).minHz - view.minHz) < 1e-8);
  assert.ok(Math.abs(zoomSpectrumView(view, 1, 0.5, 24000).maxHz - view.maxHz) < 1e-8);
  const upward = panSpectrumView(view, 0.1, 24000);
  assert.ok(upward.minHz > view.minHz && upward.maxHz > view.maxHz);
  assert.ok(Math.abs(upward.maxHz / upward.minHz - view.maxHz / view.minHz) < 1e-10);
  const highClamp = panSpectrumView(view, 100, 24000);
  const lowClamp = panSpectrumView(view, -100, 24000);
  assert.ok(highClamp.maxHz <= 24000 && Math.abs(highClamp.maxHz - 24000) < 1e-6);
  assert.ok(lowClamp.minHz >= 20 && Math.abs(lowClamp.minHz - 20) < 1e-6);
  const out = zoomSpectrumView(view, 0.5, 100, 24000);
  assert.ok(out.minHz >= 20 && out.maxHz <= 24000);
  resetSpectrumView();
  assert.equal(state.spectrumView, null);
  assert.equal(JSON.stringify(state.config), savedConfigForView, "Display adjustments must not edit capture configuration.");
  clearTelemetry();
`);
assert.equal(networkCalls, 0, "Display adjustments must not issue media-control or configuration requests.");

// Exercise the actual controls/listeners, including browser-zoom and form-submit isolation.
check(`
  const lowerInput = byId("spectrum-min-hz");
  const upperInput = byId("spectrum-max-hz");
  lowerInput.value = "1000";
  upperInput.value = "8000";
  lowerInput.dispatchEvent({type: "change"});
  assert.equal(state.spectrumView.minHz, 1000);
  assert.equal(state.spectrumView.maxHz, 8000);
  upperInput.value = "not-a-frequency";
  const invalidEnter = {type: "keydown", key: "Enter"};
  upperInput.dispatchEvent(invalidEnter);
  assert.equal(invalidEnter.defaultPrevented, true, "Enter on display bounds must not submit the capture form.");
  assert.equal(state.spectrumView.maxHz, 8000);
  assert.ok(upperInput.validationMessage);
  syncControlAvailability();
  assert.equal(upperInput.value, "not-a-frequency", "Background status updates must not replace an unfinished bounds edit.");
  upperInput.value = "8000";
  upperInput.dispatchEvent({type: "change"});
  assert.equal(upperInput.validationMessage, "");
  const canvasForGestures = byId("spectrum-canvas");
  for (const extra of [{clientX: 200}, {ctrlKey: true}, {metaKey: true}]) {
    const ignoredWheel = {type: "wheel", clientX: 20, clientY: 100, deltaY: -120, deltaMode: 0, ...extra};
    canvasForGestures.dispatchEvent(ignoredWheel);
    assert.equal(Boolean(ignoredWheel.defaultPrevented), false, "Only plain frequency-axis wheel gestures are intercepted.");
    assert.equal(state.spectrumView.minHz, 1000);
    assert.equal(state.spectrumView.maxHz, 8000);
  }
  const axisWheel = {type: "wheel", clientX: 20, clientY: 100, deltaY: -120, deltaMode: 0};
  canvasForGestures.dispatchEvent(axisWheel);
  assert.equal(axisWheel.defaultPrevented, true);
  assert.ok(state.spectrumView.minHz > 1000 && state.spectrumView.maxHz < 8000);
  assert.equal(Number(lowerInput.value), state.spectrumView.minHz, "Axis gestures must update the editable bounds.");
  const viewBeforeDrag = {...state.spectrumView};
  canvasForGestures.dispatchEvent({type: "pointerdown", clientX: 20, clientY: 100, pointerId: 7, button: 0});
  assert.equal(canvasForGestures.hasPointerCapture(7), true);
  canvasForGestures.dispatchEvent({type: "pointermove", clientX: 20, clientY: 125, pointerId: 7});
  assert.ok(state.spectrumView.minHz > viewBeforeDrag.minHz && state.spectrumView.maxHz > viewBeforeDrag.maxHz);
  canvasForGestures.dispatchEvent({type: "pointerup", pointerId: 7});
  assert.equal(canvasForGestures.hasPointerCapture(7), false);
  const releasedView = JSON.stringify(state.spectrumView);
  canvasForGestures.dispatchEvent({type: "pointermove", clientX: 20, clientY: 150, pointerId: 7});
  assert.equal(JSON.stringify(state.spectrumView), releasedView, "Released dragging must not continue panning.");
  canvasForGestures.dispatchEvent({type: "dblclick", clientX: 20, clientY: 100});
  assert.equal(state.spectrumView, null);
  assert.equal(Number(lowerInput.value), 20);
  assert.equal(Number(upperInput.value), 24000);
  setSpectrumView(1000, 8000);
  byId("spectrum-view-reset").dispatchEvent({type: "click"});
  assert.equal(state.spectrumView, null);
  setSpectrumView(1000, 22000);
  state.config.capture.mode.rate_hz = 32000;
  syncControlAvailability();
  assert.equal(Number(upperInput.value), 16000, "Changing the capture rate must clamp a now-invalid view to Nyquist.");
  assert.equal(state.spectrumView, null);
  assert.equal(setSpectrumView(1000, 16001), false);
  state.restartRequired = true;
  assert.equal(setSpectrumView(1000, 8000), false, "An unconfirmed capture format must not supply a misleading frequency scale.");
  state.restartRequired = false;
  state.config.capture.mode.rate_hz = 48000;
  resetSpectrumView();
  assert.equal(JSON.stringify(state.config), savedConfigForView);
`);
assert.equal(networkCalls, 0, "Bounds, wheel zoom, dragging, and reset must never send media/configuration requests.");

canvasOperations.length = 0;
check(`
  clearTelemetry();
  queueTelemetry(sample(900));
  state.spectrumColumns = Array.from({length: 120}, (_, index) => ({
    time: performance.now() - (119 - index) * 1000 / 60, values: [-80, -40, -60],
  }));
  const sharedViewFrame = state.telemetryFrame;
  for (const bounds of [[1000, 8000], [1200, 6000], [1400, 5500]]) {
    setSpectrumView(...bounds);
    assert.equal(state.telemetryFrame, sharedViewFrame, "Rapid frequency gestures and arriving telemetry must share one frame.");
    assert.equal(state.spectrumRaster, null, "The expensive replay must wait for the animation frame.");
  }
`);
assert.equal(frames.size, 1);
assert.equal(canvasOperations.filter((operation) => operation.kind === "pixels").length, 0);
flushFrames();
const gestureRaster = check("state.spectrumRaster");
assert.ok(gestureRaster);
assert.equal(canvasOperations.filter((operation) => operation.kind === "image"
  && operation.canvas === byId("spectrum-canvas") && operation.image === gestureRaster.plot).length, 1,
"A burst of view changes must produce only one final display repaint.");
check(`
  setSpectrumView(1400, 5500);
  assert.equal(state.telemetryFrame, null, "Unchanged bounds must not schedule a history replay.");
  resetSpectrumView();
  clearTelemetry();
`);

// Inspect actual bitmap calls, not just helper math. These are offline canvas stubs, not browser QA.
const spectrumCanvas = byId("spectrum-canvas");
spectrumCanvas.clientWidth = 1241;
spectrumCanvas.clientHeight = 341;
const stripSpans = () => {
  const raster = check("state.spectrumRaster");
  return canvasOperations.filter((operation) => operation.kind === "image"
    && operation.canvas === raster.plot && operation.image === raster.strip)
    .map((operation) => ({x: operation.args[4], y: operation.args[5], width: operation.args[6], height: operation.args[7]}));
};
for (const scale of [1, 1.25, 1.5, 2]) {
  sandbox.window.devicePixelRatio = scale;
  canvasOperations.length = 0;
  check(`
    state.spectrumRaster = null;
    state.spectrumColumns = [220, 170, 20].map((age) => ({time: performance.now() - age, values: [-60]}));
    renderSpectrum({magnitudes_db: [-60]});
  `);
  const raster = check("state.spectrumRaster");
  const spans = stripSpans();
  assert.equal(spans.length, 3);
  assert.ok(spans.every(({x, y, width, height}) => [x, y, width, height].every(Number.isInteger)
    && x >= 0 && y === 0 && width > 0 && height === raster.plot.height && x + width <= raster.plot.width),
  `Heatmap strips must use positive integer device-pixel bounds at DPR ${scale}.`);
  assert.ok(spans.slice(1).every((span, index) => span.x <= spans[index].x + spans[index].width),
    "Short delivery pauses must not paint black stripes.");
  assert.equal(spans.at(-1).x + spans.at(-1).width, raster.plot.width, "Paint must meet the live edge.");
  const pixels = canvasOperations.find((operation) => operation.kind === "pixels").image;
  assert.equal(pixels.width, 1);
  assert.equal(pixels.height, raster.plot.height);
  for (let index = 3; index < pixels.data.length; index += 4) assert.equal(pixels.data[index], 255, "A spectrum strip must be opaque.");
  assert.ok(pixels.data[0] + pixels.data[1] + pixels.data[2] > 0, "Measured spectrum values must generate color pixels.");
}
sandbox.window.devicePixelRatio = 1.25;
canvasOperations.length = 0;
check(`
  state.spectrumRaster = null;
  state.spectrumColumns = [1200, 200].map((age) => ({time: performance.now() - age, values: [-60]}));
  renderSpectrum({magnitudes_db: [-60]});
`);
const gapSpans = stripSpans();
assert.equal(gapSpans.length, 2);
assert.ok(gapSpans[1].x > gapSpans[0].x + gapSpans[0].width, "A one-second interruption must remain visible.");
const beforeStall = check("state.spectrumRaster.time");
now += 1500;
canvasOperations.length = 0;
check(`
  state.spectrumColumns.push({time: performance.now() - 20, values: [-50]});
  renderSpectrum({magnitudes_db: [-50]});
`);
const stalledRaster = check("state.spectrumRaster");
const expectedShift = Math.floor((now - beforeStall) * stalledRaster.plot.width / 30000);
const shifted = canvasOperations.find((operation) => operation.kind === "image" && operation.image === stalledRaster.plot && operation.canvas === stalledRaster.plot);
assert.equal(shifted.args[0], expectedShift, "A browser stall must advance by real elapsed time, not a capped animation delta.");
assert.ok(stripSpans().every((span) => span.x >= stalledRaster.plot.width - expectedShift));
assert.ok(stripSpans().reduce((sum, span) => sum + span.width, 0) < expectedShift / 2,
  "The new sample must not fill the missing interval after a stall.");

// After history is populated, normal frames append only a few strips rather than repainting 30 s.
check(`
  state.spectrumRaster = null;
  state.spectrumColumns = Array.from({length: 1801}, (_, index) => ({
    time: performance.now() - (1800 - index) * 1000 / 60, values: [-60],
  }));
  renderSpectrum({magnitudes_db: [-60]});
`);
const steadyRaster = check("state.spectrumRaster");
for (let frame = 1; frame <= 6; frame += 1) {
  now += 1000 / 60;
  canvasOperations.length = 0;
  check(`
    state.spectrumColumns.push({time: performance.now(), values: [-50]});
    state.spectrumColumns = compactSpectrumColumns(state.spectrumColumns, performance.now());
    renderSpectrum({magnitudes_db: [-50]});
  `);
  assert.equal(check("state.spectrumRaster"), steadyRaster, "Steady-state frames must reuse their bitmap buffers.");
  assert.ok(stripSpans().length <= 4, "One normal frame must not repaint every historical sample.");
  assert.ok(canvasOperations.filter((operation) => operation.kind === "pixels").length <= 4);
}
canvasOperations.length = 0;
check("renderSpectrum({magnitudes_db: [-50]});");
assert.equal(stripSpans().length, 0, "A duplicate frame at the same time must not recalculate spectrum strips.");
check(`
  state.spectrumSequence = 100;
  queueTelemetry(sample(1));
  assert.equal(state.spectrumColumns.length, 1, "A media sequence reset must start a new history.");
  assert.equal(state.spectrumRaster, null, "A media sequence reset must discard the old bitmap immediately.");
`);
flushFrames();
assert.notEqual(check("state.spectrumRaster"), steadyRaster);
check(`
  queueTelemetry({spectrum: {sequence: 2, magnitudes_db: [-60, NaN, -20]}});
  assert.equal(state.spectrumColumns.at(-1).values.join(","), "-60,-120,-20",
    "An invalid band must become the floor without shifting higher-frequency bins.");
`);
flushFrames();
check(`
  queueTelemetry({spectrum: {sequence: 3, magnitudes_db: [-60, -20]}});
  assert.equal(state.spectrumColumns.length, 1, "A changed FFT bin count must discard incompatible history.");
  assert.equal(state.spectrumRaster, null);
  clearTelemetry();
  assert.equal(state.spectrumRaster, null);
`);
sandbox.window.devicePixelRatio = 1;
spectrumCanvas.clientWidth = 400;
spectrumCanvas.clientHeight = 240;
canvasOperations.length = 0;

check(`
  state.revision = "r1";
  state.representationMetadata.set("fixture-opus", {codec: "Opus"});
  const liveStatus = {
    active_config_revision: "r1", saved_config_revision: "r1", restart_required: false,
    state: {
      service: "running", capture: {state: "running", generation: 1},
      stream: {state: "running", representation_id: "fixture-opus"},
      recording: {state: "running", current_file: "/recordings/sample.flac"},
      connection: {state: "connected", statistics: {
        "send-rate-mbps": 0.42, "rtt-ms": 3.2, "packets-retransmitted": 4,
        "packets-sent-total": 100, "packets-sent-lost": 0,
        "passphrase": "do-not-render",
      }},
    },
  };
  renderStatus(liveStatus);
  assert.equal(byId("srt-tx").textContent, "0.42 Mbps");
  assert.equal(byId("srt-rtt").textContent, "3.2 ms");
  assert.equal(byId("srt-retransmits").textContent, "4");
  assert.match(byId("diagnostic-srt").textContent, /packets-sent-total: 100/);
  assert.doesNotMatch(byId("diagnostic-srt").textContent, /do-not-render/);
  assert.equal(byId("stream-toggle").dataset.action, "stream.stop");
  assert.equal(byId("recording-toggle").dataset.action, "recording.stop");
  assert.equal(byId("stream-toggle").querySelector(".action-label").textContent, "Stop stream");
  assert.equal(byId("stream-toggle").querySelector("use").getAttribute("href"), "#icon-stop");
  const reconnecting = structuredClone(liveStatus);
  reconnecting.state.connection.state = "retry_wait";
  renderStatus(reconnecting);
  assert.equal(byId("srt-tx").textContent, "—");
  assert.equal(byId("srt-rtt").textContent, "—");
  assert.equal(byId("srt-retransmits").textContent, "—");
  assert.equal(byId("diagnostic-srt").textContent, "No live connection");
  const stopped = structuredClone(liveStatus);
  stopped.state.stream.state = "stopped";
  stopped.state.recording.state = "stopped";
  renderStatus(stopped);
  assert.equal(byId("srt-tx").textContent, "—");
  assert.equal(byId("stream-toggle").dataset.action, "stream.start");
  assert.equal(byId("stream-toggle").querySelector("use").getAttribute("href"), "#icon-play");
  assert.equal(byId("recording-toggle").dataset.action, "recording.start");
  assert.equal(byId("stream-secondary").disabled, false);
  state.busyButtons.add(byId("stream-toggle"));
  syncControlAvailability();
  assert.equal(byId("stream-toggle").disabled, true);
  assert.equal(byId("stream-secondary").disabled, true);
  assert.equal(byId("recording-toggle").disabled, false);
  state.busyButtons.clear();
  markLiveDisconnected("Test connection lost");
  assert.notEqual(state.config, null, "A transport disconnect must preserve the editable draft.");
  assert.equal(byId("header-status").textContent, "Reconnecting");
  assert.equal(byId("srt-tx").textContent, "—");
  assert.equal(byId("stream-toggle").disabled, true);
  const draftBaseline = state.config, draftRevision = state.revision;
  renderStatus({...liveStatus, media_available: false});
  assert.equal(state.config, draftBaseline, "Media outages must preserve the draft's baseline, not auto-hydrate on return.");
  assert.equal(state.revision, draftRevision, "Keep the old expected revision so concurrent saves still conflict.");
  assert.equal(state.lastMeter, null);
  assert.equal(byId("header-connection-state").textContent, "Unavailable");
  assert.equal(byId("header-recording-state").textContent, "Unavailable");
  assert.equal(byId("diagnostic-srt").textContent, "No live connection");
  assert.equal(byId("srt-tx").textContent, "—");
  renderLogs({diagnostics: {events: Array.from({length: 100}, (_, index) => ({
    timestamp_unix: 1000 + index, code: "fixture", message: String(index),
  }))}}, true);
  assert.equal(byId("log-list").children.length, 4);
  state.logPage = 100;
  renderLogs({diagnostics: {events: Array.from({length: 100}, (_, index) => ({
    timestamp_unix: 1000 + index, code: "fixture", message: String(index),
  }))}}, true);
  assert.equal(state.logPage, 7, "Logs should have at most 32 retained events, four per page.");
  window.location.hash = "#audio";
  setRoute();
  assert.equal(state.route, "audio");
  assert.equal(byId("audio").hidden, false);
  assert.equal(byId("dashboard").hidden, true);
  assert.equal(byId("config-save-bar").hidden, false);
  window.location.hash = "#logs";
  setRoute();
  assert.equal(state.route, "diagnostics");
  assert.equal(byId("config-save-bar").hidden, true);
  window.location.hash = "#invalid-route";
  setRoute();
  assert.equal(state.route, "dashboard");
`);
flushFrames();

check(`
  const deviceFixture = (sequence) => ({
    sequence, sampled_at_unix: 1788393600 + sequence * 2, config_revision: "r1",
    device: {hostname: "fixture-pi", model: "Synthetic Pi", os: "Fixture OS", architecture: "armv6l", software_version: "fixture"},
    system: {cpu_percent: 0, temperature_c: 43.2, memory_total_bytes: 512000000,
      memory_available_bytes: 256000000, memory_used_bytes: 256000000,
      load_average: [0, 0.5, 1], uptime_seconds: 90000},
    storage: {directory: "/recordings", mountpoint: "/", filesystem: "ext4", device: "/dev/fixture",
      total_bytes: 128000000000, used_bytes: 8000000000, available_bytes: 120000000000,
      used_percent: 6.25, recordings_bytes: 5000000000, recordings_complete: true,
      read_bytes_per_second: 0, write_bytes_per_second: 64000, stop_percent: 90,
      seconds_until_limit: 3600, safe: true, reason: null},
    network: {interface: "wlan0", address: "192.0.2.94", kind: "wifi", operstate: "up",
      speed_mbps: null, duplex: null, rx_bytes_per_second: 0, tx_bytes_per_second: 125000},
    recording: {file: "/recordings/sample.flac", size_bytes: 5000000000, elapsed_seconds: 3600},
  });
  const deviceStatus = (sequence) => {
    const payload = structuredClone(liveStatus);
    payload.device_telemetry = deviceFixture(sequence);
    Object.assign(payload.state.recording, {elapsed_file: "/recordings/sample.flac", elapsed_seconds: 3612});
    return payload;
  };
  assert.equal(formatBytes(null), "—");
  assert.equal(formatBytes(NaN), "—");
  assert.equal(formatBytes(Infinity), "—");
  assert.equal(formatBytes(-1), "—");
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(5000000000), "5.0 GB");
  assert.equal(formatBytes(64000, true), "64.0 kB/s");
  assert.equal(elapsedClock(0), "00:00:00");
  assert.equal(elapsedClock(90000), "25:00:00");
  assert.equal(elapsedClock(null), "—");
  clearDeviceTelemetry(true);
  renderStatus(deviceStatus(10));
  assert.equal(byId("device-hostname").textContent, "fixture-pi");
  assert.equal(byId("device-os").textContent, "Fixture OS");
  assert.equal(byId("software-version").textContent, "fixture");
  assert.equal(byId("system-cpu").textContent, "0%");
  assert.equal(byId("system-temperature").textContent, "43.2°C");
  assert.equal(byId("system-load").textContent, "0.00 · 0.50 · 1.00");
  assert.equal(byId("system-memory").textContent, "256 MB / 512 MB");
  assert.equal(byId("system-uptime").textContent, "1d 1h");
  assert.equal(byId("storage-recordings").textContent, "5.0 GB");
  assert.equal(byId("storage-capacity").textContent, "8.0 GB / 128 GB");
  assert.equal(byId("storage-free").textContent, "120 GB");
  assert.equal(byId("storage-read").textContent, "0 B/s");
  assert.equal(byId("storage-write").textContent, "64.0 kB/s");
  assert.equal(byId("storage-usage").getAttribute("aria-valuenow"), "6.25");
  assert.equal(byId("network-link").textContent, "Wi-Fi");
  assert.equal(byId("network-tx").textContent, "1.00 Mbps");
  assert.equal(byId("network-rx").textContent, "0.00 Mbps");
  assert.equal(byId("recording-size").textContent, "5.0 GB");
  assert.equal(byId("recording-duration").textContent, "01:00:12", "Runtime elapsed is authoritative.");
  assert.equal(byId("storage-eta").textContent, "~1h 0m");

  const zero = deviceStatus(11);
  Object.assign(zero.device_telemetry.storage, {used_bytes: 0, used_percent: 0, recordings_bytes: 0,
    recordings_complete: false, read_bytes_per_second: 0, write_bytes_per_second: 0, seconds_until_limit: 0});
  Object.assign(zero.device_telemetry.system, {cpu_percent: 0, temperature_c: 0, memory_used_bytes: 0});
  zero.device_telemetry.recording.size_bytes = 0;
  zero.state.recording.elapsed_seconds = 0;
  renderStatus(zero);
  assert.equal(byId("system-temperature").textContent, "0.0°C");
  assert.equal(byId("system-memory").textContent, "0 B / 512 MB");
  assert.equal(byId("storage-percent").textContent, "0%");
  assert.equal(byId("storage-recordings").textContent, "≥ 0 B");
  assert.equal(byId("storage-usage").getAttribute("aria-valuenow"), "0");
  assert.equal(byId("storage-eta").textContent, "Limit reached");
  assert.equal(byId("recording-size").textContent, "0 B");
  assert.equal(byId("recording-duration").textContent, "00:00:00");

  const signedTemperature = deviceStatus(12);
  signedTemperature.device_telemetry.system.temperature_c = -4.5;
  renderStatus(signedTemperature);
  assert.equal(byId("system-temperature").textContent, "-4.5°C");
  assert.equal(state.deviceHistory.at(-1).temperature, -4.5);

  const missing = deviceStatus(13);
  for (const field of ["device", "system", "storage", "network", "recording"]) missing.device_telemetry[field] = {};
  renderStatus(missing);
  for (const id of ["device-hostname", "system-cpu", "system-temperature", "system-memory", "storage-percent",
    "storage-free", "storage-read", "storage-write", "network-tx", "network-rx", "recording-size"]) {
    assert.equal(byId(id).textContent, "—", id + " should not turn missing values into zero.");
  }
  assert.equal(byId("storage-usage").getAttribute("aria-valuenow"), null);
  assert.equal(byId("storage-usage").getAttribute("role"), "img");
  assert.equal(byId("recording-duration").textContent, "01:00:12");

  const fallback = deviceStatus(14);
  delete fallback.state.recording.elapsed_file;
  delete fallback.state.recording.elapsed_seconds;
  renderStatus(fallback);
  assert.equal(byId("recording-duration").textContent, "01:00:00");
  const rotatedDevice = deviceStatus(14);
  Object.assign(rotatedDevice.state.recording, {current_file: "/recordings/next.flac",
    last_finalized_file: "/recordings/sample.flac", elapsed_file: "/recordings/next.flac", elapsed_seconds: 0});
  renderStatus(rotatedDevice);
  assert.equal(byId("recording-file").textContent, "next.flac");
  assert.equal(byId("recording-size").textContent, "—", "Old file size must not label a newly rotated file.");
  assert.equal(byId("recording-duration").textContent, "00:00:00");
  rotatedDevice.device_telemetry.sequence = 15;
  rotatedDevice.device_telemetry.recording = {file: "/recordings/next.flac", size_bytes: 128, elapsed_seconds: 0};
  renderStatus(rotatedDevice);
  assert.equal(byId("recording-size").textContent, "128 B");
  rotatedDevice.state.recording.state = "stopped";
  rotatedDevice.state.recording.current_file = null;
  rotatedDevice.state.recording.last_finalized_file = "/recordings/next.flac";
  rotatedDevice.state.recording.elapsed_seconds = 5;
  renderStatus(rotatedDevice);
  assert.equal(byId("recording-file-label").textContent, "Last file");
  assert.equal(byId("recording-duration").textContent, "00:00:05");
  assert.equal(byId("storage-eta").textContent, "Not recording");
  const wrongRevision = deviceStatus(16);
  wrongRevision.device_telemetry.config_revision = "r2";
  renderStatus(wrongRevision);
  assert.equal(state.deviceSample, null);
  assert.equal(byId("storage-percent").textContent, "—");
  assert.equal(byId("recording-size").textContent, "—");
  renderStatus(deviceStatus(16));
  const firstDeviceAt = state.deviceReceivedAt;
  const firstDeviceHistoryLength = state.deviceHistory.length;
`);
now += 2000;
check(`
  renderStatus(deviceStatus(16));
  assert.equal(state.deviceReceivedAt, firstDeviceAt, "Duplicate device sequence must not renew freshness.");
  assert.equal(state.deviceHistory.length, firstDeviceHistoryLength);
`);
now += 6000;
check(`
  checkDeviceTelemetryFreshness();
  assert.equal(state.deviceSample, null);
  assert.equal(state.deviceHistory.length, 0);
  assert.equal(byId("system-cpu").textContent, "—");
  assert.equal(byId("storage-percent").textContent, "—");
  assert.equal(byId("recording-size").textContent, "—");
  renderStatus(deviceStatus(16));
  assert.equal(state.deviceSample, null, "A stale repeated snapshot must not revive telemetry.");
  renderStatus(deviceStatus(17));
  assert.notEqual(state.deviceSample, null);
  markLiveDisconnected("Device telemetry disconnect fixture");
  assert.equal(byId("device-hostname").textContent, "—");
  assert.equal(byId("network-tx").textContent, "—");
  assert.equal(byId("recording-duration").textContent, "—");
  renderStatus(deviceStatus(17));
  assert.equal(state.deviceSample, null);
  renderStatus(deviceStatus(18));
  assert.equal(byId("system-cpu").textContent, "0%");
  renderStatus(deviceStatus(1));
  assert.equal(state.deviceHistory.length, 1, "Collector sequence reset must start a new trace.");
`);
for (let sequence = 2; sequence <= 81; sequence += 1) {
  now += 1000;
  check(`renderStatus(deviceStatus(${sequence}));`);
}
check(`
  assert.equal(state.deviceHistory.length, 32);
  assert.ok(state.deviceHistory.every((point) => performance.now() - point.time <= 60000));
`);
now += 61000;
check(`
  renderStatus(deviceStatus(82));
  assert.equal(state.deviceHistory.length, 1);
  const changedSource = deviceStatus(83);
  changedSource.active_config_revision = changedSource.saved_config_revision = "r2";
  changedSource.device_telemetry.config_revision = "r2";
  changedSource.device_telemetry.storage.device = "/dev/other";
  changedSource.device_telemetry.network.interface = "eth0";
  renderStatus(changedSource);
  assert.equal(state.deviceHistory.at(-1).storageKey, "r2:/dev/other:/");
  assert.equal(state.deviceHistory.at(-1).networkKey, "eth0");
  for (const invalid of [
    {...deviceStatus(84), device_telemetry: null},
    {...deviceStatus(84), device_telemetry: {...deviceFixture(84), sequence: -1}},
    {...deviceStatus(84), device_telemetry: {...deviceFixture(84), sampled_at_unix: NaN}},
    {...deviceStatus(84), active_config_revision: null},
  ]) {
    renderStatus(deviceStatus(84));
    renderStatus(invalid);
    assert.equal(state.deviceSample, null);
  }
  clearDeviceTelemetry(true);
`);
flushFrames();

check(`
  state.storage = {page: 1, page_size: 6, total: 2, total_pages: 1, items: [
    {file_id: "active", name: "active.flac", size_bytes: 1000, modified_unix: 1788960840, status: "active", check: {state: "unchecked"}, can_play: false, can_download: false, can_delete: false, can_check: false, can_recover: false, recovery_unavailable_reason: "Stop recording before recovery."},
    {file_id: "opaque&?id", name: "<untrusted>.flac", size_bytes: 2000, duration_seconds: 3, modified_unix: 1788960840, status: "finalized", check: {state: "passed"}, can_play: true, can_download: true, can_delete: true, can_check: true, can_recover: true},
  ]};
  renderStorage();
  assert.equal(byId("file-list").children.length, 2);
  const activeActions = byId("file-list").children[0].children[4].children;
  assert.ok(activeActions.every(button => button.hidden || button.disabled), "Active recordings must not expose file actions.");
  const finalized = byId("file-list").children[1];
  assert.equal(finalized.children[0].children[0].textContent, "<untrusted>.flac", "Filenames remain text, never markup.");
  assert.match(finalized.children[0].children[1].textContent, /UTC$/);
  assert.equal(fileUrl(state.storage.items[1], true), "/api/storage/file?id=opaque%26%3Fid&download=1");
  const fileButtons = [...finalized.children[4].children];
  assert.equal(fileButtons.length, 5);
  assert.equal(fileButtons[2].textContent, "Check");
  assert.equal(fileButtons[3].getAttribute("aria-label"), "Recover <untrusted>.flac");
  assert.equal(fileButtons[4].getAttribute("aria-label"), "Delete <untrusted>.flac", "Trash stays last.");
  assert.match(activeActions[3].title, /Stop recording/);
  const pageButtons = [...byId("storage-pagination").children];
  state.storage = structuredClone(state.storage);
  state.storage.items[0].size_bytes += 1;
  renderStorage();
  assert.equal(byId("file-list").children[1], finalized, "Other file updates must not replace a selected row.");
  assert.ok(fileButtons.every((button, index) => finalized.children[4].children[index] === button), "Preserve action nodes across polling.");
  assert.ok(pageButtons.every((button, index) => byId("storage-pagination").children[index] === button), "Preserve page-button nodes across polling.");
  state.storage.items[1].can_delete = false;
  renderStorage();
  assert.equal(fileButtons[4].disabled, true, "Actual file availability changes still disable actions immediately.");
  state.storage.items[1].can_recover = false;
  state.storage.items[1].recovery_unavailable_reason = "FLAC recovery tool is not installed.";
  renderStorage();
  assert.equal(fileButtons[3].disabled, true);
  assert.equal(fileButtons[3].title, "FLAC recovery tool is not installed.");
  for (const recoveryState of ["recovering", "recovered", "failed", "timeout", "cancelled", "unavailable"]) {
    state.storage.items[1].check = {state: recoveryState, action: "recover", message: "Original kept; fixture result.", output_name: recoveryState === "recovered" ? "<untrusted>-recovered-test.flac" : undefined};
    renderStorage();
    assert.equal(finalized._parts.status.getAttribute("data-state"), recoveryState);
    assert.match(finalized._parts.status.title, /Original kept/);
    if (recoveryState === "recovered") assert.match(finalized._parts.status.title, /<untrusted>-recovered-test.flac/);
    assert.ok(fileButtons.every((button, index) => finalized.children[4].children[index] === button), "Recovery updates preserve every action node.");
  }
  state.logEvents = [{level: "error", code: "capture_failed", message: "Input unavailable"}, {level: "info", code: "capture_started", message: "Input ready"}];
  byId("logs-level").value = "error"; byId("logs-filter").value = "input";
  assert.equal(filteredLogs().length, 1);
  assert.equal(filteredLogs()[0].code, "capture_failed");
  byId("logs-level").value = "all"; byId("logs-filter").value = "";
  localStorage.setItem("tinypirelay.visual.v1", JSON.stringify({spectrumPalette: "ember", meterPalette: "cyan", theme: "light", frequencyView: {minHz: 4000, maxHz: 12000}}));
  loadVisualPreferences();
  assert.equal(state.preferences.spectrumPalette, "ember");
  assert.equal(document.body.dataset.theme, "light");
  assert.equal(state.spectrumView.minHz, 4000);
  localStorage.setItem("tinypirelay.visual.v1", JSON.stringify({spectrumPalette: "constructor", meterPalette: "evil", theme: "evil", frequencyView: {minHz: -1, maxHz: 1e12}}));
  loadVisualPreferences();
  assert.equal(state.preferences.spectrumPalette, "ember", "Stored palette values must be allowlisted.");
  assert.equal(state.spectrumView.minHz, 4000, "Invalid stored bounds must not enter the renderer.");
`);
flushFrames();

const watchdog = setTimeout(() => {
  console.error("Dashboard confirmation did not resolve.");
  process.exitCode = 1;
}, 3000);
check(`(async () => {
  const dialog = byId("confirm-dialog");
  const cancelled = confirmAction("Cancel this fixture action?");
  assert.equal(dialog.open, true);
  assert.equal(dialog.returnValue, "cancel");
  dialog.close();
  assert.equal(await cancelled, false, "Escape/default close must not confirm.");
  const accepted = confirmAction("Confirm this fixture action?");
  dialog.close("confirm");
  assert.equal(await accepted, true);
  const expired = confirmAction("Session-expiration fixture");
  assert.equal(dialog.returnValue, "cancel", "Previous confirmation must not carry forward.");
  showLogin("Session expired");
  assert.equal(dialog.open, false);
  assert.equal(await expired, false);

  state.config = {
    capture: {device_id: "fixture", mode: {format: "S16LE", rate_hz: 48000, channels: 1}},
    stream: {enabled: true, destination_host: "receiver.local", latency_ms: 120},
    recording: {}, monitoring: {spectrum_updates_per_second: 5},
  };
  state.revision = "r1";
  renderStatus(liveStatus);
  const pendingControl = runControl(byId("stream-toggle"));
  assert.equal(dialog.open, true);
  assert.equal(byId("stream-toggle").dataset.action, "stream.stop");
  renderStatus(stopped);
  assert.equal(byId("stream-toggle").dataset.action, "stream.start");
  dialog.close("confirm");
  await pendingControl;
  assert.equal(state.busyButtons.size, 0);
  assert.match(byId("global-message").textContent, /stopped|changed/i);

  const file = {file_id: "recover-fixture", name: "interrupted.flac", status: "needs_check", can_recover: true, can_check: true, check: {state: "unchecked"}};
  state.storage = {page: 1, total: 1, total_pages: 1, items: [file]};
  const cancelRecovery = runStorageAction("recover", file);
  assert.match(byId("confirm-message").textContent, /Attempt recovery/);
  assert.match(byId("confirm-details").children.map(line => line.textContent).join(" "), /separately named.*original.*gaps.*CPU.*disk space/);
  assert.match(byId("confirm-details").children.at(-1).textContent, /five minutes.*1 GiB.*desktop recovery/);
  dialog.close("cancel"); await cancelRecovery;
  const staleRecovery = runStorageAction("recover", file);
  file.can_recover = false;
  dialog.close("confirm"); await staleRecovery;
  assert.match(byId("global-message").textContent, /state changed/);

  const originalFetch = fetch;
  const requests = [];
  let pageShift = false;
  let job = {state: "recovering", action: "recover", message: "Recovering a separate copy."};
  globalThis.fetch = async (url, options) => {
    const body = options?.body && JSON.parse(options.body);
    if (body) requests.push(body);
    const payload = body ? {accepted: job.state !== "unavailable", check: {...job}}
      : {page: 1, total: job.output_name ? 2 : 1, total_pages: 1, items: [{...file, check: {...job}, can_recover: job.state !== "recovering"}],
        ...(pageShift ? {check: {...job, file_id: file.file_id}, items: job.output_name ? [{file_id: "copy-fixture", name: job.output_name, check: {state: "passed"}}] : []} : {})};
    return {ok: true, status: 200, json: async () => payload};
  };
  try {
    state.csrfToken = "fixture-only";
    file.can_recover = true;
    const recovery = runStorageAction("recover", file);
    dialog.close("confirm"); await recovery;
    assert.equal(requests.length, 1);
    assert.equal(requests[0].action, "recover");
    assert.equal(requests[0].file_id, file.file_id);
    assert.equal(state.storage.items[0].check.state, "recovering");
    assert.equal(state.storage.items[0].can_recover, false);
    const originalLoadStorage = loadStorage;
    let polled = false;
    loadStorage = () => { polled = true; };
    state.route = "storage";
    state.storageLoadedAt = performance.now() - 3000;
    try { intervals[0](); } finally { loadStorage = originalLoadStorage; }
    assert.equal(polled, true, "Recovery jobs use the same fast polling as decode checks.");
    job = {state: "recovered", action: "recover", message: "Validated separate copy; original unchanged. Audio may have gaps.", output_name: "interrupted-recovered-fixture.flac"};
    await loadStorage();
    assert.equal(state.storage.items[0].name, "interrupted.flac");
    assert.match(byId("global-message").textContent, /Validated separate copy.*interrupted-recovered-fixture.flac/);
    assert.equal(byId("global-message").getAttribute("role"), "status");
    for (const failure of ["failed", "cancelled", "timeout", "unavailable"]) {
      state.storage.items[0].check.state = "recovering";
      job = {state: failure, action: "recover", message: failure + ": original unchanged"};
      await loadStorage();
      assert.equal(byId("global-message").textContent, failure + ": original unchanged");
      assert.equal(byId("global-message").getAttribute("role"), "alert");
    }
    const unavailable = runStorageAction("recover", state.storage.items[0]);
    dialog.close("confirm"); await unavailable;
    assert.equal(byId("global-message").textContent, "unavailable: original unchanged");
    assert.equal(requests.length, 2);
    pageShift = true;
    job = {state: "recovering", action: "recover", message: "Recovering another page's original."};
    await loadStorage();
    assert.equal(state.storage.items.length, 0);
    polled = false;
    loadStorage = () => { polled = true; };
    state.storageLoadedAt = performance.now() - 3000;
    try { intervals[0](); } finally { loadStorage = originalLoadStorage; }
    assert.equal(polled, true, "A recovery on another page still polls quickly through storage.check.");
    job = {state: "recovered", action: "recover", message: "Validated separate copy; original remains on the next page.", output_name: "interrupted-recovered-page-shift.flac"};
    await loadStorage();
    assert.equal(state.storage.items[0].file_id, "copy-fixture");
    assert.match(byId("global-message").textContent, /next page.*interrupted-recovered-page-shift.flac/);
    assert.equal(byId("global-message").getAttribute("role"), "status");
  } finally { globalThis.fetch = originalFetch; }
  state.csrfToken = "";
})()`).then(() => {
  assert.equal(networkCalls, 0, "A stale confirmation must not issue an API request.");
  console.log("Dashboard state checks passed (offline).");
}).catch((error) => {
  console.error(error);
  process.exitCode = 1;
}).finally(() => clearTimeout(watchdog));
