# Raspberry Pi 4 linear dashboard check — 2026-09-08

## Scope and identity

Release `0.5.9-dev` replaces the experimental 120-second time warp with a
linear 30-second Canvas 2D bitmap conveyor. Frequency controls are local to
the browser tab: numeric low/high bounds, Hz-axis wheel zoom, drag pan, and
double-click/Full range reset. They do not change capture, recording, streaming,
FFT size, or transport. No package or dependency was added.

- Pi: Raspberry Pi 4 Model B Rev 1.4, Debian 13 Trixie, aarch64.
- Input: Dayton Audio iMM-6C, mono S16LE, 48 kHz; monitoring remains 60 Hz / 512 bands.
- Runtime archive: 177060 bytes, SHA-256
  `6fcff04f0e5119664c1fc3596bb0e03a40751dce3146d1fac65d69191998eafe`.
- Upgrade from `0.5.8-dev` passed preflight and completed through the normal
  installer. Configuration and credential SHA-256 comparisons passed before
  and after. Configuration revision remains
  `0a411c9a23c76d450dbb7a81b3572b8c604f500875a6988fbba8f116fb02163e`.
- Both services were active with zero restarts after upgrade: media PID 227099,
  web PID 227104. Capture resumed; recording and SRT remained stopped.
  Diagnostics reported healthy, no runtime error; firmware throttling was `0x0`.

The expired workstation tunnel was restored with the same loopback-only
`127.0.0.1:18082` to Pi `127.0.0.1:8080` mapping and trusted SSH identity.

## Checks

`PYTHONPATH=src python -B -m unittest discover -s tests` ran 413 tests without
failures, with 19 platform-specific skips on Windows. All 19 focused web asset
tests passed. `node tests/web_dashboard_checks.js` and the existing preview
self-test passed. Offline checks exercise bitmap call geometry, fractional DPR,
real elapsed stalls, jitter versus long gaps, bounded history, input validation,
axis gestures, one repaint per animation frame, and no display-related API writes.

The optional `spikes/spectrum_browser_check.cjs` uses an existing Playwright
runtime and synthetic data, never a Pi. Firefox 134 and Chromium 133 both passed
at DPR 1.25, with dashboard/Audio layouts checked at 1920×1080 and 1280×720.
No document or new-control overflow, page errors, or non-login writes occurred.
The fixture seeds 1801 samples over 30 seconds, then measures 180 animation
callbacks; it is not a wall-clock 30-second acquisition or a displayed-FPS test.

| Synthetic full-history test | Firefox 134 | Chromium 133 |
|---|---:|---:|
| Steady spectrum draw median / p95 / max | 2 / 5 / 8 ms | 0.2 / 0.6 / 1 ms |
| Initial full-history rebuild | 113 ms | 51.9 ms |

View changes intentionally rebuild bounded raw history. Gesture bursts are
coalesced through the existing animation-frame gate, but these full rebuilds
can still produce a noticeable pause; smooth 60 Hz dragging is not established.

## Real microphone in Firefox

An authenticated headless Firefox 134 browser on the Windows PC consumed the
installed Pi's real SSE feed through the SSH tunnel for 32.005 seconds.
Viewport was 1920×1080, DPR 1.25. The in-app dashboard was also opened during
this check, so this was not an isolated single-client Pi CPU benchmark.

| Measurement | Result |
|---|---:|
| Spectrum producer sequence delta | 1921 |
| Spectrum render function calls | 1905 |
| Spectrum draw median / p95 / maximum | 2 / 4 / 6 ms |
| Retained distinct samples at end | 1694 |
| Retained arrival-time span | 30006 ms |
| Browser page errors | 0 |
| Non-login API writes | 0 |

The boundary sample accounts for the slight excess over 30 seconds; the
display window remains exactly 30 seconds. Full-range and 4–12 kHz screenshots
were visually inspected: history was continuous without the previous repeated
blank-strip pattern. Numeric bounds changed the plotted range and did not
write backend configuration. The document fit the tested 1920×1080 viewport.

Timing instrumentation wrapped the spectrum render function with the browser's
monotonic clock. These timings exclude other UI work, compositing and scan-out.
Producer sequence deltas are not distinct deliveries: not every produced sample
is retained or painted. This is not a guarantee of 60 displayed frames per second.
Firefox's production CSP was kept intact; the test used locator waits rather
than an eval-based polling helper rejected by that policy.

## Limits

History uses browser arrival time, not sample-accurate recording time. The
250 ms delivery hold at 60 Hz smooths short delivery jitter but does not recover
missing samples. Longer interruptions remain blank. Zoom reallocates existing
FFT bins; it does not increase frequency resolution. Settings are tab-local and
reset on reload. Layout checks do not cover viewports below the existing desktop
minimum, and the test browser version need not match the user's current Firefox.

This finite check does not establish kiosk rendering on the Pi, a hardware CPU
improvement, bandwidth reduction, endurance, active recording/SRT continuity,
or Step 6 acceptance. The earlier `.7/.8` evidence remains historical in
[the warped-dashboard report](RPI4_WARPED_DASHBOARD_2026-09-08.md).
