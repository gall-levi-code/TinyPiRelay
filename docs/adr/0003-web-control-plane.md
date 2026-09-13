# ADR 0003: Authenticated web process over a bounded local control protocol

- Status: accepted and implemented for the finite Step 3 host/target-tested scope
- Date: 2026-08-19
- Monitoring-cadence amendment: 2026-09-03
- Supersedes: none; realizes the process boundary selected by
  [ADR 0001](0001-minimal-architecture.md) and preserves the media owner from
  [ADR 0002](0002-media-engine.md)

## Context

TinyPiRelay requires a phone-friendly web UI, authentication, configuration,
diagnostics, meters, and a browser-rendered spectrogram. The Raspberry Pi Zero
W floor makes an application framework and frontend build toolchain costly, but
the more important constraint is correctness: a web crash, slow browser, or
telemetry backlog must not stop, restart, or back-pressure capture, streaming,
or recording.

The Step 2 reducer and runtime are internal objects, not a public API. Step 3
therefore needs a stable process boundary that preserves one authoritative
media owner, limits untrusted work, makes media unavailability explicit, and
does not leak configuration secrets. It also needs a capability contract that
cannot turn separately observed formats, rates, channels, and codecs into
unverified combinations.

## Decision

### Separate media and web ownership

Keep the media and web owners in separate processes. The media process owns the
GLib context, GStreamer graph, runtime state, capability validation, and durable
media configuration. The web process owns HTTP, authentication, sessions,
browser-facing validation, static assets, and SSE delivery. It never owns a
media branch and never starts a shell media pipeline.

The web process may be killed or restarted without sending a media command.
Sessions are process-local and expire on that restart; capture and dynamic
media branches remain under the existing media owner. When the socket is
unavailable or returns an invalid response, HTTP/SSE reports an explicit
unavailable/degraded state instead of substituting stale success.

### Versioned AF_UNIX request/response protocol

Use a permission-restricted Unix-domain stream socket and standard-library
JSON. Version 1 admits one newline-delimited request and one response per
connection. Envelopes have exact keys, a matching bounded request ID, an
allowlisted operation, and an argument object. JSON rejects duplicate keys and
non-finite values.

Messages are limited to 64 KiB, I/O defaults to a 15-second timeout, and the
listener has eight concurrent client slots. The media service uses a `0750`
socket directory and a `0660` socket; it creates only a missing leaf directory
and does not chmod an existing parent. Access is therefore a local filesystem
permission boundary. Step 4 must assign dedicated service users/groups and
ownership without changing this protocol.

The public operations are status, capabilities, strict config
get/validate/update, capture/stream/recording start and stop, live Opus bitrate,
and a non-operational restart request. Shutdown is not exposed. Responses are
recursively redacted at the protocol boundary and again before HTTP output;
client-visible errors use safe codes/messages.

Three additional allowlisted operations are private to the web process:
read-only `monitoring.telemetry`, `monitoring.spectrum_lease`, and
`monitoring.spectrum_release`. They accept no arguments. The media owner fixes
lease expiry at five seconds, so a crashed web process cannot leave the
spectrum analyzer attached indefinitely.

### Minimal authenticated HTTP service

Use `http.server.ThreadingHTTPServer` with explicit routes and a hard 16-client
semaphore. Serve only the fixed index, CSS, and JavaScript files; do not map URL
paths to arbitrary files or render server templates. Limit request JSON to 32
KiB, reject ambiguous framing, apply a ten-second socket timeout, set `no-store`
and restrictive browser security headers, and disable cross-origin preflight.

Use a versioned `hashlib.scrypt` credential record with a random salt, atomic
mode-`0600` writes, and no default credential. Unknown usernames perform the
same derivation work. Bound derivation concurrency to two. In each 60-second
window, admit at most five attempts per canonical IP/username pair, five across
all usernames for one canonical IP, and 20 process-global. Track at most 1024
pair keys and fail closed at capacity.

If credential replacement succeeds but the parent-directory fsync fails,
report an explicit committed-but-durability-unconfirmed result. Do not claim
rollback after the replacement point.

Keep at most 128 bearer sessions in memory. Store only bearer-token hashes,
enforce 15-minute idle and 24-hour absolute expiry, use an `HttpOnly` and
`SameSite=Strict` bearer cookie, and require the session-bound CSRF token in the
`X-CSRF-Token` header for every mutation. Add the cookie `Secure` flag only
when the configured deployment actually uses HTTPS. TLS and Internet exposure
remain outside Step 3.

### Exact capabilities and safe configuration

Represent capabilities as a hierarchy:

```text
device -> exact native/converted capture mode -> exact stream option
       -> separately evidence-gated representation metadata
```

Require accepted evidence status at every selected level and require the set of
representation IDs to match the Step 1 compatibility gate exactly. The API and
UI present only the options attached to the selected exact mode; neither forms
a Cartesian product.

Return an editable configuration DTO but replace the SRT passphrase with only
its configured state and an explicit `keep`, `clear`, or `replace` mutation.
Serialize configuration updates and require the SHA-256 revision read by the
browser. Reject a stale writer before replacement. Persist via a same-directory
mode-`0600` temporary file, fsync, atomic replace, and directory fsync.

Apply recording-setting changes without capture/stream restart only while the
recording branch is stopped. Apply a supported Opus preset live while stream
state is stable and use the existing encoder property readback for an active
Opus stream. Treat capture, remaining stream, and monitoring changes as
restart-required. Require confirmation before saving them, retain separate
active and saved revisions, and reject new starts until an external service
manager performs the restart. Safe stop operations remain available. Step 3
does not pretend its restart placeholder controls a process manager.

Serialize direct and configuration-driven bitrate mutations. A pending direct
change rejects a live configuration application before backend mutation.
Candidate backend configuration plus bitrate/readback form one transaction: a
failure or mismatched readback rolls both back before the runtime revision is
committed. Persistence intentionally precedes live application, so an apply
failure is reported as saved-but-not-active and restart-required rather than
as an unsaved edit.

### Latest-only browser telemetry

Use SSE because telemetry and state flow only from server to browser. Send one
full latest snapshot every 0.2 seconds (approximately 5 Hz). Between those
snapshots, send only the newest meter/spectrum object at the configured 5-60 Hz
monitoring cadence. Do not retain per-client history. Admit at most four SSE
clients; they also count against the HTTP client bound and are removed on
disconnect/write timeout. Media telemetry remains numeric, finite, capped,
latest-only data from Step 2. The browser coalesces rendering to animation
frames and draws meters and bounded spectrogram time buckets locally.

Keep level metering attached with capture, but do not attach spectrum at
capture startup. The first SSE client acquires the fixed media-owned lease;
all clients share the same analyzer and newest sample. The web process renews
every two seconds while at least one client exists, only the last orderly
disconnect releases, and five-second expiry handles web crash. Adding/removing
spectrum occurs on the engine owner context and touches no capture, stream,
recording, or level branch. Its queue remains downstream-leaky and bounded.
Removal blocks and unlinks the live tee branch through an owner-context IDLE
probe before setting it to `NULL`; telemetry messages are accepted only from
the current active branch. This permits a later lease to attach and flow again.

### No-build accessible browser

Use static semantic HTML, CSS, and JavaScript with native forms and controls,
text-node/DOM output encoding, keyboard access, visible focus, non-color status
labels, live regions, and reduced-motion behavior. Do not introduce Node/npm,
a framework, a template engine, binary assets, or server-rendered telemetry.

## Implemented bounds

| Boundary | Bound |
|---|---:|
| HTTP clients / accept backlog | 16 / 32 |
| HTTP socket timeout / JSON body | 10 seconds / 32 KiB |
| SSE clients / full snapshot cadence | 4 / approximately 5 Hz (0.2 seconds) |
| Lightweight monitoring cadence | configured 5-60 Hz; capped at 60 Hz |
| Spectrum lease / renewal | 5 seconds / 2 seconds while clients exist |
| Concurrent scrypt checks | 2 |
| Sessions / idle / absolute expiry | 128 / 15 minutes / 24 hours |
| Login attempts per pair / IP / process | 5 / 5 / 20 per 60 seconds |
| Login pair keys | 1024 |
| AF_UNIX clients / timeout / message | 8 / 15 seconds / 64 KiB |
| Retained control diagnostic events | 32 |

## Evidence boundary

Host tests cover credential parsing/hashing, dummy-user work, throttling,
session expiry/capacity, cookie flags, CSRF, HTTP smuggling/framing defenses,
redaction, local protocol validation, client bounds, config revisions and
atomicity, live/restart classification and rollback, non-Cartesian capability
fixtures, SSE numeric payloads, shared spectrum leasing/expiry, disconnect
churn, and static-asset syntax/contracts. POSIX socket lifecycle tests require
Linux and do not become Raspberry Pi evidence when skipped on a Windows
development host.

The retained physical Step 3 scenario killed/restarted only the web process
during active capture/logical-stream/recording state, observed unchanged media
generations and sender byte progress during that strict connected slice,
finalized/decoded FLAC, exercised bounded slow clients and session restart, and
passed three spectrum release/expiry reattachment cycles. The live workstation
FFplay output was not retained, so it is not receiver-decode evidence. Device
loss, SRT retry-wait, storage hard-stop displays, and browser QA used controlled
simulation. Exact commands, immutable-artifact reassessment, hashes, and
nonclaims are in [the physical report](../evidence/RPI_ZERO_W_STEP3_2026-08-19.md)
and [operator document](../WEB_CONTROL.md).

This ADR does not alter the Step 1/2 media evidence. PCM, FLAC, and Opus remain
in streamable Matroska. MPEG-TS/SMPTE ST 302M was not admitted, and Step 3 did
not install or depend on `gstreamer1.0-libav`. The external workstation ingest,
encryption, broad receiver interoperability, endurance, and Zero W performance
headroom remain unverified.

## Consequences

- Web authentication, HTTP load, and browser failures cannot directly own or
  back-pressure the GStreamer graph.
- A web crash can leave spectrum attached for at most its five-second lease;
  expiry removes only that disposable branch while primary media continues.
- The media owner remains the sole authority for state transitions,
  capabilities, config persistence, and secret handling.
- Standard-library implementation avoids a web framework, Node/npm, native
  Python dependency, and another ARMv6 packaging burden.
- Bounded sessions, password work, HTTP clients, socket clients, messages,
  telemetry clients, and diagnostics make resource use finite, but do not prove
  endurance or acceptable Zero W headroom.
- In-memory sessions favor simple restart invalidation over high availability.
- Filesystem socket authorization is sufficient for the manual Step 3 boundary;
  least-privilege identities, service policy, and boot lifecycle remain Step 4.
- An authenticated UI can save a restart-required configuration but cannot
  activate it until an external service manager restarts the media process.

## Alternatives rejected

- A combined web/media process violates failure isolation.
- Direct browser access to the media socket bypasses authentication and is not
  possible with AF_UNIX.
- HTTP or TCP for local IPC expands the listening/network attack surface without
  a Step 3 need.
- WebSockets add bidirectional state and queue complexity where one-way latest-
  only SSE is sufficient.
- A frontend framework/build pipeline adds dependencies and memory/packaging
  work without improving the required controls.
- Server-rendered meter or spectrogram images add encoding and buffering to the
  constrained device; bounded numbers plus browser canvas keep visualization
  disposable.
- Auto-restarting the media process from the web layer would cross the Step 3
  failure and privilege boundary and pre-empt Step 4 service management.
