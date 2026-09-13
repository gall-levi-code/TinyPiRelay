# Step 5 — Hardware Profiling and Optimization

## Goal

Measure real behavior, make Pi Zero W v1.1 viable without compromising audio, and replace speculative defaults/claims with evidence across representative 32-bit and 64-bit systems.

## Required work

- Read all shared documents, existing evidence, profiling hooks, and `docs/PROJECT_STATE.md`.
- Create a reproducible benchmark/soak procedure and compact result format recording exact board/revision, OS/kernel/userland architecture, power/storage/audio/network hardware, GStreamer versions, media mode, receiver, monitoring settings, and tested commit.
- Profile the Zero W v1.1 first: PCM, FLAC, and Opus streaming separately; FLAC recording alone and concurrent; runtime Opus bitrate changes; meters; spectrum resolutions/rates; web-client churn; reconnect; rotation; and thermal/resource behavior.
- Then test or leave explicit pending slots for Zero 2 W, a Pi 3-class board, and representative Pi 4/5 32/64-bit systems.
- Measure CPU per process, memory, file descriptors, temperature/throttling, SRT/reconnect statistics, audio discontinuities, recording finalization, disk/log growth, and browser telemetry cost over meaningful duration.
- Choose conservative Legacy, Standard, and Full defaults from measurements. Profile selection uses model/CPU/RAM and observed capability, never bitness alone.
- Under pressure, tune bounded queues, spectrum bands/interval, UI update rates, FLAC compression, and logging. Preserve the specified priority order.
- Update `docs/HARDWARE_MATRIX.md` and product README only with the allowed claim labels and links to evidence.

## Required acceptance evidence

- No hardware claim is upgraded without a dated physical test.
- Zero W runs the accepted v1 subset for a documented soak duration without unbounded growth or UI-induced stream failure, or the precise blocker is reported with evidence.
- Recording hard-stop, network recovery, device loss, web crash, slow client, and reboot recovery are exercised on at least the design-floor board when available.
- 32-bit and 64-bit results are separated from board-performance conclusions.

## Scope control

Do not add multiple destinations, DSP features, bonded networking, retention deletion, or another application implementation. Optimize measured bottlenecks only. If hardware is unavailable, improve the harness/docs and leave the milestone incomplete rather than invent results.

## Definition of done

- Raw results and summarized decisions are reproducible and linked.
- Defaults have safe ceilings and a clear fallback/degradation path.
- Hardware matrix language matches evidence exactly.
- Relevant checks pass and `docs/PROJECT_STATE.md` is ready for independent hardening.
