"use strict";

// Optional real-browser check with synthetic audio only; does not contact a Pi.
// PLAYWRIGHT_MODULE and BROWSER_EXECUTABLE may point to an existing test runtime.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {spawn} = require("node:child_process");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const browserName = process.env.TEST_BROWSER || "firefox";
const port = Number(process.env.TEST_PORT || 18083);
const base = `http://127.0.0.1:${port}`;

(async () => {
  const server = spawn(process.env.TEST_PYTHON || "py", ["-B", path.join(root, "spikes/dashboard_preview.py"), "--port", String(port)], {cwd: root, windowsHide: true});
  let browser;
  try {
    const password = await new Promise((resolve, reject) => {
      let text = "";
      const timeout = setTimeout(() => reject(new Error("Preview startup timeout")), 15000);
      server.once("error", reject);
      server.once("exit", code => reject(new Error(`Preview exited: ${code}`)));
      server.stdout.on("data", chunk => {
        text += String(chunk);
        const match = text.match(/Ephemeral password: (\S+)/);
        if (match) { clearTimeout(timeout); resolve(match[1]); }
      });
      server.stderr.on("data", chunk => process.stderr.write(chunk));
    });
    browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
    const context = await browser.newContext({viewport: {width: 1920, height: 1080}, deviceScaleFactor: 1.25});
    const errors = [];
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    const writes = [];
    page.on("request", request => {
      if (request.method() !== "GET" && !request.url().endsWith("/api/login")) writes.push(request.url());
    });
    await page.goto(base);
    await page.getByLabel("Username", {exact: true}).fill("preview");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.locator("#login-form button[type=submit]").click();
    await page.locator("#app-view").waitFor({state: "visible"});
    await page.waitForFunction(() => state.config !== null && state.mediaAvailable === true);
    const result = await page.evaluate(async () => {
      stopEvents();
      clearTelemetry();
      state.config.capture.mode.rate_hz = 48000;
      state.config.monitoring.spectrum_updates_per_second = 60;
      state.config.monitoring.spectrum_bands = 512;
      state.mediaAvailable = true;
      state.route = "dashboard";
      state.status.state.capture.state = "running";
      state.statusReceivedAt = performance.now();
      const values = Array.from({length: 512}, (_, band) => -90 + 55 * Math.exp(-(((band - 140) / 8) ** 2)));
      const start = performance.now();
      state.spectrumColumns = Array.from({length: 1801}, (_, i) => ({time: start - 30000 + i * 1000 / 60, values: [...values]}));
      state.lastSpectrum = {sequence: 1801, magnitudes_db: values};
      state.spectrumSequence = 1801;
      state.spectrumReceivedAt = start;
      const rebuildStart = performance.now();
      renderSpectrum(state.lastSpectrum);
      const rebuildMs = performance.now() - rebuildStart;
      const times = [];
      const intervals = [];
      let previous = performance.now();
      for (let i = 0; i < 180; i++) {
        await new Promise(requestAnimationFrame);
        const now = performance.now();
        intervals.push(now - previous);
        previous = now;
        state.statusReceivedAt = now;
        state.spectrumReceivedAt = now;
        state.spectrumColumns.push({time: now, values: [...values]});
        state.spectrumColumns = compactSpectrumColumns(state.spectrumColumns, now);
        const before = performance.now();
        renderSpectrum(state.lastSpectrum);
        times.push(performance.now() - before);
      }
      const stats = array => {
        const sorted = [...array].sort((a, b) => a - b);
        return {median: sorted[Math.floor(sorted.length / 2)], p95: sorted[Math.floor(sorted.length * .95)], max: sorted.at(-1)};
      };
      return {full_history_rebuild_ms: rebuildMs, render_ms: stats(times), animation_interval_ms: stats(intervals), retained_columns: state.spectrumColumns.length, summary: byId("spectrum-summary").textContent};
    });
    assert.match(result.summary, /30 s.*linear/i);
    assert.ok(result.retained_columns <= 1801);
    await page.locator("#analysis-link").click();
    await page.locator("#spectrum-min-hz").fill("4000");
    await page.locator("#spectrum-max-hz").fill("12000");
    await page.locator("#spectrum-max-hz").press("Tab");
    assert.deepEqual(await page.evaluate(() => state.spectrumView), {minHz: 4000, maxHz: 12000});
    assert.equal(await page.locator("#spectrum-min-hz").evaluate(el => el.form), null);
    await page.locator(".spectrum-view-panel a").click();
    const box = await page.locator("#spectrum-canvas").boundingBox();
    await page.mouse.move(box.x + 20, box.y + box.height / 2);
    await page.mouse.wheel(0, -200);
    await page.waitForFunction(() => state.spectrumView.maxHz - state.spectrumView.minHz < 8000);
    const zoomed = await page.evaluate(() => ({...state.spectrumView}));
    await page.mouse.down();
    await page.mouse.move(box.x + 20, box.y + box.height / 2 + 50, {steps: 5});
    await page.mouse.up();
    assert.notDeepEqual(await page.evaluate(() => state.spectrumView), zoomed);
    await page.mouse.dblclick(box.x + 20, box.y + box.height / 2);
    assert.equal(await page.evaluate(() => state.spectrumView), null);
    const layouts = [];
    for (const viewport of [{width: 1920, height: 1080}, {width: 1280, height: 720}]) {
      await page.setViewportSize(viewport);
      for (const route of ["dashboard", "audio"]) {
        await page.locator(`.section-nav a[href='#${route}']`).click();
        await page.waitForTimeout(150);
        const layout = await page.evaluate(() => ({
          width: window.innerWidth, height: window.innerHeight,
          scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight,
          overflowingControls: [...document.querySelectorAll(".spectrum-view-panel input, .spectrum-view-panel button, .spectrum-view-panel a")].filter(el => el.offsetWidth && el.scrollWidth > el.clientWidth + 1).map(el => el.id || el.textContent),
        }));
        assert.ok(layout.scrollWidth <= layout.width && layout.scrollHeight <= layout.height, JSON.stringify(layout));
        assert.deepEqual(layout.overflowingControls, []);
        layouts.push({route, ...layout});
      }
    }
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
      for (const route of ["audio", "dashboard"]) {
        await page.locator(`.section-nav a[href='#${route}']`).click();
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, `${browserName}-${route}.png`)});
      }
    }
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    console.log(JSON.stringify({kind: "synthetic real-browser check; not Pi or microphone evidence", browser: browserName, version: browser.version(), devicePixelRatio: 1.25, ...result, layouts, errors, writes}, null, 2));
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
