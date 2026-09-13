# Step 2 — Media Engine

## Goal

Build the persistent programmable GStreamer media service with isolated streaming, recording, meter, and spectrum branches.

## Required work

- Read the shared documents, Step 1 evidence/ADRs, and `docs/PROJECT_STATE.md`. Recheck assumptions against installed GStreamer capabilities.
- Implement explicit capture, stream, recording, and connection state machines with serialized commands and clear errors.
- Construct pipelines programmatically with independent bounded queues. Monitoring must be leaky/disposable and unable to back-pressure capture.
- Implement only compatibility-table-approved PCM, FLAC, and Opus streaming representations in SRT caller mode.
- Implement bounded reconnect and expose connection status, statistics, last error, and reconnect count.
- Implement runtime Opus bitrate changes where supported. If the installed element cannot do so safely, reject the operation without restarting the stream and document the limitation.
- Implement FLAC recording start/stop and safe rotation without restarting an active stream.
- Enforce the recording destination's 90%-used hard stop before start and during recording; stop/finalize only recording and latch a clear warning until safe.
- Emit bounded peak/RMS/clip and spectrum telemetry suitable for the later web process.
- Isolate and report device loss, recording I/O failure, and monitoring failure without avoidable whole-pipeline restarts.

## Required scenario checks

- Start an Opus stream; change 128 -> 64 -> 192 kbps; verify capture and the SRT connection remain established.
- Toggle FLAC recording on and off while streaming; verify the stream is uninterrupted and files finalize.
- Cross the 90% storage threshold; verify recording stops and SRT remains active.
- Disconnect and restore the receiver/network; verify bounded reconnect and no busy loop.
- Stall or remove monitoring consumers; verify streaming continues and queues remain bounded.
- Simulate device loss, read-only storage, invalid caps/config, repeated commands, and service shutdown.

## Out of scope

The production web UI/authentication, installer/systemd lifecycle, hardware optimization, multi-destination streaming, listener/rendezvous, automatic bitrate adaptation, and deletion/retention policy.

## Definition of done

- State and branch-transition tests pass without requiring physical hardware where fixtures/fakes are appropriate.
- At least one documented end-to-end SRT integration path runs where local dependencies permit; unavailable hardware evidence stays open.
- No unbounded telemetry queues, silent media restarts, or false compatibility claims remain.
- Operations and limitations are documented and `docs/PROJECT_STATE.md` is ready for Step 3.
