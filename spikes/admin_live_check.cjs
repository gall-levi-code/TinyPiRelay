"use strict";
// Read-only live GUI acceptance. Only login and GET/HEAD requests are permitted.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");

async function waitFor(page, predicate) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await page.evaluate(predicate)) return;
    await page.waitForTimeout(100);
  }
  throw new Error("Live view readiness timed out");
}

async function verifyLiveInteractions(page) {
  await waitFor(page, () => state.mediaAvailable && !state.mediaDataLoading && !state.storageLoading && !state.maintenanceLoading);
  const dropdown = await page.evaluate(() => {
    const controls = [...document.querySelectorAll('.page-section:not([hidden]) select, .page-section:not([hidden]) input, .page-section:not([hidden]) button, .page-section:not([hidden]) a.button')]
      .filter(element => element.getClientRects().length && element.clientWidth > 0);
    const focus = controls.find(element => element.tagName === "SELECT" && !element.disabled)
      || controls.find(element => element.tagName === "BUTTON" && !element.disabled);
    focus?.focus();
    const probe = window.liveInteractionProbe = {controls, focus, value: focus?.value, snapshots: 0, telemetry: 0, mutations: {}, started: performance.now(), source: state.eventSource};
    probe.snapshot = () => { probe.snapshots += 1; };
    probe.sample = () => { probe.telemetry += 1; };
    probe.source?.addEventListener("snapshot", probe.snapshot);
    probe.source?.addEventListener("telemetry", probe.sample);
    probe.observer = new MutationObserver(records => {
      for (const record of records) {
        const owner = record.target.id || record.target.closest?.("button, select, input, a.button")?.getAttribute("aria-label") || record.target.nodeName;
        const key = `${owner}:${record.attributeName || record.type}`;
        probe.mutations[key] = (probe.mutations[key] || 0) + 1;
      }
    });
    for (const element of controls) probe.observer.observe(element, {subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "style"]});
    return focus?.tagName === "SELECT";
  });
  if (dropdown) await page.keyboard.press("Alt+ArrowDown");
  // Real SSE only: do not inject snapshots or change the device's state.
  await page.waitForTimeout(1500);
  if (dropdown) await page.keyboard.press("Escape");
  const result = await page.evaluate(() => {
    const probe = window.liveInteractionProbe;
    probe.observer.disconnect();
    probe.source?.removeEventListener("snapshot", probe.snapshot);
    probe.source?.removeEventListener("telemetry", probe.sample);
    return {elapsed_ms: Math.round(performance.now() - probe.started), controls: probe.controls.length,
      snapshots: probe.snapshots, telemetry: probe.telemetry, mutations: probe.mutations,
      detached: probe.controls.filter(element => !element.isConnected).length,
      focusPreserved: !probe.focus || document.activeElement === probe.focus,
      selectionPreserved: probe.focus?.tagName !== "SELECT" || probe.focus.value === probe.value,
      mediaAvailable: state.mediaAvailable};
  });
  assert.ok(result.snapshots > 0 && result.mediaAvailable === true, `Actual live snapshots required: ${JSON.stringify(result)}`);
  assert.ok(result.detached === 0 && result.focusPreserved && result.selectionPreserved && Object.keys(result.mutations).length === 0,
    `Live controls changed; inspect availability transitions before classifying a regression: ${JSON.stringify(result)}`);
  return result;
}

(async () => {
  let password = "";
  for await (const chunk of process.stdin) password += chunk;
  const browserName = process.env.TEST_BROWSER || "firefox";
  const browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
  try {
    const page = await browser.newPage({viewport: {width: 1280, height: 720}});
    const errors = [], writes = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/api/**", async route => {
      const request = route.request();
      if (!["GET", "HEAD"].includes(request.method()) && !request.url().endsWith("/api/login")) {
        writes.push(request.url()); await route.abort(); return;
      }
      await route.continue();
    });
    await page.goto(process.env.TEST_URL || "http://127.0.0.1:18082/#storage");
    await page.getByLabel("Username", {exact: true}).fill("tinypirelay");
    await page.getByLabel("Password", {exact: true}).fill(password.trim());
    password = "";
    await page.locator("#login-form button[type=submit]").click();
    await page.locator("#app-view").waitFor({state: "visible"});
    await waitFor(page, () => state.storage?.items?.length > 0 && state.mediaAvailable === true);
    const storage = await page.evaluate(() => state.storage);
    const sample = storage.items.find(file => file.can_play);
    assert.ok(sample, "a closed playable FLAC must be listed");
    const range = await page.evaluate(async file => {
      const response = await fetch(`/api/storage/file?id=${encodeURIComponent(file.file_id)}`, {headers: {Range: "bytes=0-41"}});
      const data = new Uint8Array(await response.arrayBuffer());
      return {status: response.status, length: data.length, signature: String.fromCharCode(...data.slice(0, 4)), range: response.headers.get("content-range")};
    }, sample);
    assert.equal(range.status, 206); assert.equal(range.length, 42); assert.equal(range.signature, "fLaC");
    await page.evaluate(() => { byId("recording-player").muted = true; });
    await page.getByRole("button", {name: `Play ${sample.name}`, exact: true}).click();
    await waitFor(page, () => { const audio = byId("recording-player"); return Number.isFinite(audio.duration) && audio.duration > 0 && audio.readyState >= 2 && audio.currentTime > 0.1; });
    const playback = await page.evaluate(async () => {
      const audio = byId("recording-player");
      audio.pause();
      const target = Math.min(5, audio.duration / 2);
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(() => reject(new Error("Native seek timed out")), 10000);
        audio.addEventListener("seeked", () => { clearTimeout(timeout); resolve(); }, {once: true});
        audio.currentTime = target;
      });
      return {duration: audio.duration, target, position: audio.currentTime, error: audio.error?.code || null};
    });
    assert.equal(playback.error, null);
    assert.ok(Math.abs(playback.position - playback.target) <= 0.25, "Native player reaches the requested seek position");
    const routes = [];
    const shots = process.env.SCREENSHOT_DIR;
    if (shots) fs.mkdirSync(shots, {recursive: true});
    for (const route of ["storage", "system", "network", "audio", "settings", "diagnostics", "about", "dashboard", "stream", "recording"]) {
      await page.evaluate(route => { location.hash = route; }, route);
      await page.waitForTimeout(350);
      const interactions = await verifyLiveInteractions(page);
      const dimensions = await page.evaluate(() => ({width: innerWidth, height: innerHeight,
        scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight,
        pageHeight: document.querySelector('.page-section:not([hidden])').getBoundingClientRect().bottom,
        overflow: [...document.querySelectorAll('#app-view *')].filter(element => element.getClientRects().length).map(element => {
          const rect = element.getBoundingClientRect();
          return {id: element.id || element.className, tag: element.tagName, left: rect.left, right: rect.right,
            top: rect.top, bottom: rect.bottom, width: element.clientWidth, scrollWidth: element.scrollWidth,
            height: element.clientHeight, scrollHeight: element.scrollHeight};
        }).filter(element => element.right > innerWidth + 1 || element.bottom > innerHeight + 1
          || element.scrollWidth > element.width + 2 || element.scrollHeight > element.height + 2)}));
      if (dimensions.scrollWidth > dimensions.width || dimensions.scrollHeight > dimensions.height || dimensions.pageHeight > dimensions.height) {
        if (shots) await page.screenshot({path: path.join(shots, `live-failed-${route}.png`)});
        console.error(JSON.stringify({route, dimensions}, null, 2));
      }
      assert.ok(dimensions.scrollWidth <= dimensions.width && dimensions.scrollHeight <= dimensions.height, `${route}: document overflow`);
      assert.ok(dimensions.pageHeight <= dimensions.height, `${route}: page exceeds viewport`);
      routes.push({route, interactions, ...dimensions});
      if (shots && ["storage", "system", "network", "settings", "dashboard"].includes(route)) await page.screenshot({path: path.join(shots, `live-${route}.png`)});
    }
    const status = await page.evaluate(async () => ({maintenance: await (await fetch("/api/maintenance")).json(),
      version: state.deviceSample.device.software_version, capture: state.status.state.capture.state,
      recording: state.status.state.recording.state, stream: state.status.state.stream.state}));
    assert.equal(status.version, fs.readFileSync(path.join(__dirname, "../VERSION"), "utf8").trim());
    assert.equal(status.capture, "running");
    assert.equal(status.maintenance.available, true); assert.equal(status.maintenance.busy, false);
    assert.deepEqual(errors, []); assert.deepEqual(writes, []);
    const summary = {kind: "read-only installed Pi GUI", browser: browser.version(), files: storage.total,
      sample: sample.name, range, playback, status, routes, errors, writes};
    if (shots) fs.writeFileSync(path.join(shots, "live-summary.json"), JSON.stringify(summary, null, 2));
    console.log(JSON.stringify(summary, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
