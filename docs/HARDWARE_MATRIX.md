# TinyPiRelay Hardware Matrix

This matrix is a planning baseline, not a compatibility claim. Replace expectations with dated evidence as boards are tested.

## Current evidence — 2026-09-11

Step 6 review is authorized with further endurance runs deferred; the broad
board expectations below are not promoted to accepted profiles. The Pi 4 B
has **Hardware tested** evidence only for the named finite scenarios:

- [24-bit recording, 60 Hz monitoring and FLAC/Matroska/SRT into BirdNET](evidence/RPI4_S24_FLAC_BIRDNET_2026-09-09.md):
  30 minutes, 23% mean total CPU, 70.6°C peak, no observed throttling or decoded
  discontinuities, and finalized local FLAC decoding through EOS.
- [Expanded admin pages](evidence/RPI4_ADMIN_PAGES_2026-09-09.md) and
  [interaction stability](evidence/RPI4_INTERACTION_STABILITY_2026-09-09.md):
  finite installed-service/browser checks, not endurance or kiosk acceptance.

The later Zero W dashboard workload was saturated and is not accepted; see
[the measured limitation](evidence/RPI_ZERO_W_DASHBOARD_2026-09-03.md).
Other boards/OS combinations remain **Likely compatible / untested** or
**Unsupported** as listed. No CI-backed hardware claim is made. See
[release readiness](RELEASE_READINESS.md) for unresolved release gates.

## Raspberry Pi targets

| Hardware | Relevant constraints | Initial profile | Expected v1 result | Initial status |
|---|---|---|---|---|
| Zero W / Zero WH v1.1 | BCM2835, single-core ARMv6, 512 MB, 2.4 GHz Wi-Fi, one USB OTG data port | Legacy | Design floor: one stream, FLAC recording, conservative telemetry; finite Steps 1-4 scenarios passed and Step 5 candidate tooling is implemented, but physical profiling, endurance, and performance headroom remain open | Likely compatible / untested |
| Zero 2 W / 2 WH | Quad-core Cortex-A53 class, 512 MB, 2.4 GHz Wi-Fi, one USB OTG data port | Standard | Recommended compact target; full v1 expected with conservative defaults | Likely compatible / untested |
| Pi 3 A+ | Quad-core Cortex-A53 class, 512 MB, dual-band Wi-Fi, one USB port | Standard | Good compact target; watch shared USB/power constraints | Likely compatible / untested |
| Pi 3 B | Quad-core, 1 GB, 2.4 GHz Wi-Fi, 100 Mb Ethernet | Standard | Good fixed-install target; wired networking preferred | Likely compatible / untested |
| Pi 3 B+ | Quad-core, 1 GB, dual-band Wi-Fi, Ethernet | Standard | Very good general target | Likely compatible / untested |
| Pi 4 B | Cortex-A72 class, 1-8 GB, dual-band Wi-Fi, GbE | Full | Excellent; substantial monitoring headroom | Likely compatible / untested |
| Pi 400 | Pi 4 class | Full | Technically excellent, atypical appliance form factor | Likely compatible / untested |
| Pi 5 | Cortex-A76 class, 1-16 GB, dual-band Wi-Fi, GbE | Full | Excellent and overqualified for one stream | Likely compatible / untested |
| Pi 500 / 500+ | Pi 5 class | Full | Technically excellent, atypical appliance form factor | Likely compatible / untested |
| Compute Module 4 / 4S | Pi 4 class; carrier determines audio, USB, storage, and network | Full | Excellent embedded target on a suitable carrier; evidence is carrier-specific | Likely compatible / untested |
| Compute Module 5 | Pi 5 class; carrier-dependent | Full | Excellent embedded target with large headroom; evidence is carrier-specific | Likely compatible / untested |
| Pi 2 B rev 1.3 | Quad-core, 1 GB, Ethernet, no onboard Wi-Fi | Standard/Legacy after probe | Experimental secondary legacy candidate until tested | Likely compatible / untested |
| Older Pi 2 | Older SoC/revision variance | Legacy after probe | Outside initial support until the exact revision is identified and tested | Unsupported |
| Pi 1 / Zero without W | ARMv6; networking requires adapter | Legacy | Possible in narrow configurations, but outside initial support | Unsupported |

Important physical constraints:

- The Zero family's single USB OTG data port may require a powered hub for simultaneous USB audio and wired/USB networking.
- USB audio failures can be power-related. Record power supply, hub, audio device, network interface, OS image, and thermal conditions with results.
- Board profile is selected from model/CPU/RAM plus measured capability; OS bitness alone never selects the performance tier.

## Other SBC candidates

List these only as likely compatible / untested until a community report or project test records the exact board and OS:

| Family | Why it may work | Main uncertainty |
|---|---|---|
| Orange Pi Zero family | Debian/Ubuntu-derived images, ALSA, ARM Linux | Image/package quality, board variants, audio exposure |
| Radxa Zero / Zero 3 | Capable ARM SoCs and Debian-family options | Distro-specific GStreamer/SRT packages and device tree |
| ROCK Pi S / S0 | Small headless ARM boards | CPU margin, image maintenance, I/O |
| FriendlyElec NanoPi family | Broad Debian/Ubuntu support | Large model variance and vendor kernels |
| Generic Debian ARM SBC | Architecture-neutral application stack | Must have supported userland, ALSA capture, required GStreamer elements, systemd, and sufficient resources |

Do not let speculative SBC support complicate v1 Raspberry Pi code. Portability should come from standard Linux interfaces and dependency probes.

## Evidence record

- [Raspberry Pi Zero W Rev 1.1 Step 1 physical evidence, 2026-08-18](evidence/RPI_ZERO_W_2026-08-18.md) — the three exact short PCM/FLAC/Opus representations, queue isolation, and runtime Opus mutation passed on the Zero W with an iMM-6C.
- [Raspberry Pi Zero W Rev 1.1 Step 2 physical evidence, 2026-08-18](evidence/RPI_ZERO_W_STEP2_2026-08-18.md) — both 44-46 second production-engine scenarios passed all 11 finite checks, including iMM-6C capture, dynamic Opus bitrate, native FLAC lifecycle, simulated 90% isolation, bounded monitoring, reconnect, and ordered shutdown. These exact scenarios are **Hardware tested**. That run did not cover endurance, profiling, physical device loss, real storage failure, reboot, or installed-service behavior; the later Step 4 report covers only the finite install/reboot rows below. The ALSA run also reached 7.560 seconds of the 10-second stream-queue bound with the receiver on the same Pi, so the overall board remains **Likely compatible / untested**.
- [Raspberry Pi Zero W Rev 1.1 Step 3 physical evidence, 2026-08-19](evidence/RPI_ZERO_W_STEP3_2026-08-19.md) — finite iMM-6C scenarios exercised independent web `SIGKILL`/restart, authentication/control security, slow SSE bounds, FLAC finalization, and repeated spectrum release/expiry reattachment. These exact rows are **Hardware tested**; device/storage/SRT fault displays and browser QA were simulated. The run is not endurance or full-stream continuity evidence. A separate same-Pi diagnostic hit `queue_overrun` after 50.953 seconds, so the overall board remains **Likely compatible / untested** and Zero W headroom is an explicit Step 5 risk.
- [Raspberry Pi Zero W Rev 1.1 Step 4 physical evidence, 2026-08-20](evidence/RPI_ZERO_W_STEP4_2026-08-20.md) — finite installed-appliance acceptance passed with the iMM-6C. The exact rows below are **Hardware tested**; destructive and version-transition lifecycle cases remain isolated fixtures.
- [Step 5 physical profiling procedure](HARDWARE_PROFILING.md) — the
  `0.5.0-dev` queue instrumentation, bounded Pi profiler, and disposable FFmpeg
  receiver have host/POSIX software coverage. This is a procedure and candidate
  tooling record, not Step 5 physical evidence.

## Step 4 installation evidence boundary

Step 4 is complete for this finite physical scope. Step 5 is in progress, with
its physical results still pending:

| Zero W Step 4 row | Evidence level | Result |
|---|---|---|
| Fresh installation | Hardware tested | Preflight resolved an empty package transaction; immutable release activation and both services passed |
| Repeat installation | Hardware tested | Returned `noop`; service PIDs, state, configuration, credential, and release identity stayed unchanged |
| Least privilege and systemd | Hardware tested | Separate non-root identities, ownership/modes, socket boundary, service properties, and sandbox checks passed |
| iMM-6C capture | Hardware tested | Mono S16LE/48 kHz capture ran through the installed media service |
| Loopback authenticated dashboard | Hardware tested | SSH-tunneled login, live level meter, and spectrum passed |
| Web-only restart isolation | Hardware tested | Only the web PID changed; media PID, capture generation, and active capture remained stable |
| Real FLAC lifecycle | Hardware tested | A finalized recording was retained and decoded to end-of-stream |
| Reboot recovery | Hardware tested | Both services auto-started, capture resumed, dashboard recovered, and private state/recording hashes were preserved |

## Step 5 candidate evidence boundary

The historical `0.5.0-dev` candidate introduced bounded queue measurements for the
active stream, recording, level, and spectrum branches. Its bounded
[Pi profiler](../spikes/step5_hardware_profile.py), disposable
[FFmpeg receiver](../spikes/step5_ffmpeg_receiver.py), and
[profiling procedure](HARDWARE_PROFILING.md) have passed Windows and
network-disabled POSIX software checks. No Step 5 board-profile selection,
tuning change, endurance result, fault result, or Zero W acceptance claim has
been made. The matrix status therefore remains **Likely compatible /
untested** for broad board acceptance. Step 6 review now proceeds under the
operator's explicit deferral, without marking those missing results passed.

The isolated POSIX fixtures, not the board run, cover interruption/resume,
upgrade/reverse-version rollback, preservation uninstall, and explicit purge.
Late audio/network return and active SRT were not exercised in Step 4. This was
a finite acceptance run, not endurance, fault-injection, or Zero W performance-
headroom evidence, so the overall board remains **Likely compatible /
untested**. The approved sender formats remain PCM S16LE, FLAC, and Opus in
streamable Matroska over SRT; MPEG-TS and the workstation receiving bridge
remain deferred until after Step 6. See the
[installation and lifecycle guide](INSTALLATION.md) and
[ADR 0004](adr/0004-installer-system-integration.md).

For each tested board, add a row or linked report containing:

- exact model/revision, architecture, kernel, OS release, and 32/64-bit userland;
- power supply, storage, audio device, capture mode, and network interface;
- GStreamer and SRT plugin versions;
- stream codec/framing, SRT latency, receiver and receiver version;
- simultaneous recording/monitoring settings;
- CPU, memory, temperature, packet/reconnect observations, run duration, and result;
- limitations and the commit tested.

Sources: [Raspberry Pi hardware](https://www.raspberrypi.com/documentation/hardware/rpi/os.html), [Raspberry Pi OS](https://www.raspberrypi.com/documentation/computers/os.html), and [Raspberry Pi USB notes](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html).
