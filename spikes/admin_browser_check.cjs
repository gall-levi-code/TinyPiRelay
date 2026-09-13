"use strict";

// Optional real-browser QA. All new APIs are intercepted; no Pi or real files are touched.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const {spawn, spawnSync} = require("node:child_process");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const port = Number(process.env.TEST_PORT || 18084);
const base = `http://127.0.0.1:${port}`;
const browserName = process.env.TEST_BROWSER || "firefox";
async function waitFor(page, predicate) {
  const deadline = Date.now() + 15000;
  while (Date.now() < deadline) {
    if (await page.evaluate(predicate)) return;
    await page.waitForTimeout(50);
  }
  throw Error(`Timed out: ${String(predicate)}`);
}

(async () => {
  const fixtureDirectory = fs.mkdtempSync(path.join(os.tmpdir(), "tinypirelay-gui-qa-"));
  const fixturePath = path.join(fixtureDirectory, "synthetic.flac");
  const encoded = spawnSync("ffmpeg", ["-v", "error", "-f", "lavfi", "-i", "sine=frequency=1000:duration=3", "-ar", "48000", "-ac", "1", "-c:a", "flac", fixturePath], {windowsHide: true, maxBuffer: 1024 * 1024});
  assert.equal(encoded.status, 0, "Installed ffmpeg is required for the disposable native FLAC playback fixture.");
  const audio = {stdout: fs.readFileSync(fixturePath)};
  const server = spawn(process.env.TEST_PYTHON || "py", ["-B", path.join(root, "spikes/dashboard_preview.py"), "--port", String(port)], {cwd: root, windowsHide: true});
  let browser;
  try {
    const password = await new Promise((resolve, reject) => {
      let text = "";
      const timer = setTimeout(() => reject(Error("Preview timeout")), 15000);
      server.once("error", reject);
      server.once("exit", code => reject(Error(`Preview exited ${code}`)));
      server.stdout.on("data", chunk => { text += chunk; const match = text.match(/Ephemeral password: (\S+)/); if (match) {clearTimeout(timer); resolve(match[1]);} });
      server.stderr.on("data", chunk => process.stderr.write(chunk));
    });
    browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
    const context = await browser.newContext({viewport: {width: 1920, height: 1080}, deviceScaleFactor: 1.25});
    const page = await context.newPage();
    const errors = [], writes = [], fileRequests = [];
    page.on("pageerror", error => errors.push(error.message));
    let files = Array.from({length: 14}, (_, index) => ({file_id: `fixture-${index}`, name: `tinypirelay-20260909T1234${String(index).padStart(2, "0")}.123456Z.flac`, size_bytes: audio.stdout.length,
      modified_unix: 1788960840 + index, duration_seconds: 3, sample_rate_hz: 48000, channels: 1, bits_per_sample: 16,
      status: index === 0 ? "active" : index === 1 ? "needs_check" : "finalized", check: {state: "unchecked"}, can_play: index > 0, can_download: index > 0, can_delete: index > 0, can_check: index > 0,
      can_recover: index > 0 && index !== 4, recovery_unavailable_reason: index === 0 ? "Active recordings cannot be recovered." : index === 4 ? "FLAC recovery tool is unavailable." : null}));
    let recoveryFile = null, recoveryPolls = 0, storageCheck = null;
    let maintenance = {available: true, busy: false, action: null, phase: "idle", operation_id: null, boot_id: "boot-before", error: null};
    await page.route(/\/api\/storage(?:\/|\?|$)/, async route => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/storage/file") {
        fileRequests.push({id: url.searchParams.get("id"), range: route.request().headers().range || null});
        const match = route.request().headers().range?.match(/^bytes=(\d+)-(\d*)$/);
        const start = match ? Number(match[1]) : 0, end = match?.[2] ? Math.min(audio.stdout.length - 1, Number(match[2])) : audio.stdout.length - 1;
        return route.fulfill({status: match ? 206 : 200, contentType: "audio/flac", headers: {"Accept-Ranges": "bytes", ...(match ? {"Content-Range": `bytes ${start}-${end}/${audio.stdout.length}`} : {})}, body: audio.stdout.subarray(start, end + 1)});
      }
      if (url.pathname === "/api/storage/action") {
        const body = route.request().postDataJSON(); writes.push({path: url.pathname, action: body.action});
        const file = files.find(file => file.file_id === body.file_id);
        if (body.action === "delete") files = files.filter(item => item !== file);
        else if (body.action === "recover") {
          assert.equal(file.can_recover, true);
          recoveryFile = file; recoveryPolls = 0;
          file.check = {state: "recovering", action: "recover", message: "Recovering a separate copy; original unchanged."};
          storageCheck = {...file.check, file_id: file.file_id};
          file.can_recover = file.can_check = file.can_delete = false;
        } else file.check = {state: "passed", message: "Synthetic decode check passed"};
        return route.fulfill({json: {accepted: true, check: file.check}});
      }
      if (recoveryFile && ++recoveryPolls >= 3) {
        const outputName = recoveryFile.name.replace(/\.flac$/, "-recovered-fixture.flac");
        recoveryFile.check = {state: "recovered", action: "recover", message: "Validated separate copy; original unchanged. Audio may contain gaps.", output_name: outputName};
        storageCheck = {...recoveryFile.check, file_id: recoveryFile.file_id};
        recoveryFile.can_recover = recoveryFile.can_check = recoveryFile.can_delete = true;
        files.unshift({...recoveryFile, file_id: "recovered-fixture", name: outputName, status: "finalized", check: {state: "passed"}});
        recoveryFile = null;
      }
      const number = Number(url.searchParams.get("page") || 1);
      await route.fulfill({json: {items: files.slice((number - 1) * 6, number * 6), check: storageCheck, page: number, page_size: 6, total: files.length, total_pages: Math.ceil(files.length / 6)}});
    });
    await page.route("**/api/maintenance", async route => {
      if (route.request().method() === "POST") {
        const body = route.request().postDataJSON(); writes.push({path: "/api/maintenance", action: body.action});
        if (body.current_password !== "mock-current") return route.fulfill({status: 401, json: {error: {code: "invalid_login", message: "Current password is incorrect"}}});
        maintenance = {...maintenance, action: body.action, phase: "complete", operation_id: "mock-operation", accepted: true};
        return route.fulfill({json: maintenance});
      }
      return route.fulfill({json: maintenance});
    });
    await page.route("**/api/account/password", async route => {
      writes.push({path: "/api/account/password"});
      const body = route.request().postDataJSON();
      return route.fulfill(body.current_password === "mock-current" ? {json: {changed: true, authenticated: false}}
        : {status: 401, json: {error: {code: "invalid_login", message: "Current password is incorrect"}}});
    });
    await page.goto(base);
    await page.getByLabel("Username", {exact: true}).fill("preview");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.locator("#login-form button[type=submit]").click();
    await waitFor(page, () => state.mediaAvailable && state.config);
    const layouts = [];
    for (const viewport of [{width: 1160, height: 720}, {width: 1280, height: 720}, {width: 1920, height: 1080}]) {
      await page.setViewportSize(viewport);
      for (const name of ["dashboard", "stream", "audio", "recording", "storage", "system", "network", "diagnostics", "settings", "about"]) {
        await page.locator(`.section-nav a[href='#${name}']`).click();
        await page.waitForTimeout(130);
        const layout = await page.evaluate(() => {
          const visible = element => element.getClientRects().length && element.offsetWidth > 0;
          const active = document.querySelector(`.page-section[data-page='${state.route}']`);
          const overflow = [...active.querySelectorAll(".panel, button, .button, [data-fit], .help, .settings-note")].filter(visible).filter(element => element.scrollWidth > element.clientWidth + 2 || element.scrollHeight > element.clientHeight + 2).map(element => ({id: element.id || element.className, text: element.textContent.slice(0, 150), width: element.clientWidth, scrollWidth: element.scrollWidth, height: element.clientHeight, scrollHeight: element.scrollHeight}));
          return {route: state.route, width: innerWidth, height: innerHeight, scrollWidth: document.documentElement.scrollWidth, scrollHeight: document.documentElement.scrollHeight, overflow};
        });
        layouts.push(layout);
      }
      const original = await page.evaluate(() => {
        const original = structuredClone(state.config);
        const device = state.capabilities.capture_devices.find(device => device.modes.some(mode => mode.format === "S24LE" && mode.channels === 1));
        const mode = device.modes.find(mode => mode.format === "S24LE" && mode.channels === 1);
        const config = structuredClone(original);
        config.capture = {device_id: device.id, mode: {format: mode.format, rate_hz: mode.rate_hz, channels: mode.channels}};
        config.stream.representation_id = mode.stream_options[0].id;
        hydrateConfig({config, revision: state.revision, restart_required: false});
        return original;
      });
      await page.locator(".section-nav a[href='#stream']").click();
      await page.waitForTimeout(130);
      const longProfile = await page.evaluate(() => ({width: innerWidth, scrollWidth: document.documentElement.scrollWidth,
        text: ui.representationHelp.textContent, title: ui.representationHelp.title,
        overflow: [...ui.representationHelp.children].filter(element => element.scrollWidth > element.clientWidth + 2).length}));
      assert.ok(longProfile.scrollWidth <= longProfile.width && longProfile.overflow === 0, `Native 24-bit Stream explanation fits at ${viewport.width}px`);
      assert.match(longProfile.text, /does not preserve 24-bit samples/);
      assert.match(longProfile.title, /RPI4_S24_FLAC_BIRDNET/);
      if (process.env.SCREENSHOT_DIR && viewport.width === 1160) {
        fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, `${browserName}-admin-stream-24bit.png`)});
      }
      await page.evaluate(config => hydrateConfig({config, revision: state.revision, restart_required: false}), original);
    }
    assert.deepEqual(layouts.filter(layout => layout.scrollWidth > layout.width || layout.scrollHeight > layout.height || layout.overflow.length), []);
    await page.locator(".section-nav a[href='#settings']").click();
    assert.equal(await page.evaluate(() => {
      const original = structuredClone(state.config);
      const disabled = structuredClone(original);
      Object.assign(disabled.stream, {enabled: false, destination_host: null, destination_port: null, representation_id: null});
      hydrateConfig({config: disabled, revision: state.revision, restart_required: false});
      const candidate = editableConfig();
      const valid = configForm.checkValidity() && candidate.stream.destination_host === null && candidate.stream.destination_port === null
        && candidate.stream.representation_id === null && candidate.stream.enabled === false && !formatChangeRequiresRestart(candidate);
      hydrateConfig({config: original, revision: state.revision, restart_required: false});
      return valid;
    }), true, "Audio/recording saves must not require an unconfigured disabled stream.");
    await page.locator("#display-theme").selectOption("light");
    await waitFor(page, () => document.body.dataset.theme === "light");
    await page.locator(".section-nav a[href='#audio']").click();
    await page.locator("#spectrum-palette").selectOption("ember");
    await page.locator("#meter-palette").selectOption("cyan");
    await waitFor(page, () => JSON.parse(localStorage.getItem("tinypirelay.visual.v1")).spectrumPalette === "ember");
    await page.reload(); await waitFor(page, () => state.config);
    assert.equal(await page.evaluate(() => state.preferences.spectrumPalette), "ember");
    assert.equal(await page.evaluate(() => document.body.dataset.theme), "light");
    await page.locator(".section-nav a[href='#storage']").click();
    await waitFor(page, () => state.storage?.items?.length === 6);
    assert.equal(await page.locator("#file-list li").count(), 6);
    assert.equal(await page.locator("#file-list li").first().getByRole("button", {name: /^Delete /}).isDisabled(), true);
    await page.locator("#file-list li").nth(2).getByRole("button", {name: /^Play /}).click();
    try { await waitFor(page, () => byId("recording-player").readyState >= 2); }
    catch (error) { throw Error(`${error.message}: ${JSON.stringify({fileRequests, player: await page.locator("#recording-player").evaluate(player => ({src: player.src, error: player.error?.message, code: player.error?.code})), message: await page.locator("#playback-message").textContent()})}`); }
    await page.locator("#recording-player").evaluate(player => {player.currentTime = 1.5; player.pause();});
    assert.ok(await page.locator("#recording-player").evaluate(player => Math.abs(player.currentTime - 1.5) < .2));
    const download = page.locator("#file-list li").nth(2).getByRole("link", {name: /^Download /});
    assert.match(await download.getAttribute("href"), /download=1/);
    await page.locator("#file-list li").nth(1).getByRole("button", {name: "Check", exact: true}).click();
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await waitFor(page, () => state.storage.items[1].check.state === "passed");
    const recoveryButton = page.locator("#file-list li").nth(5).getByRole("button", {name: /^Recover /});
    assert.equal(await page.locator("#file-list li").nth(4).getByRole("button", {name: /^Recover /}).isDisabled(), true);
    assert.match(await page.locator("#file-list li").nth(4).getByRole("button", {name: /^Recover /}).getAttribute("title"), /tool is unavailable/);
    for (const theme of ["light", "dark"]) {
      await page.evaluate(theme => {document.body.dataset.theme = theme;}, theme);
      await page.setViewportSize({width: 1160, height: 720});
      await recoveryButton.click();
      assert.match(await page.locator("#confirm-details").textContent(), /separately named.*original.*gaps.*CPU.*disk space/);
      await page.waitForTimeout(130);
      assert.deepEqual(await page.locator("#confirm-dialog").evaluate(dialog => [...dialog.querySelectorAll("[data-fit], button")]
        .filter(element => element.scrollWidth > element.clientWidth + 2 || element.scrollHeight > element.clientHeight + 2)
        .map(element => element.textContent)), [], "Recovery confirmation fits without wrapping or truncation.");
      if (process.env.SCREENSHOT_DIR) await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, `${browserName}-recovery-confirm-${theme}.png`)});
      await page.locator("#confirm-dialog button[value=cancel]").click();
    }
    assert.equal(writes.filter(write => write.action === "recover").length, 0, "Cancel never starts recovery.");
    await recoveryButton.click();
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await waitFor(page, () => state.storage.check?.state === "recovering");
    assert.equal(await recoveryButton.isDisabled(), true);
    await page.evaluate(() => {
      const row = byId("file-list").children[0];
      window.recoveryControls = [...row.querySelectorAll("button, a")];
    });
    await waitFor(page, () => state.storage.check?.state === "recovered");
    assert.equal(await page.evaluate(() => window.recoveryControls.every((button, index) => byId("file-list").children[1].querySelectorAll("button, a")[index] === button)), true);
    assert.match(await page.locator("#global-message").textContent(), /Validated separate copy.*-recovered-fixture.flac/);
    assert.equal(files.length, 15);
    assert.equal(files[6].name.includes("recovered"), false, "Recovery preserves the original file, now on the next page.");
    assert.equal(await page.evaluate(() => state.storage.items.some(file => file.file_id === "fixture-5")), false);
    assert.equal(await page.locator("#file-list li").nth(1).locator("button").last().getAttribute("aria-label"), `Delete ${files[1].name}`, "Trash stays at the far right.");
    assert.deepEqual(await page.evaluate(() => ({width: document.documentElement.scrollWidth - innerWidth, height: document.documentElement.scrollHeight - innerHeight})), {width: 0, height: 0});
    await page.locator("#file-list li").nth(2).getByRole("button", {name: /^Delete /}).click();
    await page.locator("#confirm-dialog button[value=cancel]").click();
    assert.equal(files.length, 15);
    await page.locator("#file-list li").nth(2).getByRole("button", {name: /^Delete /}).click();
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await waitFor(page, () => state.storage.total === 14);
    await page.locator("#storage-pagination").getByRole("button", {name: "Recording page 2", exact: true}).click();
    await waitFor(page, () => state.storage.page === 2);
    await page.locator(".section-nav a[href='#settings']").click();
    await page.locator("#display-theme").selectOption("dark");
    await page.locator("button[data-maintenance='system.reboot']").last().click();
    assert.equal(await page.locator("#confirm-current-password").isVisible(), true);
    await page.locator("#confirm-dialog button[value=cancel]").click();
    await page.locator("button[data-maintenance='media.restart']").click();
    await page.locator("#confirm-current-password").fill("wrong");
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await waitFor(page, () => byId("global-message").textContent.includes("incorrect"));
    assert.equal(await page.locator("#app-view").isVisible(), true);
    await page.locator("button[data-maintenance='media.restart']").click();
    await page.locator("#confirm-current-password").fill("mock-current");
    await page.locator("#confirm-dialog button[value=confirm]").click();
    await page.locator("#maintenance-overlay").waitFor({state: "visible"});
    await page.locator("#maintenance-retry").click();
    await page.locator("#maintenance-overlay").waitFor({state: "hidden"});
    for (const id of ["account-new-password", "account-confirm-password"]) await page.locator(`#${id}`).fill("mock-new-password");
    await page.locator("#account-current-password").fill("wrong");
    await page.locator("#password-form button[type=submit]").click();
    await waitFor(page, () => byId("account-message").textContent.includes("incorrect"));
    assert.equal(await page.locator("#app-view").isVisible(), true);
    if (process.env.SCREENSHOT_DIR) {
      fs.mkdirSync(process.env.SCREENSHOT_DIR, {recursive: true});
      for (const route of ["storage", "system", "network", "settings", "audio", "about"]) {
        await page.locator(`.section-nav a[href='#${route}']`).click(); await page.waitForTimeout(100);
        await page.screenshot({path: path.join(process.env.SCREENSHOT_DIR, `${browserName}-admin-${route}.png`)});
      }
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({kind: "Mocked admin APIs; synthetic local FLAC; no Pi mutations", browser: browserName, version: browser.version(), layouts, fileRequests, writes, errors}, null, 2));
  } finally { if (browser) await browser.close(); server.kill(); fs.unlinkSync(fixturePath); fs.rmdirSync(fixtureDirectory); }
})().catch(error => {console.error(error); process.exitCode = 1;});
