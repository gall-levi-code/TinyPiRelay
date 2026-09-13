# ADR 0002: Serialized programmable media owner and isolated branches

- Status: accepted and implemented for the Step 2 evidence scope
- Date: 2026-08-18
- Supersedes: none; refines [ADR 0001](0001-minimal-architecture.md)

## Context

TinyPiRelay needs one persistent programmable GStreamer owner for exact ALSA
capture, one SRT caller stream, native lossless recording, and disposable
level/spectrum monitoring. Runtime commands and media callbacks can arrive
concurrently, while GStreamer object mutation must remain on one GLib context.
A slow or failed branch must not cause an avoidable capture restart.

Step 1 admitted three finite stream representations. Step 2 now implements the
production media graph and serialized controller in
[`gstreamer_engine.py`](../../src/tinypirelay/gstreamer_engine.py),
[`media_runtime.py`](../../src/tinypirelay/media_runtime.py),
[`media_state.py`](../../src/tinypirelay/media_state.py), and
[`media_service.py`](../../src/tinypirelay/media_service.py). Target synthetic
and physical ALSA artifacts exercise the implemented Opus lifecycle, native
FLAC recording, monitoring, reconnect, and shutdown. They do not establish
endurance or every receiver/storage failure mode.

## Decision

### Ownership and entry point

One media process owns the pipeline and authoritative runtime state. External
commands, timers, bus messages, streaming signals, and event delivery are
marshalled onto one owner context. The pure reducer always returns the next
state plus explicit effects; the runtime adopts that state even when a safety
decision also reports an error.

Each logical capture, stream, and recording lifecycle has a generation. Each
physical dynamic branch has an instance token. Completion, error, statistics,
retry, and removal callbacks must match both applicable identities before they
can affect current state. The token is published at attachment before a branch
can asynchronously reach `PLAYING` or fail.

The repository entry point is:

```sh
PYTHONPATH=src python3 -m tinypirelay.media_service \
  --config /path/to/config.json \
  --evidence evidence/stream_compatibility.json
```

It validates capability evidence and strict configuration before starting the
GLib loop. `alsasrc` is the default; `audiotestsrc` is a controlled integration
option only. The stable inter-process protocol remains Step 3 scope.

### Pipeline topology

```text
alsasrc -> exact native caps -> tee allow-not-linked=true
                              |-> queue 5 s, non-leaky
                              |    -> flacenc -> filesink async=false
                              |-> queue 10 s, non-leaky -> convert/resample
                              |    -> S16LE/48 kHz/stereo -> codec/parser
                              |    -> streamable Matroska -> valve
                              |    -> srtsink caller async=false
                              |-> queue 4 buffers, downstream-leaky
                              |    -> level -> fakesink async=false
                              `-> queue 4 buffers, downstream-leaky
                                   -> spectrum -> fakesink async=false
```

Every production element is created and linked programmatically. Native source
caps name one validated device tuple, not a product of separately observed
properties. Recording remains before stream conversion. On the tested iMM-6C,
this preserves native mono S16LE/48 kHz recording while the stream branch
explicitly converts channel count to stereo.

The admitted stream chains are exactly:

- PCM S16LE directly into streamable `matroskamux`;
- `flacenc quality=5` directly into streamable `matroskamux`, with no parser;
  and
- `opusenc ! opusparse` into streamable `matroskamux`.

Dynamic `GstBaseSink` endpoints use `async=false`. This avoids an attached bin
remaining in asynchronous PAUSED preroll after joining a live `PLAYING`
pipeline. It applies to `srtsink`, recording `filesink`, and monitoring
`fakesink`; it does not disable the stream sink's normal clock synchronization.

### Transport failure containment

A core `valve` sits immediately before every `srtsink`. A bus sync handler keeps
a lock-protected registry of exact, physically attached sink-to-valve pairs.
The registry entry exists before the tee is unblocked, survives logical
retirement/finalization and brief old/new overlap, and is removed only after
physical teardown.

When an exact registered `srtsink` posts `ERROR`, the sync handler only sets its
valve to `drop=true` and returns `PASS`. It does not unlink, change application
state, or deliver callbacks. The ordinary asynchronous bus handler then reports
loss and detaches the branch. This ordering uses the valve's documented ability
to turn downstream flow errors into successful dropped buffers and prevents a
transport `GST_FLOW_ERROR` from propagating through the bounded stream queue to
the shared tee/source.

See the official
[`valve`](https://gstreamer.freedesktop.org/documentation/coreelements/valve.html),
[`GstBaseSink`](https://gstreamer.freedesktop.org/documentation/base/gstbasesink.html),
and [`GstBus`](https://gstreamer.freedesktop.org/documentation/gstreamer/gstbus.html#gst_bus_set_sync_handler)
documentation for the underlying primitives.

### Dynamic branch lifecycle

Attachment uses a blocked requested tee pad. Its exact physical transport guard
is registered before data is unblocked. The active branch and token are then
recorded immediately after unblock, with `branch-created` delivery deferred on
the owner context; the guard covers the narrow interval between those actions.
`branch-added` is sent only after the bin reaches `PLAYING`.

Removal schedules exactly one IDLE drain, unlinks/releases the tee request pad,
and sends EOS into the retained branch entry sink. EOS is intercepted after the
encoder/mux boundary so it cannot stop the capture pipeline. Recording and
ordinary connected-stream teardown may drain; a never-connected attempt is
aborted without drain so it cannot overlap a retry. The EOS bound is eight
seconds, longer than the five-second recording queue bound. Timeout removal is
marked forced and is never described as successful file finalization.

Rotation attaches the replacement FLAC branch first and then finalizes the old
branch. This accepts a small possible sample overlap at the boundary in exchange
for capture continuity. Target artifacts show both rotated files and the final
shutdown file with FLAC headers and decoder EOS, with non-forced removal.

### State, timers, and reconnect

The explicit states are independent:

- service: `running`, `shutting_down`, `stopped`;
- capture/stream: `stopped`, `starting`, `running`, `stopping`, `failed`;
- recording: `stopped`, `starting`, `running`, `stopping`, `blocked`, `failed`;
- connection: `disconnected`, `connecting`, `connected`, `retry_wait`, `failed`.

`srtsink` operates in caller mode with automatic reconnect disabled. A physical
attempt has a five-second fallback watchdog. On the target, an offline receiver
produced native SRT `Connection timeout (16)` errors after about 2.95 seconds,
so native failure won before the fallback. The exact failed token was retired
and removed with `forced=false` before one retry timer advanced through the
1, 2, 4, 8, 16, 30-second capped schedule. After receiver restoration, a
distinct token connected and receiver buffers advanced.

Although handlers exist, target caller-mode runs did not observe a
`caller-added` signal. The runtime therefore also treats a positive
`bytes-sent-total` from the one-Hz, allow-listed statistics poll as connected,
but only when generation and physical token still match. Zero/stale statistics,
late caller signals, and late branch errors are ignored. Stream stop and
shutdown cancel attempt/retry timers.

All exact `srtsink` errors are currently treated as retryable. No retained
target evidence distinguishes invalid-host, DNS, authentication, or encryption
signatures reliably enough to enter a non-retryable state. Such classification
is pending and unsupported; it is not inferred from error text.

Exact repeated operations are idempotent. Configuration changes while active
require a controlled restart except for Opus bitrate, which becomes
authoritative only after backend property readback. Shutdown finalizes
recording, stops stream and retry, removes monitoring, stops capture, and then
completes the service.

### Storage and telemetry

Recording checks the exact configured destination before start and every five
seconds while active. An optional required mount prevents silently writing to
an underlying root directory when removable storage disappears. The hard stop
is `used * 100 >= total * 90`; failure affects recording only and latches a
warning until a fresh safe check. The target 90% scenario used explicit status
injection, not a physically filled disk.

Level and spectrum messages are normalized into latest-only finite snapshots.
Their four-buffer downstream-leaky queues make telemetry disposable. Current
queue levels/maxima and allow-listed primitive SRT statistics are polled at one
Hz; no history or secret-bearing structure is retained.

## Implemented bounds

| Bound | Value |
|---|---:|
| Stream queue | 10 seconds |
| Recording queue | 5 seconds |
| Monitoring queue | 4 buffers per branch |
| Branch-local EOS | 8 seconds |
| Connection-attempt fallback | 5 seconds |
| Retry | 1, 2, 4, 8, 16, then 30 seconds capped |
| Statistics poll | 1 second |
| Storage poll | 5 seconds |

## Evidence boundary

The 44.708-second
[`step2_synthetic_rpi_zero_w_2026-08-18.json`](../../evidence/step2_synthetic_rpi_zero_w_2026-08-18.json)
run passed on the Raspberry Pi Zero W with `audiotestsrc`. It exercised the real
engine/runtime, live Opus bitrate readback, concurrent stream/record/telemetry,
FLAC rotation and decoder EOS, a simulated storage stop, bounded no-consumer
telemetry, receiver loss/backoff/recovery, and ordered shutdown.

The 45.782-second
[`step2_alsa_rpi_zero_w_imm6c_2026-08-18.json`](../../evidence/step2_alsa_rpi_zero_w_imm6c_2026-08-18.json)
run passed the same scope using the real iMM-6C at native mono S16LE/48 kHz and
the explicit stereo Opus conversion. It retained no fatal/scenario errors and
validated rotated, stopped, storage-stopped, and shutdown FLAC files to decoder
EOS. Its disposable loopback receiver is evidence machinery, not a production
receiver.

This accepts Step 2 within that scope. The following remain open:

- physical near-full, read-only, unmounted, and returned storage;
- capture-device unplug/loss and return;
- stream ID, passphrase, encryption, DNS, and authentication paths;
- endurance, power-loss, thermal, and long-term queue behavior;
- the workstation SRT endpoint, which appeared TS-oriented and rejected the
  approved Matroska stream; and
- the ALSA run's stream queue growth to 7.56 seconds of its 10-second bound, a
  material Zero W performance risk despite continuous receiver progress.

The Step 1 evidence remains authoritative for admission of all three stream
representations. The Step 2 target runs are fresh physical evidence for the
implemented Opus lifecycle and native FLAC recording, not a blanket board,
receiver, or endurance claim.

## Consequences

- The production owner can add, replace, and finalize branches without a shell
  pipeline or avoidable whole-capture restart.
- Bounded non-leaky required-media queues fail visibly and detach locally;
  disposable telemetry queues may drop.
- Owner generations and physical tokens make late asynchronous work harmless.
- Transport errors are contained at the exact failing sink while retry remains
  observable in application state.
- Native recording and converted streaming coexist without relabeling the
  microphone as natively stereo.
- Broad Zero W deployment remains premature until the queue-growth and
  endurance risks are resolved.

## Alternatives rejected

- Shell pipelines, FFmpeg, or VLC cannot provide the required serialized
  dynamic-branch lifecycle.
- GStreamer-owned automatic reconnect would split ownership of retry timing and
  token state.
- Sending pipeline-wide EOS to stop one recording would threaten capture and
  stream.
- Leaky stream or recording queues would silently discard required media.
- Allowing a downstream SRT flow error to propagate to the tee would trade a
  receiver failure for a capture failure.
- Treating schema acceptance or one plugin error string as hardware,
  interoperability, encryption, or non-retryable-auth evidence would create a
  false capability claim.
