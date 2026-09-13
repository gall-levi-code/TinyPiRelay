# Raspberry Pi Zero W dashboard deployment — 2026-09-03

Publication copy: LAN endpoints use documentation-only addresses. The original
report is retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

Status: **deployed and visually verified; Zero W dashboard profile not accepted**.
This is a dashboard-integration result, not Step 5 hardware acceptance or Step
6 release approval.

## Bound identity and rollback

- Target: Raspberry Pi Zero W Rev 1.1, 32-bit `armv6l`, Raspbian GNU/Linux 13,
  Dayton iMM-6C USB microphone, Wi-Fi address `192.0.2.10`.
- Installed release: `0.5.3-dev`; installer phase `complete`.
- Frozen source archive SHA-256:
  `bfb3cae37f411c3e7fef52b627decf81ebc42d51b1992a25f57b4f06949a7fee`.
- Installed payload SHA-256:
  `f540a2909608fb1de254475a5ed01452dd48901998c45bfd38714074ef181b4c`.
- Previous installed payload SHA-256:
  `12a29a3778c8d9b28a4aad4a45376a895a509122772ff8b7ec15dd7be77db3492`.
- Pre-upgrade backup SHA-256:
  `7aac7fbbd72738d00be6ef95328a9e82cd0aa66732d44b8fb5a88cf1e8448015`.
  The private archive was copied to the validation workstation, its member list
  and checksum were verified, and access was restricted to the operator,
  Administrators, and SYSTEM. A root-owned installer backup also remains on the
  Pi.
- The old `/opt/tinypirelay/releases/0.5.2-dev` release remains intact. If a
  rollback is needed, run its installer in `upgrade` mode first as a read-only
  plan and then repeat with `--apply`; do not edit the `current` link or install
  journal by hand.

The upgrade used the ordinary installer without `--config`. The media
configuration and web-credential file hashes were identical before and after
the run. All seven pre-existing recording hashes still pass. No package, OS,
firewall, receiver container, or credential change was made.

## Software and appliance checks

- The full Windows run passed 409 tests with 19 platform-dependent skips.
- The first Pi discovery invocation omitted the source root needed by six
  spike-backed modules. It ran 332 application tests with no test failure, two
  skips, and six loader errors. The corrected invocation ran those six modules'
  77 tests successfully. Thus 409 real tests were exercised on the Pi with no
  application-test failure, but this was not one clean discovery invocation.
- Final appliance diagnostics returned `healthy: true`. Both systemd services
  are loaded, enabled, active, and running. The credential file retains its
  required owner, group, and mode `0600`.
- The authenticated status backend reported the real Zero W identity, 32-bit
  architecture, release version, CPU, RAM, temperature, ext4 recording
  filesystem, Wi-Fi interface/address and disk/network rates. The root
  filesystem was about 2.46% used, not the mockup's 72%.
- The live iMM-6C meter produced one real input channel, with no capture runtime
  error. The authenticated dashboard displayed its real changing meter and
  spectrum, and the identity, system, storage, network, SRT, recording, and
  audio-device panels all showed appliance data. No simulated fallback was
  present. The complete fixed workspace was visible without a scrollbar at the
  validation browser's 1160 x 992 viewport.

## Closed-dashboard stream/record shakedown

The immutable candidate streamed FLAC/48 kHz/stereo in Matroska to the
disposable workstation receiver while recording locally for a 120-second,
one-second-cadence profile. The browser dashboard was closed and spectrum
demand was disabled. The profiler-owned recording was then finalized.

| Check | Result |
|---|---|
| Samples and schedule | Pass: 121 samples, zero missed slots; maximum delay 0.094 s |
| Queues and runtime | Pass: all visible and bounded; no overrun, runtime, queue, or recording errors |
| Connection | Pass during the measured window: connected throughout, zero reconnects or consecutive failures |
| Process continuity | Pass: service PIDs and media generations remained stable |
| Temperature and throttling | Pass: maximum 41.16 C; every observation `0x0` |
| Available memory | Pass for this row: minimum 290.92 MiB |
| Descriptors and threads | Pass for this row: within the declared bounds |
| Wi-Fi transmit drops | Pass: 5 before and 5 after |
| System CPU, post-warm-up | p50 56.18%, p95 67.95%, maximum **100%** |
| Media CPU, post-warm-up | p50 47.00%, p95 51.00%, maximum 70.65% |
| Web CPU, post-warm-up | p50 0%, p95 1.00%, maximum 2.00% |

The CPU p95 gate passed, but the formal Zero W maximum gate requires less than
95%. Three consecutive post-warm-up samples reached 100%. No runtime, queue,
connection, or process anomaly accompanied them, and this evidence cannot
attribute their cause. The row therefore does **not** pass formal CPU
acceptance.

The paired receiver decoded 421 seconds of the requested 420-second interval as
FLAC, 48 kHz, two channels. It reported zero missing or non-monotonic PTS, zero
discontinuities, zero errors/warnings, bounded progress gaps, and clean exit.
Decoded audio was discarded. Receiver evidence SHA-256:
`f73775ba0518c30bb4701d157392cff81f8727de4b77ef69610aafc7de9e9914`.

The new finalized recording is 2,917,008 bytes, mode `0600`, and SHA-256
`fdc57fc900c80e3b2f59425cb415aa7c7ecfb9a78513fc3eab1a37c258e3cadf`.
An independent GStreamer read reached FLAC end-of-stream successfully. The raw
closed-profile SHA-256 is
`94cd60a6c46b277f4e8c66c058c69568e59169bcdb471bb8ee6d969b71a8a1f1`.

After the disposable receiver exited, the sender recorded expected connection
timeouts until cleanup. Streaming and recording were then stopped explicitly;
capture remains running for the live meter. Final status has no capture,
stream, recording, monitoring, or media-runtime error. Final diagnostics
evidence SHA-256:
`8069ddfbe44c462c7723da1ac3da8a2ae2ae9e85d8fd20495d5fa017d56a5b5e`.

## Authenticated open-dashboard comparison

After operator authentication, the same 120-second, one-second-cadence profile
was repeated with the real dashboard visible. Capture, FLAC SRT streaming and
FLAC recording configuration were identical. The browser was the only source
of spectrum demand. A first attempt was retained but excluded because its
receiver timer expired before the profiler; the synchronized rerun below is the
comparison row.

| Post-warm-up metric | Dashboard closed | Dashboard open | Open delta |
|---|---:|---:|---:|
| System CPU p50 / p95 / max | 56.18 / 67.95 / 100% | **100 / 100 / 100%** | +43.82 / +32.05 / 0 pp |
| Media CPU p50 / p95 / max | 47.00 / 51.00 / 70.65% | 71.04 / 88.67 / 97.00% | +24.03 / +37.67 / +26.35 pp |
| Web CPU p50 / p95 / max | 0 / 1.00 / 2.00% | 11.06 / 19.99 / 20.99% | +11.06 / +18.99 / +18.99 pp |
| Media RSS p50 / p95 / max | 36.488 / 36.488 / 36.488 MiB | 38.625 / 38.637 / 38.641 MiB | +2.137 / +2.148 / +2.152 MiB |
| Web RSS p50 / p95 / max | 18.945 / 18.945 / 18.945 MiB | 20.551 / 20.656 / 20.668 MiB | +1.605 / +1.711 / +1.723 MiB |
| Temperature p50 / p95 / max | 40.622 / 41.160 / 41.160 C | 42.774 / 43.312 / 43.312 C | +2.152 C throughout |

The open run collected 121 samples with zero missed slots and a maximum sample
delay of 0.197 seconds. System CPU never fell below 95.79%; its post-warm-up
median and p95 were both 100%. It therefore fails both the at-most-85% p95 gate
and the below-95% maximum gate. This is sustained load, not a warm-up spike.

The profiler's other bounded invariants passed: the SRT connection remained
connected with zero reconnects/failures throughout its measured window; queues
remained visible and bounded; no runtime, scope, queue or recording error was
reported; temperature stayed below 43.312 C; throttling stayed `0x0`; available
memory stayed above 292.92 MiB; descriptor/thread bounds passed; and WLAN
RX/TX drops stayed at 0/5. The profiler's local execution result is `pass`, but
its formal profile status remains `not-assessed`. Raw profile SHA-256:
`b09d272f52515f19ca7aec3b84e2d2a667b1aab273efa6cfb653ade301f7e56d`.

The paired open-run receiver decoded 181 seconds with the expected FLAC, 48 kHz,
two-channel caps, no missing/non-monotonic PTS, no FFmpeg warning/error, and a
clean exit. Its strict continuity result nevertheless failed: it observed two
PTS discontinuities and a maximum 1.24-second gap. One visible event was the
1.21-second gap from PTS 169.24 to 170.45 seconds; the retained report cannot
place the other event or correlate either event reliably to the profiler
window. Receiver SHA-256:
`108a9281ee25808c2d900311331d4f1466f73f91111e7781889055d836303bc5`.

The profiler's new 3,010,322-byte recording, SHA-256
`2015b394b188107f040f442eea81706e5f45c216c6d36512832dec63a47dae0b`,
independently decoded to FLAC end-of-stream. The original configuration,
credential and seven pre-existing recording hashes still pass after the open
run. Both services remain active with zero restarts. No profiler or receiver
process remains; streaming and recording are stopped, while capture remains
running.

The open client added about 41.92 percentage points of mean system CPU. It
caused 542 spectrum samples over 120 seconds (about 4.52 Hz under saturation),
while meter delivery fell from exactly 5 Hz closed to about 4.75 Hz open. The
configured 64 spectrum bands are already the validation minimum. Every visible
authenticated route keeps the same 5 Hz SSE connection and therefore the
spectrum lease, even when the dashboard canvas is hidden. Closing the live page
released that lease; a later point status showed spectrum absent, CPU 46.63%,
temperature 40.622 C, no runtime error, and the safe stopped stream/recording
state. That point is cleanup evidence, not another profile. Its SHA-256 is
`4654a76406bb7bebea2763e8fbbec0e6a42f90817a7a1cfc5ab834162b761ff8`.

## Remaining work

The smallest existing tuning experiment is one restart-required configuration
change: reduce spectrum updates from 5 Hz to 1 Hz while retaining 64 bands, then
repeat the matched profile and receiver. Its benefit is a hypothesis until
measured. Setting the rate to 0 is the clean control but removes spectrum.
Beyond that, code changes would be needed to lease spectrum only on pages that
show it and to reduce repeated 5 Hz status/diagnostic serialization.

The tuned candidate would still need the required six-hour soak, long-window
RSS/log behavior, recording rotation, fault matrix, and all other Step 5 gates
before Step 6 can begin.
