"use strict";

// Real browser/auth against a disposable loopback fixture; never uses the Pi.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const {spawn} = require("node:child_process");
const playwright = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const root = path.resolve(__dirname, "..");
const browserName = process.env.TEST_BROWSER || "firefox";

(async () => {
  const server = spawn(process.env.TEST_PYTHON || "py", ["-B", path.join(root, "spikes/onboarding_preview.py"), "--port", "0"], {cwd: root, windowsHide: true});
  let browser;
  try {
    const base = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error("Disposable preview did not start")), 15000);
      let output = "";
      server.once("error", reject);
      server.once("exit", code => reject(Error(`Preview exited ${code}`)));
      server.stdout.on("data", chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:[0-9]+\//);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
      server.stderr.on("data", chunk => process.stderr.write(chunk));
    });
    browser = await playwright[browserName].launch({headless: true, executablePath: process.env.BROWSER_EXECUTABLE || undefined});
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    const screenshots = fs.mkdtempSync(path.join(os.tmpdir(), `tinypirelay-onboarding-${browserName}-`));
    await page.goto(base);
    await page.getByRole("heading", {name: "Create your account"}).waitFor();
    async function checkLayout(mode) {
      for (const viewport of [{width: 1160, height: 720}, {width: 1920, height: 1080}]) {
        await page.setViewportSize(viewport);
        const layout = await page.evaluate(() => {
          const card = document.querySelector(".login-card").getBoundingClientRect();
          return {
            overflowX: document.documentElement.scrollWidth - innerWidth,
            overflowY: document.documentElement.scrollHeight - innerHeight,
            cardWithinView: card.left >= 0 && card.top >= 0 && card.right <= innerWidth && card.bottom <= innerHeight,
            overflowing: [...document.querySelectorAll(".login-card p, .login-card input, .login-card button")]
              .filter(node => node.getBoundingClientRect().height > 0)
              .filter(node => node.scrollWidth > node.clientWidth + 1 || node.getBoundingClientRect().right > card.right)
              .map(node => node.id || node.tagName),
          };
        });
        assert.deepEqual(layout, {overflowX: 0, overflowY: 0, cardWithinView: true, overflowing: []}, `${mode} ${viewport.width}x${viewport.height}`);
        await page.screenshot({path: path.join(screenshots, `${mode}-${viewport.width}x${viewport.height}.png`)});
      }
    }
    await checkLayout("create-account");
    assert.equal(await page.locator("#password").getAttribute("type"), "password");
    assert.equal(await page.locator("#setup-confirm-password").getAttribute("type"), "password");
    const password = crypto.randomBytes(20).toString("base64url");
    await page.getByLabel("Username", {exact: true}).fill("browserqa");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.getByLabel("Confirm password", {exact: true}).fill(password);
    await page.getByRole("button", {name: "Create account", exact: true}).click();
    await page.getByRole("heading", {name: "Sign in", exact: true}).waitFor();
    assert.equal(await page.locator("#password").inputValue(), "");
    assert.equal(await page.locator("#setup-confirm-password").inputValue(), "");
    await checkLayout("sign-in");
    await page.reload();
    await page.getByRole("heading", {name: "Sign in", exact: true}).waitFor();
    await page.getByLabel("Username", {exact: true}).fill("browserqa");
    await page.getByLabel("Password", {exact: true}).fill(password);
    await page.getByRole("button", {name: "Sign in", exact: true}).click();
    await page.locator("#app-view:not([hidden])").waitFor();
    assert.equal(await page.locator("#password").inputValue(), "");
    await page.locator('.section-nav a[href="#audio"]').click();
    await page.locator("#audio:not([hidden])").waitFor();
    const device = page.locator("#capture-device"), mode = page.locator("#capture-mode");
    await device.selectOption({index: 1});
    await mode.selectOption({index: 1});
    await page.locator("#spectrum-rate").fill("7");
    const draft = {device: await device.inputValue(), mode: await mode.inputValue()};
    const refresh = page.getByRole("button", {name: "Refresh audio inputs", exact: true});
    const [refreshed] = await Promise.all([
      page.waitForResponse(response => new URL(response.url()).pathname === "/api/capabilities"),
      refresh.click(),
    ]);
    assert.equal(refreshed.status(), 200);
    await page.getByText("Audio inputs refreshed. No settings were saved.", {exact: true}).waitFor();
    assert.deepEqual({device: await device.inputValue(), mode: await mode.inputValue()}, draft);
    assert.equal(await page.locator("#spectrum-rate").inputValue(), "7");
    await device.selectOption("");
    await page.locator("#spectrum-rate").fill("5");
    const [saved] = await Promise.all([
      page.waitForResponse(response => response.request().method() === "PUT" && new URL(response.url()).pathname === "/api/config"),
      page.getByRole("button", {name: "Save configuration", exact: true}).click(),
    ]);
    assert.equal(saved.status(), 200);
    assert.deepEqual(saved.request().postDataJSON().config.capture, {device_id: null, mode: null});
    assert.equal(saved.request().postDataJSON().config.stream.enabled, false);
    await page.getByText("Configuration saved.", {exact: true}).first().waitFor();
    const idleStatus = await page.evaluate(async () => (await fetch("/api/status")).json());
    assert.equal(idleStatus.audio_setup_required, true);
    assert.equal(idleStatus.state.capture.state, "stopped");
    for (const viewport of [{width: 1160, height: 720}, {width: 1920, height: 1080}]) {
      await page.setViewportSize(viewport);
      await page.screenshot({path: path.join(screenshots, `idle-audio-${viewport.width}x${viewport.height}.png`)});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth || document.documentElement.scrollHeight > innerHeight), false);
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({browser: browserName, passed: true, screenshots, checked: ["first-run creation", "normal login", "password fields cleared", "audio refresh preserves draft", "unchanged null-audio config save", "no-mic idle GUI", "1160x720 and 1920x1080 no overflow"]}));
  } finally {
    await browser?.close();
    server.kill();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
