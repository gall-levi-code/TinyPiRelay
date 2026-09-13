"use strict";

// Capture the shipped UI against the disposable local preview; never a Pi.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {spawn, spawnSync} = require("node:child_process");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const output = path.join(root, "docs/screenshots");
const port = Number(process.env.TEST_PORT || 18087);
assert.ok(Number.isInteger(port) && port >= 1024 && port <= 65535, "Invalid preview port");
const base = `http://127.0.0.1:${port}`;
const browserName = process.env.TEST_BROWSER || "chromium";
let password = "";

(async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), "tinypirelay-docs-"));
  const fixture = path.join(directory, "sample.flac");
  let server, browser;
  try {
    const encoded = spawnSync("ffmpeg", ["-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:duration=8", "-ar", "48000", "-ac", "1", "-c:a", "flac", fixture], {windowsHide: true});
    assert.equal(encoded.status, 0, "Installed ffmpeg is required for the disposable FLAC fixture");
    const audio = fs.readFileSync(fixture);
    server = spawn(process.env.TEST_PYTHON || (process.platform === "win32" ? "py" : "python3"), ["-B", path.join(root, "spikes/dashboard_preview.py"), "--port", String(port)], {cwd: root, windowsHide: true});
    password = await new Promise((resolve, reject) => {
      let text = "";
      const timer = setTimeout(() => reject(Error("Preview startup timeout")), 15000);
      server.once("error", error => {clearTimeout(timer); reject(error);});
      server.once("exit", code => {clearTimeout(timer); reject(Error(`Preview exited ${code}`));});
      server.stdout.on("data", chunk => {
        text += chunk;
        const match = text.match(/Ephemeral password: (\S+)/);
        if (match) {clearTimeout(timer); resolve(match[1]);}
      });
      // Consume logs without exposing the ephemeral login or recording a trace.
      server.stderr.resume();
    });
    browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
    const context = await browser.newContext({viewport: {width: 1600, height: 900}, deviceScaleFactor: 1, serviceWorkers: "block"});
    const errors = [], blocked = [], controls = [], captures = [];
    const files = Array.from({length: 6}, (_, index) => ({
      file_id: `sample-${index}`, name: `sample-20260913T12${String(index * 10).padStart(2, "0")}00Z.flac`,
      size_bytes: audio.length, modified_unix: 1789300800 + index * 600,
      duration_seconds: 8, sample_rate_hz: 48000, channels: 1, bits_per_sample: 16,
      status: index === 1 ? "needs_check" : "finalized",
      check: index === 2 ? {state: "passed", message: "Synthetic fixture decode check passed"} : {state: "unchecked"},
      can_play: true, can_download: true, can_delete: true, can_check: true,
      can_recover: true, recovery_unavailable_reason: null,
    }));
    await context.route("**/*", async route => {
      const request = route.request(), url = new URL(request.url());
      const stopPreviewRecording = request.method() === "POST" && url.pathname === "/api/control" && request.postData() === '{"action":"recording.stop"}';
      if (url.origin !== base || !(["GET", "HEAD"].includes(request.method()) || request.method() === "POST" && url.pathname === "/api/login" || stopPreviewRecording)) {
        blocked.push(`${request.method()} ${url.origin}${url.pathname}`);
        return route.abort();
      }
      if (stopPreviewRecording) controls.push("recording.stop");
      if (url.pathname === "/api/storage") return route.fulfill({json: {items: files, check: null, page: 1, page_size: 6, total: 6, total_pages: 1}});
      if (url.pathname === "/api/storage/file") {
        assert.ok(files.some(file => file.file_id === url.searchParams.get("id") && file.can_play));
        const range = request.headers().range?.match(/^bytes=(\d+)-(\d*)$/);
        const start = range ? Number(range[1]) : 0;
        const end = range?.[2] ? Math.min(audio.length - 1, Number(range[2])) : audio.length - 1;
        return route.fulfill({status: range ? 206 : 200, contentType: "audio/flac", headers: {"Accept-Ranges": "bytes", ...(range ? {"Content-Range": `bytes ${start}-${end}/${audio.length}`} : {})}, body: audio.subarray(start, end + 1)});
      }
      return route.continue();
    });
    const page = await context.newPage();
    page.on("pageerror", error => errors.push(error.message));
    await page.goto(base);
    await page.getByLabel("Username", {exact: true}).fill("preview");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.locator("#login-form button[type=submit]").click();
    await page.waitForFunction(() => state.config && state.mediaAvailable);
    fs.mkdirSync(output, {recursive: true});
    async function capture(name) {
      await page.mouse.move(1599, 899);
      await page.waitForTimeout(200);
      assert.match(await page.locator("#data-source").innerText(), /SIMULATED LOCAL PREVIEW.*NO PI/);
      assert.deepEqual(await page.evaluate(() => ({width: document.documentElement.scrollWidth - innerWidth, height: document.documentElement.scrollHeight - innerHeight})), {width: 0, height: 0}, `${name} must fit the viewport`);
      assert.deepEqual(errors, []);
      assert.deepEqual(blocked, [], "Only loopback reads, the disposable login, and simulated recording.stop are allowed");
      const target = path.join(output, `${name}.png`);
      await page.screenshot({path: target});
      captures.push({name, bytes: fs.statSync(target).size});
    }
    // Let the actual SSE stream fill the full 30-second spectrogram history.
    await page.waitForFunction(() => state.spectrumColumns.length > 100 && performance.now() - state.spectrumColumns[0].time >= 30000, null, {timeout: 35000});
    await capture("dashboard");
    await page.locator(".section-nav a[href='#audio']").click();
    await capture("audio");
    // Real UI transition in the simulated reducer, so file recovery is available.
    await page.locator(".section-nav a[href='#recording']").click();
    await page.locator("#recording button[data-action='recording.stop']").click();
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await page.waitForFunction(() => state.status?.state?.recording?.state === "stopped");
    await page.locator(".section-nav a[href='#storage']").click();
    await page.waitForFunction(() => state.storage?.items?.length === 6);
    await page.locator("#recording-player").evaluate(player => {player.muted = true;});
    await page.locator("#file-list li").nth(2).getByRole("button", {name: /^Play /}).click();
    await page.waitForFunction(() => document.getElementById("recording-player").readyState >= 2);
    await page.locator("#recording-player").evaluate(player => {player.currentTime = 2; player.pause();});
    await capture("storage");
    await page.locator("#file-list li").nth(1).getByRole("button", {name: /^Recover /}).click();
    await page.locator("#confirm-dialog").waitFor({state: "visible"});
    await capture("recovery");
    await page.locator("#confirm-dialog button[value=cancel]").click();
    assert.deepEqual(blocked, []);
    assert.deepEqual(controls, ["recording.stop"]);
    console.log(JSON.stringify({kind: "Real UI, simulated local preview; no Pi", browser: browserName, version: browser.version(), width: 1600, height: 900, captures, errors, blocked, controls}, null, 2));
  } finally {
    try {if (browser) await browser.close();}
    finally {
      if (server && server.exitCode === null) server.kill();
      if (fs.existsSync(fixture)) fs.unlinkSync(fixture);
      fs.rmdirSync(directory);
    }
  }
})().catch(error => {console.error(password ? String(error).replaceAll(password, "[redacted]") : String(error)); process.exitCode = 1;});
