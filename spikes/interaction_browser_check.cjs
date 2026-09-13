"use strict";

// Optional real-browser regression: local simulated preview only; no Pi access.
const assert = require("node:assert/strict");
const path = require("node:path");
const {spawn} = require("node:child_process");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const port = Number(process.env.TEST_PORT || 18085);
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
  const server = spawn(process.env.TEST_PYTHON || "py", ["-B", path.join(root, "spikes/dashboard_preview.py"), "--port", String(port)], {cwd: root, windowsHide: true});
  let browser;
  try {
    const password = await new Promise((resolve, reject) => {
      let output = "";
      const timer = setTimeout(() => reject(Error("Preview timeout")), 15000);
      server.once("error", reject);
      server.once("exit", code => reject(Error(`Preview exited ${code}`)));
      server.stdout.on("data", chunk => { output += chunk; const match = output.match(/Ephemeral password: (\S+)/); if (match) { clearTimeout(timer); resolve(match[1]); } });
      server.stderr.on("data", chunk => process.stderr.write(chunk));
    });
    browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
    const page = await browser.newPage({viewport: {width: 1280, height: 720}});
    const errors = [], results = [], writes = [];
    page.on("pageerror", error => errors.push(error.message));
    const files = Array.from({length: 12}, (_, index) => ({file_id: `fixture-${index}`, name: `fixture-${index}.flac`, size_bytes: 5000,
      modified_unix: 1788960840, duration_seconds: 3, status: "finalized", check: {state: "unchecked"},
      can_play: true, can_download: true, can_check: true, can_delete: true}));
    await page.route("**/api/storage?*", route => {
      const number = Number(new URL(route.request().url()).searchParams.get("page") || 1);
      return route.fulfill({json: {page: number, page_size: 6, total: 12, total_pages: 2, items: files.slice((number - 1) * 6, number * 6)}});
    });
    await page.route("**/api/maintenance", route => route.fulfill({json: {available: true, busy: false, phase: "idle", boot_id: "mock"}}));
    await page.route("**/api/**", route => {
      const request = route.request();
      if (!["GET", "HEAD"].includes(request.method()) && !request.url().endsWith("/api/login")) {
        writes.push(new URL(request.url()).pathname);
        return route.abort();
      }
      return route.fallback();
    });
    await page.goto(`http://127.0.0.1:${port}`);
    await page.getByLabel("Username", {exact: true}).fill("preview");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.locator("#login-form button[type=submit]").click();
    await waitFor(page, () => state.mediaAvailable && state.config && state.status?.device_telemetry);
    await page.evaluate(() => stopEvents());

    for (const route of ["audio", "stream", "recording", "storage", "system", "network", "diagnostics", "settings", "dashboard", "about"]) {
      await page.locator(`.section-nav a[href='#${route}']`).click();
      if (route === "storage") await waitFor(page, () => state.storage?.items?.length === 6 && !state.storageLoading);
      if (["audio", "stream"].includes(route)) await page.evaluate(() => {
        const select = state.route === "audio" ? ui.mode : ui.representation;
        const longest = [...select.options].filter(option => !option.disabled).sort((left, right) => right.textContent.length - left.textContent.length)[0];
        if (longest) { select.value = longest.value; select.dispatchEvent(new Event("change", {bubbles: true})); }
        scheduleTextFit();
      });
      await page.waitForTimeout(150);
      await page.evaluate(() => {
        const visible = element => element.getClientRects().length && element.clientWidth > 0;
        const controls = [...document.querySelectorAll(".page-section:not([hidden]) select, .page-section:not([hidden]) input, .page-section:not([hidden]) button")].filter(visible);
        const focus = (state.route === "audio" ? ui.mode : state.route === "storage" ? byId("file-list").querySelector("li:nth-child(2) button") : null)
          || controls.find(element => !element.disabled && element.tagName === "SELECT") || controls.find(element => !element.disabled);
        focus?.focus();
        const probe = window.interactionProbe = {controls, focus, value: focus?.value, changes: []};
        probe.observer = new MutationObserver(records => {
          for (const record of records) probe.changes.push({id: record.target.id || record.target.closest?.("button")?.getAttribute("aria-label") || record.target.nodeName, kind: record.type, attribute: record.attributeName});
        });
        for (const element of controls) probe.observer.observe(element, {subtree: true, childList: true, attributes: true, attributeFilter: ["disabled", "style"]});
      });
      // Exercise a real native select popup, not a replacement DOM menu.
      const hasSelect = await page.evaluate(() => window.interactionProbe.focus?.tagName === "SELECT");
      if (hasSelect) await page.keyboard.press("Alt+ArrowDown");
      for (let update = 0; update < 5; update += 1) {
        await page.evaluate(async () => {
          const payload = structuredClone(state.status);
          payload.device_telemetry.sequence += 1;
          payload.device_telemetry.system.cpu_percent += .1;
          renderStatus(payload);
          if (state.route === "storage") await loadStorage();
          if (["system", "settings"].includes(state.route)) await loadMaintenance();
          scheduleTextFit();
        });
        await page.waitForTimeout(75);
      }
      const result = await page.evaluate(() => {
        const probe = window.interactionProbe;
        probe.observer.disconnect();
        return {route: state.route, changes: probe.changes, detached: probe.controls.filter(element => !element.isConnected).length,
          focusPreserved: !probe.focus || document.activeElement === probe.focus};
      });
      if (hasSelect) {
        await page.keyboard.press("Escape");
        result.dropdownSelectionPreserved = await page.evaluate(() => window.interactionProbe.focus.value === window.interactionProbe.value);
      }
      results.push(result);
    }

    await page.locator(".section-nav a[href='#stream']").click();
    await page.locator("#destination-host").fill("unsaved-draft.invalid");
    const draft = await page.locator("#destination-host").evaluate(input => {
      input.setSelectionRange(2, 8);
      const original = structuredClone(state.status);
      const revision = state.revision, capabilities = state.capabilities, mode = ui.mode.value;
      for (let index = 0; index < 10; index += 1) renderStatus(original);
      const result = {value: input.value, start: input.selectionStart, end: input.selectionEnd, focused: document.activeElement === input};
      // Genuine availability changes must still disable and then re-enable controls.
      renderStatus({available: false});
      result.disabledOnDisconnect = input.disabled;
      renderStatus(original);
      result.enabledOnReconnect = !input.disabled;
      result.draftOnReconnect = input.value === result.value && ui.mode.value === mode && state.revision === revision && state.capabilities === capabilities;
      return result;
    });
    assert.deepEqual(draft, {value: "unsaved-draft.invalid", start: 2, end: 8, focused: true, disabledOnDisconnect: true, enabledOnReconnect: true, draftOnReconnect: true});

    await page.locator(".section-nav a[href='#storage']").click();
    await waitFor(page, () => state.storage?.items?.length === 6 && !state.storageLoading);
    await page.evaluate(() => {
      const rows = [...byId("file-list").children];
      window.storageProbe = {rows, button: rows[1].querySelector("button")};
      window.storageProbe.button.focus();
    });
    files[0].size_bytes += 1000;
    files[0].check = {state: "checking", message: "Synthetic check underway"};
    files[0].can_delete = false;
    await page.evaluate(() => loadStorage());
    const storage = await page.evaluate(() => ({
      sameRows: window.storageProbe.rows.every((row, index) => row === byId("file-list").children[index]),
      sameFocusedButton: document.activeElement === window.storageProbe.button,
      changedStatus: byId("file-list").children[0].querySelector(".file-state").textContent,
      deletionBlocked: [...byId("file-list").children[0].querySelectorAll("button")].at(-1).disabled,
    }));
    assert.deepEqual(storage, {sameRows: true, sameFocusedButton: true, changedStatus: "Checking…", deletionBlocked: true});

    await page.locator(".section-nav a[href='#dashboard']").click();
    const toggle = page.locator("button[data-toggle='stream']");
    const beforePress = await page.evaluate(() => structuredClone(state.status));
    const box = await toggle.boundingBox();
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.evaluate(() => {
      const payload = structuredClone(state.status);
      payload.state.stream.state = "stopped";
      payload.state.connection.state = "disconnected";
      renderStatus(payload);
    });
    await page.mouse.up();
    await waitFor(page, () => byId("global-message").textContent.includes("while pressing"));
    await page.evaluate(payload => renderStatus(payload), beforePress);
    assert.deepEqual(errors, []);
    assert.deepEqual(writes, []);
    console.log(JSON.stringify({browser: browserName, version: browser.version(), results: results.map(result => ({...result, changes: result.changes.reduce((summary, change) => {
      const key = `${change.id}:${change.attribute || change.kind}`; summary[key] = (summary[key] || 0) + 1; return summary;
    }, {})})), draft, storage, pointerIntentProtected: true, errors, writes}, null, 2));
    assert.ok(results.every(result => result.dropdownSelectionPreserved !== false), "A native dropdown selection must survive live updates before it is committed.");
    assert.deepEqual(results.filter(result => result.changes.length || result.detached || !result.focusPreserved), [], "Background updates must leave unchanged interactive controls intact.");
  } finally { if (browser) await browser.close(); server.kill(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
