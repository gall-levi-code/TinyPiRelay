import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "src/tinypirelay/web_assets"


def _contrast_ratio(left, right):
    def luminance(value):
        channels = [int(value[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
            for channel in channels
        ]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    high, low = sorted((luminance(left), luminance(right)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class _DocumentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.sections = set()
        self.labels = set()
        self.canvases = {}
        self.sources = []
        self.live_regions = 0
        self.duplicate_ids = set()
        self.inline_handlers = []
        self.inline_scripts = 0
        self.routes = set()
        self.buttons = []
        self.dialogs = {}
        self.inputs = {}

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        identifier = attrs.get("id")
        if identifier:
            if identifier in self.ids:
                self.duplicate_ids.add(identifier)
            self.ids.add(identifier)
        if tag == "section" and identifier:
            self.sections.add(identifier)
        if tag == "label" and attrs.get("for"):
            self.labels.add(attrs["for"])
        if tag == "canvas" and identifier:
            self.canvases[identifier] = attrs
        if tag == "script" and attrs.get("src"):
            self.sources.append(attrs["src"])
        elif tag == "script":
            self.inline_scripts += 1
        if tag == "link" and attrs.get("href"):
            self.sources.append(attrs["href"])
        if "aria-live" in attrs:
            self.live_regions += 1
        self.inline_handlers.extend(key for key in attrs if key.startswith("on"))
        if "data-route" in attrs:
            self.routes.add(attrs["data-route"])
        if tag == "button":
            self.buttons.append(attrs)
        if tag == "dialog" and identifier:
            self.dialogs[identifier] = attrs
        if tag == "input" and identifier:
            self.inputs[identifier] = attrs


class WebAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ASSETS / "index.html").read_text(encoding="utf-8")
        cls.css = (ASSETS / "app.css").read_text(encoding="utf-8")
        cls.js = (ASSETS / "app.js").read_text(encoding="utf-8")
        cls.document = _DocumentParser()
        cls.document.feed(cls.html)

    def test_page_has_login_and_all_required_sections(self):
        self.assertTrue({"login-view", "app-view"} <= self.document.ids)
        self.assertTrue(
            {"dashboard", "audio", "stream", "recording", "storage", "system",
             "network", "diagnostics", "settings", "about"} <= self.document.ids
        )
        self.assertFalse(self.document.duplicate_ids)
        self.assertIn("config-form", self.document.ids)
        self.assertIn("logout-button", self.document.ids)

    def test_live_dashboard_hooks_exist_in_the_document(self):
        hooks = set(re.findall(r'(?:byId|setValue|setStateValue)\("([^"\n]+)"', self.js))
        self.assertFalse(hooks - self.document.ids, f"Missing dashboard hooks: {sorted(hooks - self.document.ids)}")

    def test_device_telemetry_panels_have_all_live_fields(self):
        hooks = {
            "device-hostname", "device-model", "device-os", "device-architecture", "software-version",
            "system-cpu", "system-temperature", "system-load", "system-memory", "system-uptime",
            "storage-percent", "storage-capacity", "storage-free", "storage-filesystem", "storage-recordings",
            "storage-read", "storage-write", "storage-state", "storage-ring-progress", "storage-usage", "storage-eta",
            "network-interface", "network-address", "network-link", "network-tx", "network-rx",
            "recording-file", "recording-file-label", "recording-size", "recording-duration",
            "cpu-history", "temperature-history", "disk-history", "network-history",
        }
        self.assertFalse(hooks - self.document.ids, f"Missing telemetry fields: {sorted(hooks - self.document.ids)}")

    def test_controls_are_labelled_and_live_media_has_text_alternatives(self):
        labelled_controls = {
            "username", "password", "capture-device", "capture-mode",
            "spectrum-rate", "spectrum-bands", "stream-enabled",
            "stream-representation", "destination-host", "destination-port",
            "srt-latency", "opus-bitrate", "stream-id", "srt-passphrase",
            "passphrase-clear", "recording-directory", "rotation-seconds",
            "required-mountpoint", "spectrum-min-hz", "spectrum-max-hz",
        }
        self.assertTrue(labelled_controls <= self.document.labels)
        self.assertTrue({"level-canvas", "spectrum-canvas"} <= set(self.document.canvases))
        for attributes in self.document.canvases.values():
            self.assertTrue(attributes.get("aria-label"))
        self.assertIn("level-values", self.document.ids)
        self.assertGreaterEqual(self.document.live_regions, 4)
        self.assertIn("Skip to main content", self.html)

    def test_frequency_view_controls_are_local_accessible_and_separate_from_capture_settings(self):
        for identifier in ("spectrum-min-hz", "spectrum-max-hz"):
            attributes = self.document.inputs[identifier]
            self.assertEqual("number", attributes.get("type"))
            self.assertEqual("", attributes.get("form"), "Display bounds must not submit or invalidate the capture form.")
            self.assertIn(identifier, self.document.labels)
        self.assertIn("spectrum-view-reset", self.document.ids)
        self.assertIn("spectrum-view-message", self.document.ids)
        self.assertIn("spectrum-gesture-help", self.document.ids)
        self.assertEqual("0", self.document.canvases["spectrum-canvas"].get("tabindex"))
        self.assertIn("drawImage(", self.js)
        self.assertIn("putImageData(", self.js)
        self.assertNotIn("SPECTRUM_TIME_WARP_MS", self.js)
        self.assertNotIn("SPECTRUM_MIDDLE_BUCKET_MS", self.js)

    def test_assets_are_local_and_need_no_build_or_framework(self):
        self.assertEqual(["app.css", "app.js"], self.document.sources)
        self.assertFalse(self.document.inline_handlers)
        self.assertEqual(0, self.document.inline_scripts)
        combined = f"{self.html}\n{self.css}\n{self.js}".lower()
        combined = combined.replace("http://www.w3.org/2000/svg", "")
        for forbidden in (
            "http://", "https://", "cdn", "react", "vue", "angular", "jquery",
            "webpack", "vite", "npm", "node_modules",
        ):
            self.assertNotIn(forbidden, combined)

    def test_api_contract_and_csrf_header_are_explicit(self):
        for endpoint in (
            "/api/login", "/api/session", "/api/logout", "/api/status",
            "/api/capabilities", "/api/config", "/api/control", "/api/events",
        ):
            self.assertIn(f'"{endpoint}"', self.js)
        self.assertIn('headers["X-CSRF-Token"] = state.csrfToken', self.js)
        self.assertIn('method: "PUT"', self.js)
        self.assertIn('body: {config, confirm_restart: confirmRestart, expected_revision: expectedRevision}', self.js)
        self.assertIn('const body = {action: button.dataset.action}', self.js)
        self.assertIn('body.value = integerValue(ui.bitrate)', self.js)
        self.assertIn('body: {username:', self.js)
        self.assertIn("credentials: \"same-origin\"", self.js)

    def test_choices_are_exact_mode_capabilities_not_a_cartesian_product(self):
        self.assertIn("mode?.stream_options", self.js)
        self.assertIn("activeMode()", self.js)
        self.assertIn("sameMode(mode, selectedMode)", self.js)
        self.assertIn("evidence-approved", self.html)
        for hardcoded_representation in (
            "pcm_s16le_48000_stereo_matroska",
            "flac_48000_stereo_matroska",
            "opus_128k_48000_stereo_matroska",
        ):
            self.assertNotIn(hardcoded_representation, self.js)

    def test_config_secret_and_restart_flows_are_fail_closed(self):
        for action in ('action: "keep"', 'action: "clear"', 'action: "replace"'):
            self.assertIn(action, self.js)
        self.assertNotIn("config.stream.passphrase.value", self.js)
        self.assertIn("formatChangeRequiresRestart", self.js)
        self.assertNotIn("window.confirm(", self.js)
        self.assertGreaterEqual(self.js.count("await confirmAction("), 3)
        self.assertIn('error.payload?.error?.code === "revision_conflict"', self.js)
        self.assertIn('error.payload?.error?.code === "confirmation_required"', self.js)
        self.assertIn('api("/api/config")', self.js)
        self.assertIn("restart-required configuration?", self.js)
        self.assertNotIn("Control request accepted.", self.js)
        for phrase in ("Stop capture?", "Stop the active SRT stream?", "Stop and finalize the active recording?"):
            self.assertIn(phrase, self.html)
        self.assertNotIn("service.restart_request", self.html)
        self.assertIn("10–79 printable ASCII characters", self.js)
        self.assertIn('pattern="[ -~]{10,79}"', self.html)

    def test_confirmations_use_accessible_native_dialog_and_preserve_revision(self):
        dialog = self.document.dialogs.get("confirm-dialog")
        self.assertIsNotNone(dialog)
        for attribute in ("aria-labelledby", "aria-describedby"):
            references = dialog.get(attribute, "").split()
            self.assertTrue(references)
            for reference in references:
                self.assertIn(reference, self.document.ids)
        self.assertIn('method="dialog"', self.html)
        self.assertIn('value="cancel"', self.html)
        self.assertIn('value="confirm"', self.html)
        self.assertIn("const expectedRevision = state.revision", self.js)
        self.assertIn("function confirmAction(", self.js)

    def test_diagnostic_events_use_the_backend_shape_and_stay_bounded(self):
        self.assertIn("payload?.diagnostics?.events", self.js)
        self.assertIn("reportedEvents.slice(-32)", self.js)
        self.assertIn("state.logPage * 4", self.js)
        self.assertIn("entry?.timestamp_unix", self.js)
        self.assertIn("entry?.code", self.js)

    def test_unavailable_media_clears_stale_state_and_disables_controls(self):
        self.assertIn("payload?.error", self.js)
        self.assertIn("state.config = null", self.js)
        self.assertIn("syncControlAvailability", self.js)
        self.assertIn("renderMeters(null)", self.js)
        self.assertIn("renderSpectrum(null)", self.js)
        self.assertIn("renderMediaRequestFailure(error)", self.js)

    def test_config_numbers_round_trip_without_ui_quantization(self):
        self.assertIn('id="rotation-seconds"', self.html)
        self.assertIn('min="60" max="2678400" step="1"', self.html)
        self.assertIn("config.recording.rotation_seconds", self.js)
        self.assertNotIn("rotation_seconds / 60", self.js)
        self.assertIn('id="spectrum-rate" type="number" min="0" max="60" step="any"', self.html)

    def test_sse_ingests_distinct_numeric_telemetry_and_coalesces_rendering(self):
        self.assertIn('new EventSource("/api/events")', self.js)
        self.assertIn('addEventListener("snapshot"', self.js)
        self.assertIn('addEventListener("telemetry"', self.js)
        self.assertIn("state.spectrumColumns.push", self.js)
        self.assertIn("requestAnimationFrame", self.js)
        self.assertIn("slice(0, 8)", self.js)
        self.assertIn("slice(0, 2048)", self.js)
        self.assertIn("finiteDb", self.js)
        self.assertIn("getContext(\"2d\")", self.js)
        self.assertIn("state.spectrumColumns", self.js)
        self.assertIn("checkTelemetryFreshness", self.js)

    def test_server_strings_are_never_inserted_as_markup(self):
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(unsafe, self.js)
        self.assertIn("textContent", self.js)
        self.assertIn("replaceChildren", self.js)

    def test_desktop_workspace_keeps_one_viewport_and_accessible_styles(self):
        self.assertIn(":focus-visible", self.css)
        self.assertIn("@media (prefers-reduced-motion: reduce)", self.css)
        self.assertIn("status-pill::before", self.css)
        self.assertIn("height: 100dvh", self.css)
        self.assertIn("min-width: 1160px", self.css)
        compact = re.search(r"@media \(max-width: 1279px\)\s*\{(.*?)\n\}", self.css, re.S)
        self.assertIsNotNone(compact)
        self.assertRegex(compact.group(1), r"\.dashboard\s*\{\s*grid-template-columns:\s*\d+px minmax\([^;]+\) \d+px \d+px;")
        self.assertIn("min-height: 720px", self.css)
        self.assertIn("white-space: nowrap", self.css)
        self.assertNotRegex(self.css, r"overflow(?:-y)?:\s*(?:auto|scroll)")
        self.assertNotIn("overflow-wrap: anywhere", self.css)
        self.assertNotIn("text-overflow: ellipsis", self.css)
        surface = re.search(r"--surface:\s*(#[0-9a-fA-F]{6})", self.css)
        self.assertIsNotNone(surface)
        for name in ("text", "muted", "accent"):
            color = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", self.css)
            self.assertIsNotNone(color)
            self.assertGreaterEqual(_contrast_ratio(color.group(1), surface.group(1)), 4.5)

    def test_dashboard_toggle_buttons_keep_fixed_geometry(self):
        branches = {button.get("data-toggle") for button in self.document.buttons if "data-toggle" in button}
        self.assertEqual({"stream", "recording"}, branches)
        self.assertRegex(self.css, r"\.card-actions button[^{}]*\{[^}]*height:\s*32px")
        self.assertRegex(self.css, r"button \.icon[^{}]*\{[^}]*width:\s*14px;[^}]*height:\s*14px")
        self.assertIn('id="icon-play"', self.html)
        self.assertIn('id="icon-stop"', self.html)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; no build tool is required")
    def test_javascript_parses_when_node_is_available(self):
        result = subprocess.run(
            [shutil.which("node"), "--check", str(ASSETS / "app.js")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; no build tool is required")
    def test_dashboard_state_handling_offline(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests/web_dashboard_checks.js")],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
