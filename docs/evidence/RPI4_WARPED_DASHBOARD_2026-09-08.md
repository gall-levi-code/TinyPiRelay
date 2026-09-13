# Raspberry Pi 4 warped dashboard validation — 2026-09-08

Publication copy: LAN endpoints use documentation-only addresses. The original
report is retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

Finite live-monitoring validation of installed `0.5.7-dev`; Step 5 remains in progress.

## Recovery and release identity

The returning Pi 4 was reachable at `192.0.2.20` with its previously trusted
SSH host key. It had the Dayton Audio iMM-6C connected instead of the previously
configured Scarlett Solo. Capture startup failed and systemd retried until the
device selection was corrected.

The prior configuration was preserved at
`/var/lib/tinypirelay/backups/20260908-before-imm6c.json`. Only capture selection
was changed, using the existing capability validation and atomic config writer:
`hw:CARD=iMM6C,DEV=0`, mono S16LE, 48 kHz. Monitoring remained 60 Hz / 512 bands;
recording and streaming remained stopped. The staged installer repaired the
config group and upgraded both services. Config and web-credential hashes were
unchanged across the upgrade itself.

- Board: Raspberry Pi 4 Model B Rev 1.4, 64-bit aarch64, Debian 13 Trixie.
- Installed release: `0.5.7-dev`.
- Runtime archive: 174230 bytes; SHA-256
  `0ce9501fb06bcf17923862f4d2491c22e69380161e05c16fa251dd37c3849a3c`.
- Active config SHA-256:
  `0a411c9a23c76d450dbb7a81b3572b8c604f500875a6988fbba8f116fb02163e`.
- [Raw bounded profiler evidence](RPI4_WARPED_DASHBOARD_2026-09-08.json), SHA-256
  `e755731cdda34189e3e098ac2b71e5c7f39aa2959e286efad339cd8be52fef58`.

## Results

One authenticated, visible dashboard ran through a 150-second collection with
151 samples and no missed profiler sample slots. The existing profiler reported
`execution_status: pass`: continuous capture, stable processes, bounded queues,
no runtime errors, and no firmware throttling. Both services had zero restarts
after upgrade and retained their PIDs (media 1959, web 1955).

| Measurement | Result |
|---|---:|
| Media meter sequence rate | 59.99 Hz |
| Media spectrum sequence rate | 59.99 Hz |
| Whole-system CPU, mean / maximum | 23.65% / 30.75% |
| Media CPU, mean (one-core percentage) | 58.83% |
| Web CPU, mean (one-core percentage) | 29.01% |
| Media RSS, minimum / maximum | 38092800 / 38354944 bytes |
| Web RSS throughout collection | 26562560 bytes |
| Temperature, mean / maximum | 54.92 / 57.94 C |
| Spectrum queue maximum | 1 buffer / 960 bytes / 10 ms |
| Level queue maximum | 0 buffers |
| Firmware throttling | `0x0` throughout |

Sequence rates are calculated from the first and last media counters over the
sampled elapsed interval. They describe produced telemetry, not unique browser
deliveries or rendered frames per second.

At 1280 x 720, the browser showed live mono peak/RMS readings, the 24 kHz upper
axis, expanded newest five seconds, and populated history reaching 120 seconds.
There was no document overflow and no captured browser warning/error. The
offline dashboard state checks also passed before deployment.

## Limits and follow-up

This run did not measure browser frame time or verify that every produced
sample arrived at the browser. Fine vertical dark lines were visible near the
live edge; fractional canvas stripe edges and the existing arrival-gap display
are possible causes, not confirmed audio dropouts. Browser rendering still
needs a separate frame-time/seam investigation if those lines are distracting.

The 480-column bound and peak rollups have offline regression coverage; runtime
browser heap/buffer instrumentation was not performed. This is not endurance,
active recording/SRT, receiver continuity, multi-client, fault, or Step 6
acceptance. Hardware and microphone differ from the prior Scarlett run, so CPU
figures are not a controlled before/after rendering comparison.

## Same-day 0.5.8 display correction

The reported broad stripes were reproduced by the renderer's 33.3 ms span
limit at 60 Hz: samples are positioned by browser arrival time, which can be
batched. A separate ten-second SSE probe over the local SSH tunnel received
595 events and 496 distinct spectrum samples; median distinct-sample arrival
spacing was 16 ms, maximum 62 ms, with 13 intervals above 33.3 ms and 20 below
1 ms. This probe used the Windows monotonic clock and was not a browser
frame-time measurement. Fractional canvas boundaries also allowed thin seams.

Version `0.5.8-dev` holds each received color through short delivery/paint
delays, capped at 250 ms for the 60 Hz live edge. Longer interruptions still
leave blank history. Existing lower-rate and older-bucket bounds remain in
place. Horizontal edges are aligned to whole device pixels. This is a bounded
display hold, not interpolation or recovery of unreceived samples.

The actual paint-span regression covers continuous 50–150 ms arrival jitter,
pixel alignment, and a visible one-second interruption. Offline dashboard
checks and 18 web asset tests passed. The installed live dashboard at
1920 x 1440 showed continuous color strips with the recurring black stripes
gone; no browser warning/error was captured. The temporary viewport override
was reset afterward. No new audio, transport, or frame-rate claim is made.

Installed archive: 174349 bytes, SHA-256
`6cdb57806166209881c11a447c718eeb6e6afd1eec3c384a98fff784e4a59cf7`.
The configuration and credential hashes were preserved. Media PID 174963 and
web PID 174958 remained active with zero restarts after upgrade; throttling
flags were `0x0`. The earlier 150-second evidence above remains bound to 0.5.7.
