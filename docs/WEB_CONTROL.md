# TinyPiRelay web and local control plane

## Onboarding update — 0.5.17-dev

Fresh installations open username/password creation in the existing login card,
then require normal sign-in. No claim code is used. Only a private fresh-install
marker enables registration; absent/corrupt established credentials fail closed.
Fresh LAN access is IPv4 HTTP port 80 with peer/Host/Origin checks, not TLS.
Legacy upgrades preserve loopback access. See [installation](INSTALLATION.md)
for the current operator flow; manual developer commands below remain supported.

The production web client's newer desktop dashboard integration is documented
in [DASHBOARD_INTEGRATION.md](DASHBOARD_INTEGRATION.md). The Step 3 evidence below
describes the original client; it is not hardware validation of the new layout.

Status: Step 3 is implemented with host, simulated-POSIX, and finite physical
Raspberry Pi evidence. Claims remain limited to the exact rows in
[the Step 3 report](evidence/RPI_ZERO_W_STEP3_2026-08-19.md).

The product requirements are in [PROJECT_SPEC.md](PROJECT_SPEC.md). Media-owner
behavior remains documented in [MEDIA_SERVICE.md](MEDIA_SERVICE.md), and the
process-boundary decision is recorded in
[ADR 0003](adr/0003-web-control-plane.md).

## Scope and failure domains

### Interaction stability (0.5.14-dev)

Live telemetry must not rebuild or restyle controls just because another sample
arrived. Shared change-only property/attribute setters apply each control's final
availability once. Text fitting is cached by content/layout and deferred for the
focused or pressed control; native selects fit their option set rather than
changing font with each selection. Storage rows are keyed by version-bound file
IDs, with stable action and pagination nodes and immediate real permission changes.

Temporary media outages disable commands without discarding the editable form,
capability choices or its original expected revision. An explicit page reload,
save/conflict reload, login or maintenance refresh may replace those values;
background snapshots do not. Server validation and revision conflicts remain
authoritative. A dynamic Start/Stop button rejects a press if its action changed
between pointer/key activation and click.

Future controls should use these same leaf-update helpers and stable nodes; do
not put form hydration, option replacement or unconditional control styling in a
telemetry callback. The local `spikes/interaction_browser_check.cjs` regression
exercises all ten pages in real Firefox/Chrome without any Pi writes.

### Expanded desktop pages (0.5.13-dev)

Storage now lists six original FLAC recordings per page and exposes authenticated
native-browser playback/byte-range seeking, download, confirmed deletion and a
single background decoder check. Only the configured recording filesystem is
addressable; Linux directory descriptors, no-follow opens and version-bound
opaque IDs reject traversal, symlinks, hard links and stale file selections.
Ancestor traversal uses Linux `O_PATH`, preserving the installed root-owned
`0710` parent directory; only the media-owned final directory is opened for reading.
Active files cannot be read, deleted or checked. Metadata-derived duration is
not an integrity guarantee: a finalized-looking header is distinct from a
decoder check. `flac --test` success is reported as checked; the FFmpeg and
GStreamer fallbacks are reported as decode-only because a clean EOF can miss a
truncated tail. Neither result guarantees the original recording is complete.
Since `0.5.16-dev`, confirmed recovery can re-encode salvageable audio into a
separate, validated FLAC copy using optional installed FFmpeg; the original is
never overwritten. See [FLAC recovery](FLAC_RECOVERY.md) for limits and failure behavior.

Catalog scans are capped at 4096 directory entries with an explicit error above
that limit. Only the 256 most recently listed file identities are retained.
Two HTTP file transfers may run at once, each using at most 24 KiB per media RPC;
the web process does not transcode audio or gain filesystem permissions. Native
FLAC playback depends on the browser; original download remains the fallback.
One check may run for at most five minutes, only while not recording, and is
cancelled if recording starts. It uses an already-installed decoder and reports
unavailable when none is present. No new decoder package is installed for this.

System and Network render the existing cached device telemetry. Unknown UPS,
power and network details remain unavailable rather than fabricated. Audio's
palette, meter appearance and frequency view preferences are validated and
remembered in this browser; they do not change capture or recording fidelity.
Logs filter/export the bounded redacted in-memory event window, not the entire
system journal. About reports the actual software/device/capability data.

New HTTP routes (all except login/static assets require a session):

| Method | Route | Contract |
|---|---|---|
| GET | `/api/storage?page=1` | Six-row catalog and running/most-recent check or recovery state |
| GET/HEAD | `/api/storage/file?id=ID` | Original FLAC; one HTTP byte range; `&download=1` attachment |
| POST | `/api/storage/action` | `action` = `delete`, `check` or `recover`, plus `file_id`; CSRF required |
| GET | `/api/maintenance` | Real availability, busy/phase, operation and boot IDs |
| POST | `/api/maintenance` | Allowlisted action, `current_password`, `confirm:true`; CSRF required |
| POST | `/api/account/password` | Current/new/confirmation passwords; CSRF; revokes all sessions |

Maintenance is a separate narrow privilege boundary described in the
[ADR 0004 amendment](adr/0004-installer-system-integration.md). It is not the
legacy report-only `service.restart_request` media operation. Restart applies
saved configuration and stops the current recording; recording is not silently
resumed. Reboot/shutdown require verified finalization and clean service exit.

Step 3 adds a plain HTML/CSS/JavaScript application and an authenticated Python
HTTP process. The web process does not import, own, or run GStreamer. It sends
one bounded request at a time to the independent media process through a
permission-restricted Unix-domain socket.

```text
browser -- HTTP/SSE --> web_service.py -- AF_UNIX JSON --> media_service.py
   |                       |                                  |
   | renders numeric       | authentication, validation,     | authoritative
   | telemetry locally     | CSRF, redaction, client bounds   | media state and
   |                       |                                  | GStreamer graph
   `-- may disconnect -----'                                  `-- keeps running
```

A browser disconnect or a stopped/restarted web process does not issue a
capture, stream, or recording stop. The disposable spectrum analyzer is the
one demand-owned exception: the first SSE client renews a fixed five-second
media-side lease, the web process renews it every two seconds while any SSE
client remains, and only the last orderly disconnect releases it. If the web
process is killed, lease expiry removes only the spectrum branch; level
metering and all primary media branches continue. Web sessions are
intentionally in memory, so a web restart invalidates them and requires a new
login while the media process continues. If the media socket is absent, status
is reported as unavailable (`503`) and SSE emits an unavailable snapshot; the
authenticated application shell can remain open.

`service.restart_request` is only a Step 3 contract placeholder. It returns
`accepted: false` because Step 4, not the web process, will own service-manager
integration. Step 3 does not install systemd units or run either process as a
daemon.

## Components

| Component | Responsibility |
|---|---|
| [`web_service.py`](../src/tinypirelay/web_service.py) | Bounded HTTP server, explicit routes, JSON framing, authentication enforcement, CSRF checks, SSE, static assets, and module CLI |
| [`web_security.py`](../src/tinypirelay/web_security.py) | Versioned scrypt credentials, constant-work unknown-user checks, login throttling, bounded server-side sessions, and cookies |
| [`control_protocol.py`](../src/tinypirelay/control_protocol.py) | Versioned newline-delimited JSON over AF_UNIX, timeouts, message/client bounds, operation allowlist, and response redaction |
| [`media_control.py`](../src/tinypirelay/media_control.py) | Public media operations, redacted configuration DTO, revision conflicts, atomic writes, live/restart classification, and bounded diagnostics |
| [`audio_capabilities.py`](../src/tinypirelay/audio_capabilities.py) | Strict non-Cartesian device/mode/representation capability validation |
| [`web_assets`](../src/tinypirelay/web_assets) | No-build desktop/large-tablet UI with dashboard, configuration, recording, diagnostics, meters, and browser-rendered spectrogram |

## Manual repository entry points

These are repository module commands, not installed commands or system
services. The checked-in [`config/example.json`](../config/example.json) is
disabled; use a private configuration containing one validated capture mode.

On a Unix-like host, create a private runtime directory. The media entry point
uses mode `0750` for its control-socket parent and `0660` for the socket. An
existing parent must already have the requested mode; the server does not
weaken or rewrite an existing directory's permissions.

```sh
export PYTHONPATH=src
TPR_RUN="$(mktemp -d /tmp/tinypirelay-step3.XXXXXX)"
chmod 0750 "$TPR_RUN"
TPR_SOCKET="$TPR_RUN/control.sock"
TPR_CREDENTIAL="$TPR_RUN/web-credential.json"
TPR_CONFIG="/replace/with/private-config.json"
chmod 0600 "$TPR_CONFIG"
```

The media owner opens the configuration without following symlinks and accepts
only a bounded regular file. On POSIX, a configuration containing an SRT
passphrase is rejected when group or other permission bits are set. Keeping
every configuration at mode `0600` avoids a mode-dependent deployment trap and
protects future secret additions.

SRT passphrase input is conservatively restricted to 10–79 printable ASCII
characters. This keeps the form, JSON schema, runtime validator, and libsrt's
byte-oriented limit aligned; it is not a claim that encrypted SRT has been
interoperability-tested.

Run this variable block in each terminal, or replace the variables below with
the same literal paths.

Create the first credential interactively. The password is read with
`getpass`; it is not accepted as a command-line argument.

```sh
python3 -B -m tinypirelay.web_service passwd \
  --credentials "$TPR_CREDENTIAL" \
  --username operator
```

Start the media owner in one terminal:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.media_service \
  --config "$TPR_CONFIG" \
  --evidence evidence/stream_compatibility.json \
  --audio-capabilities evidence/audio_capabilities.json \
  --control-socket "$TPR_SOCKET"
```

Start the web process in another terminal:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.web_service serve \
  --credentials "$TPR_CREDENTIAL" \
  --control-socket "$TPR_SOCKET" \
  --bind 127.0.0.1 \
  --port 8080
```

The default loopback bind is deliberate. For a remote browser, prefer an SSH
tunnel while Step 4 deployment and TLS are absent:

```sh
ssh -L 8080:127.0.0.1:8080 tinypirelay@192.0.2.10
```

Then open `http://127.0.0.1:8080/` on the workstation. Binding to a LAN address
is an explicit operator choice for a trusted network; this repository provides
no Internet-exposure or TLS claim. Use `--secure-cookie` only when the browser
actually reaches the service through HTTPS.

Run the same `passwd` module command without `--username` to retain the username
from an existing credential record and replace its password. The eventual
installed `sudo tinypirelay passwd` wrapper belongs to Step 4.

## Browser-facing HTTP API

All responses use `Cache-Control: no-store` and restrictive CSP, framing,
referrer, permissions, and anti-sniffing headers. Request JSON is strict:
duplicate keys, non-finite numbers, malformed framing, unsupported transfer
encoding, wrong content type, and bodies over 32 KiB are rejected. Pre-read
framing failures close the connection so unread bytes cannot be interpreted as
a second request.

| Method and route | Authentication | CSRF | Contract |
|---|---|---|---|
| `GET /`, `/index.html`, `/app.css`, `/app.js` | no | no | Serve only three allowlisted files across these four routes; no arbitrary filesystem paths |
| `POST /api/login` | no | no | Exact JSON `{username, password}`; creates a server-side session |
| `GET /api/session` | optional | no | Returns `authenticated`; an authenticated result also supplies the current CSRF token |
| `POST /api/logout` | yes | yes | Empty body; deletes the server session and expires both cookies |
| `GET /api/status` | yes | no | Redacted media state, revisions, restart flag, diagnostics, and latest telemetry; returns an explicit unavailable body on media `503` |
| `GET /api/capabilities` | yes | no | Exact device/mode/stream-option graph plus representation metadata |
| `GET /api/config` | yes | no | Editable redacted config, current saved revision, and restart flag |
| `PUT /api/config` | yes | yes | Exact JSON `{config, confirm_restart, expected_revision}` |
| `POST /api/control` | yes | yes | Exact action object; bitrate is the only action accepting an integer `value` |
| `GET /api/events` | yes | no | SSE full `snapshot` events approximately five times per second, with lightweight latest-only `telemetry` events at the configured 5-60 Hz monitoring cadence |

Control actions exposed by `POST /api/control` are `capture.start`,
`capture.stop`, `stream.start`, `stream.stop`,
`stream.set_opus_bitrate`, `recording.start`, `recording.stop`, and
`service.restart_request`. Unknown actions and extra fields fail closed.

Authentication/configuration conflicts use stable codes. Examples include
`authentication_required`, `csrf_rejected`, `rate_limited`,
`revision_conflict`, `confirmation_required`, `recording_active`,
`reconfigure_required`, and `media_unavailable`. The browser should use the
code, not parse the human-readable message.

### Session and credential behavior

- There are no default credentials. A credential file stores a 16-byte random
  salt and a 32-byte `hashlib.scrypt` result (`N=16384`, `r=8`, `p=1`), never a
  password. POSIX writes use mode `0600`, file fsync, atomic replacement, and
  parent-directory fsync. A failure after atomic replacement reports that the
  new credential is committed but directory durability is unconfirmed; it
  does not claim the old credential was restored.
- Unknown usernames still perform scrypt work. At most two password checks run
  concurrently in one web process.
- Login attempts use one 60-second fixed window at three nested bounds: five
  attempts per canonical IP/username pair, five across all usernames from one
  canonical IP, and 20 across the web process. The pair table is bounded at
  1024 keys; capacity exhaustion fails closed.
- At most 128 in-memory sessions exist. The default idle timeout is 15 minutes
  and the absolute timeout is 24 hours; oldest sessions are evicted at
  capacity. Only SHA-256 token digests are stored server-side.
- The bearer cookie is `HttpOnly`, `SameSite=Strict`, and path `/`. The separate
  CSRF token must match the `X-CSRF-Token` header for every mutation. `Secure`
  is added only with `--secure-cookie`.
- Logout and web-process restart invalidate sessions. Session and CSRF values,
  password hashes, passwords, and SRT passphrases are excluded from logs and
  media responses.

The login throttle is one in-process defense, not a perimeter firewall or a
distributed rate limiter. Internet exposure remains out of scope.

## Local media control protocol

The browser cannot access the Unix socket. Each web-to-media operation opens
one AF_UNIX connection, writes one UTF-8 JSON object followed by a newline,
shuts down its write side, reads one newline-terminated response, and closes.
Both directions carry `protocol_version: 1` and the same bounded `request_id`.

The protocol limit is 64 KiB including the newline, its default I/O timeout is
15 seconds, and the media listener admits at most eight concurrent clients.
JSON duplicate keys, non-finite numbers, unknown envelope keys, unknown
operations, extra trailing data, oversized input, and mismatched response IDs
are rejected. Operation failures are distinct from socket unavailability and
malformed responses. Access control is currently the socket's filesystem mode;
dedicated users/groups and service ownership are Step 4 work.

The allowlist is:

```text
status                         capabilities
config.get                     config.validate
config.update                  capture.start / capture.stop
stream.start / stream.stop     stream.set_opus_bitrate
recording.start / recording.stop
monitoring.telemetry
monitoring.spectrum_lease / monitoring.spectrum_release
service.restart_request
```

The three `monitoring.*` operations are internal web-to-media operations, not
browser control actions. `monitoring.telemetry` is a read-only empty-argument
request for only the newest meter and spectrum samples. The lease operations
accept no caller-selected duration: the media owner fixes the lease at five
seconds and owns its expiry timer.

The media process owns status, validation, state serialization, and
configuration persistence. The web process performs another redaction pass
before encoding a browser response. A passphrase is represented publicly only
as `{configured: boolean, action: "keep"}`; replacement values may enter a
mutation but never return over the socket or HTTP.

## Capability and configuration rules

[`audio_capabilities.json`](../evidence/audio_capabilities.json) is an exact
graph, not independent lists of formats, rates, channel counts, and codecs:

```text
device -> exact capture mode -> exact stream option -> representation metadata
```

The validator requires the device, selected mode, stream option, and
representation to use an accepted evidence status (`Hardware tested` or
`CI validated`). Representation IDs must exactly match the separately
evidence-gated compatibility record. The UI therefore changes its stream list
when the capture mode changes, labels native and converted paths, and cannot
construct a Cartesian product. For the current iMM-6C record, native mono
S16LE/48 kHz has the three retained Matroska stream options with explicit
mono-to-stereo conversion. Native mono S24LE/48 kHz admits only the physically
tested FLAC option: conversion to 16-bit stereo before encoding, while local
recording retains 24-bit mono. PCM and Opus from S24 remain unvalidated.

Configuration reads return a SHA-256 revision of the exact saved bytes. An
update must present that value as `expected_revision`; a stale browser receives
`revision_conflict` before writing. Updates are serialized and use a same-
directory temporary file, mode `0600`, file fsync, atomic replacement, and
parent-directory fsync. If replacement succeeds but directory durability
cannot be confirmed, the in-memory saved revision is reconciled and the API
reports that restart/durability is unresolved instead of pretending the old
file remains authoritative.

### Live application versus restart

The implemented live subset is intentionally narrow:

| Change | Implemented behavior |
|---|---|
| Recording directory, rotation, or required mount | Applied without restarting capture or stream, but only while recording is stopped; otherwise the save is rejected |
| Supported Opus bitrate preset (64/96/128/192 kbit/s) | Applied live when the stream is stable; an active Opus encoder is updated through the existing property/readback path without replacing the stream branch |
| Capture device or exact mode | Requires explicit confirmation and a later media-process restart |
| Stream enabled flag, representation, destination host/port, latency, stream ID, or passphrase | Requires explicit confirmation and a later media-process restart |
| Monitoring cadence or band count | Requires explicit confirmation and a later media-process restart |

After a restart-required configuration is saved, status carries distinct
active and saved revisions plus `restart_required: true`. New capture, stream,
recording, and bitrate-start mutations are rejected with
`reconfigure_required`; stop operations remain available for safe shutdown.
Step 3 does not perform the restart. Starting the media module again loads the
saved file and makes it active.

A direct bitrate command and a configuration bitrate update cannot overlap. If
a direct change is pending, the live configuration path fails before any
backend mutation. Backend configuration/bitrate application is transactional:
a failure or mismatched bitrate readback rolls the backend back before the
runtime revision/state changes. Because the control owner durably saves before
asking the runtime to apply live, a live-apply failure is explicitly reported
as saved-but-not-active with restart required; it is not reported as an
unsaved change.

## Telemetry, UI, and resource bounds

The UI is static semantic HTML with no template interpolation, third-party
script, framework, npm dependency, or frontend build step. It uses text nodes
and DOM construction rather than `innerHTML`, keyboard-operable native
controls, visible focus, live status regions, non-color status text, and a
reduced-motion stylesheet.

SSE sends a current state snapshot approximately five times per second and
latest-only finite numeric meter/spectrum events between snapshots at the
configured 5-60 Hz monitoring cadence. The browser draws per-channel
peak/RMS/clipping/no-signal meters and a scrolling spectrogram on canvas; the
server does not render or queue images. In `0.5.9-dev`, rendering and frequency
gestures share an animation-frame gate. The time axis is a uniform 30 seconds,
with at most 1801 raw browser-arrival samples and no time warp or age-dependent
peak aggregation. One preceding sample can be retained within that bound when
it covers the leftmost displayed interval. A cached Canvas 2D bitmap moves left
by integer device pixels; only the newly exposed strips are colored. Zoom/resize
replays the bounded raw history, rather than repeatedly shrinking an old image.

The display holds each measured color to cover short delivery/paint delays:
the hold limit is the larger of 250 ms and two configured sample intervals;
longer interruptions remain blank. Browser stalls advance by real elapsed time,
not a capped animation delta. This is arrival-time visualization, not a
sample-accurate recording clock. The default high-detail logarithmic frequency
map gives 4-24 kHz about 37% of the plot height in the 20-24 kHz view at 48 kHz.
The browser caps displayed meter channels at eight and spectrum values at 2048.

The Audio page's low/high frequency bounds and the dashboard's frequency-axis
wheel zoom, drag pan, and double-click reset are display-only. Full range also
resets the view. Bounds must be finite, at least 20 Hz, at least 10 Hz apart, and
no higher than the known active capture Nyquist frequency; unavailable or
restart-pending format information disables these controls. Changes update this
tab only and issue no configuration or media-control request. Inputs are kept
outside the capture form's validation/submission, and Ctrl/Meta-wheel remains
available for browser zoom. Changing the displayed range does not improve FFT
resolution or alter recording/streaming audio.

| Bound | Value |
|---|---:|
| Concurrent HTTP clients | 16 |
| HTTP accept backlog | 32 |
| Per-connection socket timeout | 10 seconds |
| Concurrent SSE clients | 4 |
| Full SSE state snapshot cadence | approximately 5 Hz (0.2-second interval) |
| Lightweight meter/spectrum cadence | configured 5-60 Hz; capped at 60 Hz |
| Browser spectrogram history | linear 30 seconds; at most 1801 raw arrival-time samples |
| Spectrum lease / renewal | 5 seconds / every 2 seconds while SSE clients exist |
| Concurrent password derivations | 2 |
| Request JSON | 32 KiB |
| Sessions | 128 |
| Login throttle | 5 per pair / 5 per IP / 20 process-global per 60 seconds |
| Login throttle pair keys | 1024 |
| Media control clients | 8 |
| Media control message | 64 KiB |
| Media control I/O timeout | 15 seconds |
| Retained control diagnostics | 32 events |

Each SSE client holds one bounded HTTP slot and receives only the newest media
snapshot. A slow or disconnected client has no media-buffer queue and releases
its slots on write error or timeout. Level metering remains attached whenever
capture runs. Spectrum is not attached at capture startup: the first active SSE
client leases it, additional clients share that one analyzer, and last release
or media-owned lease expiry removes only that downstream-leaky branch. No
per-client media branch or telemetry history exists. A finite Zero W run kept
two unread clients open for 12 seconds, rejected overload, recovered after
churn, and stayed within explicit RSS/FD bounds. It did not measure CPU cost,
endurance, or sustained five-Hz browser performance.

## Reproducible validation

### Development host

From the repository root on Windows PowerShell:

```powershell
$env:PYTHONPATH = 'src'
py -3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

The suite exercises security, HTTP framing, redaction, revision conflicts,
non-Cartesian capabilities, UI assets, bounded SSE, local protocol validation,
media-control mapping, and prior media behavior. POSIX socket lifecycle checks
are expected to skip on Windows; a host pass is not Raspberry Pi hardware
evidence.

On Linux, use:

```sh
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v
```

### Raspberry Pi scenario and retained evidence

The exact 2026-08-19 setup, resolved physical harness command, immutable JSON
hashes, current reassessment command, simulator/physical classification, and
limitations are recorded in
[the Step 3 physical report](evidence/RPI_ZERO_W_STEP3_2026-08-19.md). The
generic procedure below remains useful for a new board or final-tree rerun;
replace every uppercase placeholder and retain stdout/stderr plus exact source
identity. These commands do not install packages or services.

```sh
mkdir -p "<STEP3_EVIDENCE_DIRECTORY>"
PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v \
  > "<STEP3_EVIDENCE_DIRECTORY>/unit-tests.log" 2>&1
sha256sum "<STEP3_EVIDENCE_DIRECTORY>/unit-tests.log"
```

Use the manual entry points above with a private `<CONFIG_PATH>`, then start the
web process with its PID captured:

```sh
PYTHONPATH=src python3 -B -m tinypirelay.web_service serve \
  --credentials "<CREDENTIAL_PATH>" \
  --control-socket "<CONTROL_SOCKET>" \
  --bind 127.0.0.1 --port 8080 \
  > "<STEP3_EVIDENCE_DIRECTORY>/web-first.log" 2>&1 &
WEB_PID=$!
```

Through an SSH tunnel, log in and start a validated stream and recording. Save
the authoritative pre-crash state directly from the media socket:

```sh
PYTHONPATH=src python3 -B -c \
  'import json,sys; from tinypirelay.control_protocol import ControlClient; print(json.dumps(ControlClient(sys.argv[1]).request("status"), sort_keys=True))' \
  "<CONTROL_SOCKET>" \
  > "<STEP3_EVIDENCE_DIRECTORY>/media-before-web-kill.json"
```

Verify that `WEB_PID` is the command just started before terminating only that
process. `SIGKILL` is intentional here because the required scenario is web
failure, not orderly web shutdown:

```sh
ps -p "$WEB_PID" -o pid=,args=
kill -KILL "$WEB_PID"
wait "$WEB_PID" || true
sleep 6
PYTHONPATH=src python3 -B -c \
  'import json,sys; from tinypirelay.control_protocol import ControlClient; print(json.dumps(ControlClient(sys.argv[1]).request("status"), sort_keys=True))' \
  "<CONTROL_SOCKET>" \
  > "<STEP3_EVIDENCE_DIRECTORY>/media-after-web-kill.json"
```

A reproduction passes only if the same media process remains alive, capture and
the requested stream/recording remain active, receiver/recording progress
continues, the post-kill status has no retained spectrum sample after the
five-second lease expires, level metering continues, and restarting the web
command permits a fresh login while the old session is invalid. Also exercise
five failed logins followed by a rate-limited attempt, logout/session
invalidation, CSRF rejection, an impossible capability combination, a slow
fifth SSE client, repeated SSE connect/disconnect churn, media-socket absence,
SRT reconnecting, device-loss state, and storage hard-stop state. Record which
cases used physical faults and which used controlled software injection; do
not relabel injections as hardware tests.

Hash the retained artifacts only after the scenario is complete:

```sh
sha256sum \
  "<STEP3_EVIDENCE_DIRECTORY>/unit-tests.log" \
  "<STEP3_EVIDENCE_DIRECTORY>/web-first.log" \
  "<STEP3_EVIDENCE_DIRECTORY>/media-before-web-kill.json" \
  "<STEP3_EVIDENCE_DIRECTORY>/media-after-web-kill.json" \
  > "<STEP3_EVIDENCE_DIRECTORY>/SHA256SUMS"
```

The retained physical run passed the current 7/7 assessor, including strict
web-`SIGKILL` isolation, bounded slow SSE clients, authentication/security,
FLAC finalization, and explicit control-socket unavailable/recovery behavior.
Its immutable JSON embeds an obsolete one-check failure from an earlier
predicate that coupled the temporary proxy test to SRT connection state; the
current assessor and rationale are retained in the physical report rather than
rewriting the artifact. A separate focused physical artifact passed three
spectrum attach/remove cycles (release, expiry, release).

Device-loss, SRT retry-wait, and storage-hard-stop display states were
controlled simulator injections, not physical faults. Browser QA used a
simulated POSIX container, not the Pi; mobile layouts, unavailable recovery,
contrast/focus, reduced motion, and console cleanliness passed, but a complete
automated Tab-order traversal was not recorded.

## Exact nonclaims and deferred work

- Step 3 does not create users/groups, systemd units, an installer, an installed
  CLI, log rotation, boot/reboot recovery, an upgrade path, or an uninstall
  path. Those are Step 4 concerns.
- Step 3 does not terminate TLS, configure a reverse proxy, manage networking,
  or authorize Internet exposure.
- Runtime device discovery/hotplug promotion is not added. The UI consumes the
  strict retained capability graph; adding a device requires new validated
  evidence and a capability-file update.
- Session persistence across web restart is deliberately absent. Distributed
  authentication throttling and external identity providers are not provided.
- Demand-driven spectrum release/expiry/reattachment and finite slow-client
  process bounds have Zero W evidence. CPU savings and five-Hz endurance remain
  unverified.
- Step 3 adds no new codec, framing, SRT receiver, encryption, or hardware
  compatibility claim. The workstation ingest at `192.0.2.30:4001` remains
  outside the admitted compatibility evidence.
- The approved sender representations remain PCM, FLAC, and Opus in streamable
  Matroska. MPEG-TS and SMPTE ST 302M were **not** added or substituted.
- No Step 3 command installed `gstreamer1.0-libav` (and repository state does
  not assert whether an operator installed it independently). It is not a Step
  3 dependency or evidence basis.
