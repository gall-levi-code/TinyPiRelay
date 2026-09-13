# Dashboard integration — updated 2026-09-09

The `0.5.13-dev` expansion connects Storage, System/Network, browser appearance
preferences, maintenance/account controls, and Logs/About. Its contracts and
limits are described in [WEB_CONTROL.md](WEB_CONTROL.md). The renderer evidence
below remains historical; the expanded pages do not change the capture graph.

`0.5.14-dev` isolates interactive controls from background refreshes: unchanged
controls are not rewritten, text fitting is interaction-aware, Storage retains
its buttons, and temporary outages preserve drafts and their original revision.
See [the interaction contract](WEB_CONTROL.md#interaction-stability-0514-dev).

The approved desktop dashboard is now the production web client's layout,
using the existing Python HTTP service and media-control APIs. The separate
design mockup remains unchanged. Historical release `0.5.3-dev` was installed on the
physical Zero W. Its real backend telemetry, controls, meter, spectrum and all
dashboard panels were visually verified. A matched closed/open profile found
that the visible dashboard saturates the Zero W, so this configuration is not
accepted; see the
[physical dashboard report](evidence/RPI_ZERO_W_DASHBOARD_2026-09-03.md).
The `0.5.9-dev` client replaced the experimental time warp with a
linear 30-second bitmap conveyor and display-only frequency controls. It is
subsequently retained by later Pi 4 releases; a finite 32-second live Firefox 134 check passed and is recorded separately in
the [linear dashboard report](evidence/RPI4_LINEAR_DASHBOARD_2026-09-08.md).

## Connected in this milestone

| Dashboard feature | Authoritative source |
|---|---|
| Capture, stream, SRT connection and recording states | `/api/status`, `/api/events` |
| Start/stop controls | CSRF-protected `/api/control`; reported media decisions |
| Audio peak, RMS, clipping and no-signal | Actual per-channel meter telemetry |
| Spectrogram | Spectrum magnitude samples; browser-rendered linear 30-second bitmap conveyor, with at most 1801 raw arrival-time samples |
| Spectrogram frequency view | Browser-only low/high bounds, frequency-axis wheel zoom, drag pan, and reset |
| SRT TX rate, RTT and retransmissions | `send-rate-mbps`, `rtt-ms`, `packets-retransmitted` |
| Recording filename, size and elapsed time | Media owner's current or last finalized fragment; guarded file metadata and a monotonic fragment clock |
| Device identity and uptime | Hostname, device tree, OS release, running architecture, release version and `/proc/uptime` |
| CPU, RAM, temperature and load | `/proc/stat`, `/proc/meminfo`, thermal sysfs and `/proc/loadavg` |
| Recording filesystem capacity and cutoff | The existing recording-destination safety check and its exact filesystem |
| Disk activity | Sector-counter deltas from that filesystem's block device/partition |
| Recording folder size and estimated time to cutoff | Bounded FLAC metadata scan; current fragment's measured byte size / elapsed time |
| Network interface, address, link and traffic | Default-route interface (or an up-interface fallback), local address query and sysfs counters |
| Capture/stream settings | Redacted `/api/config` and evidence-approved `/api/capabilities` |
| Warnings and recent events | Bounded diagnostic snapshot; four events per page |

Stream-running and SRT-connected remain separate. Old SRT statistics are not
shown as live after disconnection. Duplicate telemetry sequences do not extend
freshness; stale samples, unavailable media, and capture restarts clear the
visuals. Hiding the browser tab closes SSE and releases its spectrum demand;
it does not stop capture, streaming, or recording.

The frequency display maps GStreamer's linearly spaced spectrum bands onto a
high-frequency-biased logarithmic axis. In the default 20-24 kHz view at 48 kHz,
4-24 kHz receives about 37% of the plot height. Editable low/high bounds and
frequency-axis wheel zoom/drag pan change screen allocation, not source frequency
resolution. Double-click the axis or use Full range to reset. Bounds remain
within the known active capture Nyquist limit and at least 10 Hz apart. These
preferences are now remembered in this browser: they do not save device configuration,
change capture/recording/streaming, or add a network request.

Time is uniformly linear across 30 seconds. At most 1801 raw samples are retained,
including at most one boundary predecessor when needed to paint the left edge;
there are no age-dependent peak rollups. Canvas 2D shifts a cached bitmap by
integer device pixels and paints only newly exposed strips. Zoom/resize replays
the bounded history; telemetry and frequency gestures share an animation frame.
Measured colors are held through short delivery jitter (250 ms at 60 Hz), while
longer interruptions remain blank and browser stalls advance by real elapsed
time. Timing is browser arrival, not sample-accurate recording time.
See [GStreamer's spectrum documentation](https://gstreamer.freedesktop.org/documentation/spectrum/index.html).

## Preserved safety boundaries

- No framework, frontend build step, new dependency, media graph or codec has
  been added. The `0.5.13-dev` file/account/maintenance endpoints and narrow
  root-helper boundary are documented in [WEB_CONTROL.md](WEB_CONTROL.md).
- The interface retains authenticated sessions, CSRF headers, redacted secrets,
  explicit saves, expected-revision conflicts and restart confirmations.
- Start controls are disabled while a media transition or restart-required
  configuration is pending. Safe stops remain available when applicable.
- Saved settings are not labelled as active when their revision differs.
- Browser API values are written as text, never inserted as markup.
- Desktop/large-tablet layout targets at least 1160 × 720, with narrower desktop
  columns below 1280 pixels. Routes replace one another in the workspace; logs
  are paginated rather than scrolling.

## Not yet connected

Recovery/re-encoding, Wi-Fi configuration, UPS telemetry and full system-journal
access remain later work. The file library, native FLAC playback/download,
confirmed deletion/checking, reboot/shutdown, password changes and browser
appearance preferences are connected in `0.5.13-dev`.
The reference design retains those concepts. The production shell does not
simulate these as if they were implemented.

## Device sampling and limits

The media service owns one read-only background sampler. It publishes a cached
`device_telemetry` object through the existing authenticated status and SSE
paths, at a two-second cadence shared by all browser clients. File/proc/sysfs
reads do not hold the media-runtime lock or run on its GLib/audio loop. The web
service gains no filesystem, device or service-manager permissions. Failure of
the optional sampler clears its cache without stopping media or controls.

Each sample carries a sequence, collection timestamp and active configuration
revision. The browser rejects mismatched revisions, expires unchanged samples
after seven seconds, and clears readings on disconnect. It keeps at most 32
points / 60 seconds per device history. Graphs are drawn in the browser, with
gaps for missing samples. First or reset rate counters are unknown, not zero.

Storage follows the configured recording filesystem, including the existing
required-mount checks. A missing required mount never falls back to displaying
root-filesystem capacity. Disk rates describe one partition/device, not both a
partition and its parent. Folder totals count regular same-filesystem FLAC files
directly in the recording folder, without following symlinks; they refresh every
30 seconds. The scan stops after 4096 entries and reports an unavailable total
if incomplete. This is not a recursive, instantaneous file-library index.

Size is a guarded metadata read of the exact current or last finalized fragment;
no file content is opened. Elapsed time begins when the media owner confirms a
fragment opening, resets on rotation, and freezes only after confirmed closure.
It is wall-clock elapsed time, not sample-exact decoded audio duration. Partial
or forced-close failures are not relabelled as finalized recordings.

The approximate time to 90% uses the current recording's average observed byte
size / elapsed time after at least five seconds. Available bytes also cap the
estimate. It is unavailable when stopped, unmeasurable or mismatched; changing
compression or other filesystem writes can change the estimate. The existing
90% recording stop guard remains authoritative and unchanged.

Network rates cover all traffic on the selected interface, not only SRT. A
local address query sends no probe packets. No receiver DNS lookup or connection
test is performed. Wi-Fi link speed/duplex may be unavailable, and optional
sensor failures remain em dashes. Thermal-zone temperature is not a battery or
supply-voltage measurement; those hardware capabilities remain unconfigured.

## Local verification

```powershell
$env:PYTHONPATH = 'src'
py -3 -B -m unittest discover -s tests -p 'test_*.py'
py -3 -B spikes/dashboard_preview.py --self-test
py -3 -B spikes/dashboard_preview.py --port 18082
```

The optional preview binds only to loopback, generates a disposable in-memory
login, serves the real HTTP/auth/SSE application and uses the production media
state reducer with synthetic samples. Its configuration is deliberately
read-only. It creates no media files, streams, Pi connections or system actions.
Its banner explicitly identifies it as a simulated local preview.

The native Node checks in `tests/web_dashboard_checks.js` run without browser
packages or network access. The existing backend tests also cover logical
command rejection over HTTP 200 and the exact REST/SSE redaction contract.

Current `0.5.9-dev` verification on the development PC (2026-09-08): 413 tests
ran without failures, with 19 platform-dependent skips. The 19 focused web asset
tests include offline bitmap-call/interaction regressions for linear time,
bounded history, real elapsed stalls, visible outage gaps, fractional device
pixel ratios, restart clearing, editable bounds, axis gestures, and coalesced
repainting. Synthetic real-browser checks passed in Firefox 134 and Chromium
133. Separately, the installed Pi 4's actual 48 kHz mono microphone feed passed
a finite 32-second Firefox 134 check at 1920 × 1080 and DPR 1.25. Full-range and
4-12 kHz views were visually continuous, with no captured errors or non-login
API writes. The measured render-function median/p95/maximum was 2/4/6 ms; this
is function execution time, not complete browser frame latency. Neither row
establishes kiosk suitability, endurance, or a guaranteed 60 FPS display.

Historical verification on the development PC (2026-09-03): 409 tests ran with no failures
and 19 platform-dependent skips. The collector's 12 fixture tests cover Linux
counter parsing, missing/changed filesystems, bounded scans, relative and linked
paths, values above 4 GiB, and guarded estimates. Worker tests verify that reads
remain off-thread, requests share a copied cache, and failures reveal no private
paths. Changed backend modules also parse with Python 3.11 syntax rules.

Those historical browser checks covered all ten routes at 1160 × 720 and the dashboard at
1160 × 992, 1280 × 720 and 1920 × 1080. Stopping the simulated recording froze
its size/elapsed time and retained streaming; starting another reset the file
identity and values. No visible panel or document overflow remained at those
sizes, and the final preview reported no browser console warnings or errors.
The 18 frontend checks were rerun after the compact-height CSS adjustment.
These checks used the local simulated-media harness, not the Pi.

The historical Zero W deployment shakedown does not replace hardware acceptance. The closed row
found a short 100% CPU excursion; the open row sustained 100% system CPU and its
paired receiver found two PTS discontinuities. Spectrum tuning, a matched
rerun, endurance, rotation and fault rows remain pending.
