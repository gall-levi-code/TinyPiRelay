# Raspberry Pi 4 60 Hz dashboard validation — 2026-09-03

This is a finite monitoring validation, not full Step 5 acceptance.

## Bound identity

- TinyPiRelay: `0.5.6-dev`
- Source archive SHA-256: `8a3feaa1eb2a7bf6f9f057b90f82a9ad8d12726d6c79fbe4afd9047b1a68affa`
- Archive: 53 members, 173593 bytes; no Git, tests, docs, spikes, bytecode, or cache directories
- Board: Raspberry Pi 4 Model B Rev 1.4, 64-bit `aarch64`
- OS: Debian GNU/Linux 13 (trixie)
- Capture: Focusrite Scarlett Solo (3rd Gen.), stereo `S24_32LE`, 48 kHz
- Network: onboard Wi-Fi
- Monitoring: 512 spectrum bands, 60 updates per second
- Browser clients in measured warm run: one authenticated dashboard

The Windows development suite ran 412 tests successfully with 19 expected
platform-dependent skips. The dashboard preview self-test also passed. The
post-change suite was not rerun on the Pi.

## Results

The installed upgrade preserved the 60 Hz/512-band configuration. Both systemd
services remained active with zero restarts. The authenticated browser showed
live numeric left/right peak/RMS values and a 512-bin spectrum.

An authenticated two-second SSE sample received 10 full snapshots and 108
lightweight events: 118 total, or 59.0 events/second. Every event carried a
non-null telemetry object; maximum spectrum width was 512 bins. The measured
wire rate was 563.4 KiB/second. Only 78 unique spectrum sequences arrived in
that cold sample because it included analyzer attachment; the subsequent warm
run measured both meter and spectrum sequence rates at 60.0 Hz.

One visible-dashboard 20-second warm sample measured:

| Measurement | Result |
|---|---:|
| Whole-system CPU | 22.84% |
| Media CPU | 53.15% of one core |
| Web CPU | 30.35% of one core |
| Media RSS | 37796 KiB |
| Web RSS | 42096 KiB |
| Meter sequence rate | 60.0 Hz |
| Spectrum sequence rate | 60.0 Hz |
| Temperature after the run | 62.809 C |
| Firmware throttling flags | `0x0` |

The level queue was empty. The downstream-leaky spectrum queue held one 3840
byte, 10 ms buffer. No runtime error was present. Capture stayed running;
recording and streaming were stopped.

## Limits

- This changes analysis/GUI update cadence, not the 48 kHz audio sample rate.
- SRT, receiver continuity, active recording in the final browser run, four
  simultaneous dashboards, endurance, power loss, and fault recovery were not
  tested here.
- The 563.4 KiB/second result is for one local-tunnel client at 512 bands. Cost
  scales with spectrum width and client count.
- The earlier Zero W dashboard result remains unchanged and unaccepted; this
  Pi 4 result must not be generalized to that board or another audio/network
  setup.
