# TinyPiRelay media service

Status: the Step 2 media owner and Step 3 local-control/leased-monitoring
integration are implemented, with finite target evidence. This is a
programmable PyGObject media owner, not a shell-pipeline wrapper. The evidence
is deliberately narrower than a production or endurance claim; the remaining
gaps are listed below.

The product requirements are in [PROJECT_SPEC.md](PROJECT_SPEC.md). Architecture
rationale is in [ADR 0001](adr/0001-minimal-architecture.md),
[ADR 0002](adr/0002-media-engine.md), and
[ADR 0003](adr/0003-web-control-plane.md).

## Components and entry point

| Module | Responsibility |
|---|---|
| [`media_config.py`](../src/tinypirelay/media_config.py) | Strict JSON parsing, approved-representation gate, secret-safe configuration, and read-only recording-destination checks |
| [`media_state.py`](../src/tinypirelay/media_state.py) | Pure capture, stream, connection, recording, monitoring, and shutdown reducer |
| [`media_runtime.py`](../src/tinypirelay/media_runtime.py) | Serialized effects, generations/tokens, timers, storage checks, statistics, and latest-only telemetry |
| [`gstreamer_engine.py`](../src/tinypirelay/gstreamer_engine.py) | Lazy PyGObject load and programmatic GStreamer graph/branch ownership |
| [`media_service.py`](../src/tinypirelay/media_service.py) | One GLib owner context, signal handling, configuration/evidence validation, startup, and ordered process shutdown |
| [`media_control.py`](../src/tinypirelay/media_control.py) | Step 3 public-operation mapping, strict redacted configuration DTO, revisions, and restart classification |
| [`control_protocol.py`](../src/tinypirelay/control_protocol.py) | Step 3 bounded, permission-restricted AF_UNIX request/response boundary |
| [`step2_media_integration.py`](../spikes/step2_media_integration.py) | Retained target harness with a disposable local GStreamer receiver; not a production component |

From the repository root, the service entry point is:

```sh
PYTHONPATH=src python3 -m tinypirelay.media_service \
  --config /path/to/config.json \
  --evidence evidence/stream_compatibility.json \
  --source-factory alsasrc \
  --log-level INFO
```

`--evidence` defaults to the repository compatibility record.
`--source-factory audiotestsrc` is restricted to controlled integration work,
and `--record-on-start` is an integration/bootstrap convenience. The checked-in
[`example.json`](../config/example.json) is intentionally disabled and must be
copied and populated with an exact capture mode and any desired stream settings.
With Step 3, `--control-socket PATH` enables the permission-restricted local
AF_UNIX control boundary documented in [WEB_CONTROL.md](WEB_CONTROL.md). The
media process still exposes no HTTP listener; the independent authenticated web
process owns that boundary.

## Configuration and safety contract

The parser rejects unknown or missing fields, duplicate JSON keys, non-finite
numbers, invalid ranges, and representations absent from
[`stream_compatibility.json`](../evidence/stream_compatibility.json). A stream
passphrase is excluded from object representations, state snapshots,
statistics, and errors. Until encrypted SRT interoperability is proven, the
input contract is conservatively limited to 10–79 printable ASCII characters;
the schema, web form, and runtime enforce the same bound.

Capture configuration names one ALSA device and one exact format/rate/channel
tuple. The source is constrained to those native caps, and startup is
acknowledged only after the pipeline reaches `PLAYING`; a negotiation or ALSA
failure is not reported as a successful start. Stream conversion is labeled
separately. The target iMM-6C evidence is native mono `S16LE`, 48 kHz capture
converted to stereo only inside the stream branch.

Only these Step 1-approved SRT caller representations are admitted:

| Representation ID | Chain after stream conversion |
|---|---|
| `pcm_s16le_48000_stereo_matroska` | `matroskamux streamable=true` |
| `flac_48000_stereo_matroska` | `flacenc quality=5 -> matroskamux streamable=true` |
| `opus_128k_48000_stereo_matroska` | `opusenc -> opusparse -> matroskamux streamable=true` |

All consume `S16LE`, 48 kHz, stereo representation-input caps. The FLAC sender
has no parser, matching the corrected Step 1 evidence. Opus presets are 64, 96,
128, and 192 kbit/s. Runtime bitrate mutation is accepted only after setting the
encoder property and reading the applied value back without rebuilding the
branch.

Of the configured capture formats, native recording accepts only those the
installed `flacenc` path can preserve losslessly (`S16LE`, `S24LE`, and
`S24_32LE`). `S32LE` fails closed. The configured destination must exist, be
writable/searchable,
match the optional required mount, and expose readable usage for that exact
filesystem. `used * 100 >= total * 90` blocks or stops recording while leaving
capture and stream active.

## Programmatic topology and bounds

```text
alsasrc -> exact native caps -> tee allow-not-linked=true
                              |-> queue 5 s, non-leaky -> flacenc -> filesink
                              |-> queue 10 s, non-leaky -> convert/resample
                              |    -> S16LE/48 kHz/stereo -> codec/parser
                              |    -> matroskamux streamable=true
                              |    -> valve -> srtsink caller
                              |-> queue 4 buffers, downstream-leaky
                              |    -> convert -> level -> fakesink
                              `-> queue 4 buffers, downstream-leaky
                                   -> convert -> spectrum -> fakesink
```

Every element and link in the production graph is created programmatically;
`Gst.parse_launch` appears only in the disposable integration receiver. The
fixed operational bounds are:

| Control | Value |
|---|---:|
| Stream queue | 10 seconds, non-leaky |
| Recording queue | 5 seconds, non-leaky |
| Level/spectrum queues | 4 buffers each, downstream-leaky |
| Branch-local EOS finalization | 8 seconds |
| SRT connection-attempt fallback | 5 seconds |
| Retry delays | 1, 2, 4, 8, 16, then 30 seconds indefinitely |
| SRT statistics polling | 1 Hz |
| Recording storage polling | every 5 seconds |

All dynamic branch endpoints are configured with the
[`GstBaseSink`](https://gstreamer.freedesktop.org/documentation/base/gstbasesink.html)
`async=false` property: `srtsink`, recording `filesink`, and monitoring
`fakesink`. This prevents a branch attached to an already-`PLAYING` live
pipeline from remaining in asynchronous preroll. Stream clock synchronization
is otherwise unchanged; the recording and monitoring sinks also use
`sync=false`.

## Ownership and dynamic branches

Commands, timers, bus messages, SRT signals, and engine events are marshalled to
one owning GLib context before they mutate state or the graph. Capture, stream,
and recording generations reject stale lifecycle completions. Each physical
branch also has an instance token so a late connection, error, removal, or
statistics result cannot apply to a replacement branch.

Attachment blocks the requested tee pad, builds and links the bin, synchronizes
state, and registers its physical transport guard before unblocking data. The
active branch/token record is installed immediately after unblock and
`branch-created` delivery is deferred on the owner context; the physical guard
therefore covers the narrow interval before active registration. `branch-added`
is the authoritative `PLAYING` acknowledgement.

Removal schedules its drain exactly once from an IDLE probe, unlinks and
releases the tee request pad, then sends EOS into the retained branch entry
sink. Encoder/mux EOS is intercepted so it cannot become pipeline-wide EOS.
Successful terminal EOS finalizes the branch; the eight-second bound
force-removes only that branch and reports that the file was not safely
finalized. Rotation first attaches the replacement FLAC branch and then drains
the old branch, allowing a small boundary overlap rather than a capture gap.

## Transport isolation and reconnect

The stream branch places a core
[`valve`](https://gstreamer.freedesktop.org/documentation/coreelements/valve.html)
immediately before `srtsink`. A
[`GstBus` sync handler](https://gstreamer.freedesktop.org/documentation/gstreamer/gstbus.html#gst_bus_set_sync_handler)
matches only a physically attached stream branch's exact `srtsink`. On its
`ERROR`, the handler synchronously sets that branch's valve to `drop=true` and
returns `PASS`; it performs no graph mutation or event delivery. The normal
asynchronous bus path then reports the loss and retires the branch. The guard is
installed before tee unblock, remains valid while a branch is finalizing and
during brief old/new overlap, and is removed only after physical teardown. This
contains `GST_FLOW_ERROR` from the SRT sink so it cannot collapse the shared
capture pipeline.

`srtsink` is caller-only with `auto-reconnect=false`. Connection policy belongs
to the owner:

1. A new token enters `connecting` and starts a five-second attempt watchdog.
2. Target GStreamer did not emit an observed `caller-added` notification.
   Therefore a positive, generation- and token-matched `bytes-sent-total` from
   the one-Hz allow-listed statistics poll is also authoritative connection
   evidence.
3. A native sink error schedules one bounded retry while the engine immediately
   detaches that branch; target removal completed before the retry became due.
   A watchdog expiry instead marks its token retiring, aborts that exact
   still-connecting physical attempt without EOS drain, and waits for factual
   removal before posting the failure that schedules retry.
4. One retry timer uses 1, 2, 4, 8, 16, then 30 seconds. Stop/shutdown cancels it;
   stale timers and late callbacks are ignored.
5. Recovery creates a distinct physical token. Positive statistics and receiver
   decode progress confirm it; successful recovery clears consecutive failures
   but retains the session reconnect count.

With the loopback listener offline, both target runs received native SRT
`Connection timeout (16)` errors in about 2.95 seconds, before the five-second
fallback. Each failed token was removed with `forced=false`, backoff occurred,
and a distinct token connected after the listener returned. The fallback still
bounds plugin versions or failure modes that do not report a native error.

The installed target did not provide evidence for reliable invalid-host,
authentication, or encryption error classification. Consequently every exact
`srtsink` error currently enters the same bounded retry path. Non-retryable
DNS/auth classification is pending and unsupported rather than claimed.

## Recording, monitoring, and shutdown

Recording starts only after a fresh destination check and repeats the check
every five seconds. Start, rotation, explicit stop, storage stop, and shutdown
all use branch-local FLAC finalization. A forced removal is never reported as a
finalized fragment. The service does not delete recordings automatically.

Level and spectrum messages are normalized into finite, latest-only snapshots
on the owner context. Level data contains bounded per-channel peak/RMS,
clipping, and no-signal values; spectrum is capped by the configured cadence and
band count. Slow or absent consumers cannot retain media buffers because both
monitor branches are four-buffer downstream-leaky. Adapter statistics expose
only allow-listed primitive fields and current queue levels/maxima; no histories
or secrets accumulate.

Step 3 keeps level attached with capture but makes spectrum lease-driven. Live
spectrum removal uses an owner-context IDLE probe to block, unlink, and release
the tee pad before the branch reaches `NULL`; bus messages are accepted only
from the currently active branch. This replaced unsafe immediate teardown found
during physical testing. Three physical attach/remove cycles (release, expiry,
release) then produced new samples with stable capture/stream/recording
generations and advancing SRT byte statistics; see the
[Step 3 physical report](evidence/RPI_ZERO_W_STEP3_2026-08-19.md).

Shutdown is ordered and idempotent: finalize recording, stop/cancel stream and
retry, remove monitoring, stop capture, then emit service completion. A branch
timeout is recorded but cannot hold shutdown indefinitely.

## Validation and retained evidence

The host suite exercises strict configuration, pure state transitions,
generations/tokens, exact branch plans/properties, GLib callback arity,
transport-valve isolation, dynamic attach/removal races, EOS/rotation,
statistics typing, simulated storage faults, reconnect timers, telemetry bounds,
and ordered shutdown without importing GI where that is not needed.

Target acceptance used the real engine/runtime/service components and a
disposable local `srtsrc -> matroskademux -> opusparse -> opusdec` receiver:

| Artifact | Evidence class and passed scope |
|---|---|
| [`step2_synthetic_rpi_zero_w_2026-08-18.json`](../evidence/step2_synthetic_rpi_zero_w_2026-08-18.json) | 44.708-second Raspberry Pi Zero W run with `audiotestsrc`: programmable Opus stream, 128 -> 64 -> 192 kbit/s readback without restart, concurrent FLAC recording/rotation, decoded-to-EOS fragments, simulated 90% isolation, zero-consumer monitoring, native-failure/backoff/recovery, and ordered shutdown all passed. This is target-software evidence, not microphone evidence. |
| [`step2_alsa_rpi_zero_w_imm6c_2026-08-18.json`](../evidence/step2_alsa_rpi_zero_w_imm6c_2026-08-18.json) | 45.782-second hardware run with `hw:CARD=iMM6C,DEV=0`, native mono S16LE/48 kHz capture and explicitly converted stereo Opus: the same lifecycle scenarios passed, receiver buffers advanced, rotated/stopped/shutdown FLAC files had headers and decoded to EOS, reconnect recovered on a distinct token, and no fatal or scenario errors were retained. |

These Step 2 runs exercise the Opus representation. The three exact PCM, FLAC,
and Opus stream encodings remain admitted by the earlier
[Step 1 physical report](evidence/RPI_ZERO_W_2026-08-18.md) and are covered by
the current programmatic plan/property tests; Step 2 does not inflate the Opus
integration run into fresh physical evidence for every stream encoding.

The ALSA run retained bounded telemetry with no consumer and continuous receiver
progress, but its stream queue reached 7.56 seconds of the 10-second bound. That
is a material Raspberry Pi Zero W performance/endurance risk, not a pass for
unbounded operation.

Step 3 adds finite physical evidence for the independent web/control boundary,
FLAC finalization, slow SSE resource bounds, and repeated leased-spectrum
reattachment. It does not replace the Step 2 media acceptance or establish
endurance; exact results and evidence classifications are in the
[Step 3 physical report](evidence/RPI_ZERO_W_STEP3_2026-08-19.md).

## Open evidence and limitations

- The 90% scenario used explicit status injection. A real nearly-full disk,
  read-only filesystem, disappeared mount, and mount return have not been
  exercised on target storage.
- Physical capture-device unplug/loss and return are untested. Reducer, runtime,
  and error-isolation paths have software coverage only.
- Stream ID routing, passphrase handling, SRT encryption, DNS failure, and
  authentication failure have not been validated end to end. A configured
  `pbkeylen=16` is not encryption evidence.
- No endurance/soak, power-loss, thermal/throttling, or long-term queue-growth
  run has been completed. The 7.56-second queue observation must be resolved or
  bounded operationally before broad Zero W acceptance. A later same-Pi
  sender/receiver diagnostic reached exact `queue_overrun` after 50.953 seconds,
  strengthening this as a Step 5 headroom risk.
- Step 3 used a live FFplay listener at `192.0.2.30:9141` (publication-redacted endpoint) to move decode load
  off the Pi. Its output was not retained or hashed, so the run proves only
  sender connection/byte progress during the strict web-failure slice, not new
  receiver compatibility. The supplied FRAME ingest at
  `192.0.2.30:4001` remains unverified; approved recipes were not changed.
- SRT later stopped advancing in the 91-second Step 3 run. The cause was not
  proven and receiver lifecycle was a confounder, so that run does not establish
  full-run continuity or slow-client noninterference with media.
- Dynamic connection detection currently depends on positive statistics when
  caller signals are absent. Additional GStreamer/libsrt versions and receiver
  products remain unverified.

Within those explicit limits, Steps 2 and 3 are complete for their finite
scopes. Installer/systemd lifecycle, runtime discovery/hotplug promotion,
retention policy, profiling, and release hardening remain later milestones.
