# Step 5 physical hardware profiling

Status: procedure only; no Step 5 physical result is claimed here.

2026-09-11: the operator deferred further endurance runs and authorized Step 6
review. This retained procedure does not schedule or authorize another run;
its missing rows remain unverified. Later finite Pi 4 evidence and the current
review decision are linked in [RELEASE_READINESS.md](RELEASE_READINESS.md).

This procedure turns a bounded observation on an installed TinyPiRelay appliance
into reviewable physical evidence. It applies the requirements in the
[project specification](PROJECT_SPEC.md), the current limitations in the
[project state](PROJECT_STATE.md), and the
[Step 5 milestone](../codex/STEP_5_HARDWARE_PROFILING.md). The Raspberry Pi Zero
W Rev 1.1 is the design-floor board and must be run first.

The [Pi profiler](../spikes/step5_hardware_profile.py) and
[workstation receiver](../spikes/step5_ffmpeg_receiver.py) are evidence tools,
not production components. A Pi profiler `execution_status` of `pass` proves
only that its bounded collection and local invariants passed. It is not a
hardware-compatibility claim, a receiver-decode result, a fault-test result, a
soak result, or a Legacy/Standard/Full profile decision.

## Claim gate

A scenario may be called **Hardware tested** only when one final manifest binds
all of the following to the same scenario key and immutable source-bundle
SHA-256:

1. the exact source archive and archive manifest;
2. installed diagnostics and install-state identity from the Pi;
3. one complete Pi profiler JSON report;
4. one passing workstation decoded-receiver JSON report for every streaming
   row;
5. the manual hardware/environment metadata listed below; and
6. any row-specific recording, fault, or recovery evidence.

Every file is hashed after collection. The Pi report's
`identity.source_bundle_sha256`, the receiver report's
`source_bundle_sha256`, and the manifest's `source_bundle_sha256` must be the
same lowercase 64-hex value. The Pi report's `run.scenario`, the receiver
report's `scenario`, and the manifest's `scenario` must be the same scenario
key. A missing, null, malformed, or mismatched identity fails the row.

The repository currently has no commit. Until that changes, record the tested
commit as `null` with reason `unborn repository; source bundle SHA-256 is the
primary identity`. Never substitute a working-tree timestamp for a commit.

## Required manual metadata

Store this metadata as UTF-8 JSON beside the machine reports. Values must be
observed for the run, not copied from a product page or an older report.

| Section | Required fields |
|---|---|
| Run | scenario key, UTC start/end, operator, approvals, source-bundle SHA-256, commit or null reason, installed source fingerprint, installed config revision |
| Board | manufacturer, exact model string, revision, SoC/CPU, RAM, serial redacted for publication |
| OS | `/etc/os-release`, kernel, `uname -m`, userland bitness, dpkg architecture, Python version |
| Power | supply make/model, rated voltage/current, cable, powered peripherals/hub, UPS if any, `vcgencmd get_throttled` before and after |
| Storage | boot/recording device make/model, capacity, filesystem, mount options, free/used bytes, whether recording uses the same device |
| Audio | microphone/interface make/model, USB or I2S identity, ALSA card/device, negotiated capture mode, cable/adapter |
| Network | Pi interface and driver, wired/Wi-Fi and band, route to receiver, measured RSSI when available, access-point/router model, Pi address, receiver address |
| GStreamer | core version and installed versions of `alsasrc`, `audioconvert`, `audioresample`, `capsfilter`, `tee`, `queue`, `level`, `spectrum`, `opusenc`, `flacenc`, `matroskamux`, and `srtsink` |
| Receiver | workstation hardware/OS, address, NIC/link, FFmpeg executable, version/configuration, libsrt status, listening port |
| Scenario | representation ID, codec/container, SRT latency, Opus bitrate, recording rotation, spectrum bands/rate, browser-client schedule, fault schedule |

For the example workstation, replace documentation address `192.0.2.30` with the receiver address only
after the route check confirms that address for the run. Do not store web
passwords, SRT passphrases, stream IDs, Wi-Fi credentials, credential hashes, or
other secrets. A public copy may redact serial numbers, MAC addresses, SSIDs,
and private topology, but the redactions must be declared.

## Safety and authority boundary

The normal profiler is intentionally conservative. It may start a configured
stream or recording only when the operator passes the corresponding flag and
the scope is stopped. It never starts capture, changes configuration, injects
faults, or manages services. It stops only scopes whose generation it started,
restores the bitrate of a pre-existing Opus stream, and lets a shared spectrum
lease expire rather than releasing another client's lease.

The following require explicit operator approval for each run:

- saving or restoring TinyPiRelay configuration;
- manually starting or stopping a pre-existing stream;
- starting a recording that writes to persistent storage;
- unplugging audio, interrupting networking, killing a service, filling a
  dedicated test filesystem, or rebooting the Pi; and
- retaining or publishing hardware/network metadata.

Never fill the root filesystem, change the distribution, install packages,
alter routing on the SSH management path without out-of-band recovery, or run
two fault rows at once. Keep power-cycle and destructive storage tests out of
this procedure.

## Source and installed identity

Create one immutable source archive while the tree is quiescent. Exclude only
`.git`, `__pycache__`, and `*.pyc`; record the archive hash, byte count, and
sorted member list. Do not reuse an archive after any source, test, profiler,
receiver, configuration template, or procedure change.

On the workstation, place the evidence directory outside the source tree and
use this exact archive recipe:

```powershell
$Workspace = (Resolve-Path 'D:\UserFiles\Documents\ChatGPT\TinyPiRelay').Path
$EvidenceRoot = '<ABSOLUTE_EVIDENCE_DIRECTORY_OUTSIDE_WORKSPACE>'
$Bundle = Join-Path $EvidenceRoot 'tinypirelay-step5-source.tar.gz'
$Members = Join-Path $EvidenceRoot 'tinypirelay-step5-source-members.txt'

tar -czf $Bundle `
  --exclude='.git' `
  --exclude='*/.git/*' `
  --exclude='__pycache__' `
  --exclude='*/__pycache__/*' `
  --exclude='*.pyc' `
  -C $Workspace .
tar -tf $Bundle | Sort-Object | Set-Content -Encoding utf8 $Members
Get-Item -LiteralPath $Bundle | Select-Object FullName, Length
Get-FileHash -Algorithm SHA256 -LiteralPath $Bundle, $Members
```

### One-time Step 5 upgrade gate

The installed Step 4 appliance is `0.4.0-dev`; its stream is disabled and its
representation and destination are null. The Step 5 bundle is `0.5.0-dev` and
adds the queue observations required by the profiler. Before the first matrix
row, obtain explicit approval for this upgrade and for retaining its bounded
identity evidence. Stop if the installed version or starting configuration
differs from that stated baseline.

Transfer the frozen archive to the Pi without unpacking it. Then use a new,
root-owned staging directory named by the archive hash. This prevents the
root-run profiler from importing mutable operator-owned Python. Do not reuse or
delete an existing staging directory; investigate it and choose a fresh
approved run instead.

```sh
set -eu
TRANSFERRED_BUNDLE="<ABSOLUTE_TRANSFERRED_SOURCE_ARCHIVE>"
SOURCE_BUNDLE_SHA256="<64_HEX_SOURCE_BUNDLE_SHA256>"
SOURCE_ROOT="/var/tmp/tinypirelay-step5-source-$SOURCE_BUNDLE_SHA256"
UPGRADE_EVIDENCE_DIR="<ABSOLUTE_UPGRADE_EVIDENCE_DIRECTORY>"
/usr/bin/mkdir -p "$UPGRADE_EVIDENCE_DIR"

ACTUAL_SOURCE_BUNDLE_SHA256="$([ -f "$TRANSFERRED_BUNDLE" ] && \
  /usr/bin/sha256sum "$TRANSFERRED_BUNDLE" | /usr/bin/awk '{print $1}')"
/usr/bin/test "$ACTUAL_SOURCE_BUNDLE_SHA256" = "$SOURCE_BUNDLE_SHA256"
sudo /usr/bin/test ! -e "$SOURCE_ROOT"
sudo /usr/bin/install -d -o root -g root -m 0755 "$SOURCE_ROOT"
sudo /bin/tar -xzf "$TRANSFERRED_BUNDLE" -C "$SOURCE_ROOT" \
  --no-same-owner --no-same-permissions
sudo /usr/bin/chown -R root:root "$SOURCE_ROOT"
sudo /usr/bin/find "$SOURCE_ROOT" -type d -exec /bin/chmod 0755 '{}' +
sudo /usr/bin/find "$SOURCE_ROOT" -type f -exec /bin/chmod 0644 '{}' +
/usr/bin/test "$(sudo /bin/cat "$SOURCE_ROOT/VERSION")" = "0.5.0-dev"

sudo /usr/bin/tinypirelay diagnostics \
  > "$UPGRADE_EVIDENCE_DIR/diagnostics.before-upgrade.json"
sudo /bin/cat /var/lib/tinypirelay/install-state.json \
  > "$UPGRADE_EVIDENCE_DIR/install-state.before-upgrade.json"
sudo /usr/bin/sha256sum /etc/tinypirelay/media/config.json \
  > "$UPGRADE_EVIDENCE_DIR/config.before-upgrade.sha256"

cd "$SOURCE_ROOT"
sudo /bin/sh ./install.sh plan \
  > "$UPGRADE_EVIDENCE_DIR/upgrade-plan.txt"
```

Review the retained plan before applying it. It must say version
`Version: 0.5.0-dev`, `Mode: upgrade`, `Installed version: 0.4.0-dev`, and
`Preflight errors:` followed by `- None`. The upgrade must not receive
`--config`: it preserves and revalidates the installed private configuration.
With separate apply approval:

```sh
set -eu
SOURCE_ROOT="<ROOT_OWNED_STAGED_SOURCE_DIRECTORY>"
UPGRADE_EVIDENCE_DIR="<ABSOLUTE_UPGRADE_EVIDENCE_DIRECTORY>"
cd "$SOURCE_ROOT"
sudo /bin/sh ./install.sh upgrade --apply
sudo /usr/bin/tinypirelay diagnostics \
  > "$UPGRADE_EVIDENCE_DIR/diagnostics.after-upgrade.json"
sudo /bin/cat /var/lib/tinypirelay/install-state.json \
  > "$UPGRADE_EVIDENCE_DIR/install-state.after-upgrade.json"
sudo /usr/bin/sha256sum /etc/tinypirelay/media/config.json \
  > "$UPGRADE_EVIDENCE_DIR/config.after-upgrade.sha256"
```

Do not begin a row unless the after-state is complete at `0.5.0-dev`, its
installed payload fingerprint is retained, both services and capture are
healthy, the media status contains a bounded `runtime_metrics.queues.level`
object, and the before/after configuration hashes are identical. The source
archive hash and installed payload fingerprint remain different digest domains;
retain both. The upgrade is installation evidence only, not a Step 5 hardware
profile result.

Before every scenario, capture these installed snapshots on the Pi:

```sh
set -eu
EVIDENCE_DIR="<ABSOLUTE_EVIDENCE_DIRECTORY>"
/usr/bin/mkdir -p "$EVIDENCE_DIR"
sudo /usr/bin/tinypirelay diagnostics > "$EVIDENCE_DIR/installed-diagnostics.before.json"
sudo /bin/cat /var/lib/tinypirelay/install-state.json > "$EVIDENCE_DIR/install-state.before.json"
/bin/systemctl show tinypirelay-media.service tinypirelay-web.service \
  -p Id -p MainPID -p NRestarts -p ActiveState -p SubState \
  > "$EVIDENCE_DIR/services.before.txt"
```

Repeat the three snapshots after cleanup with `.after` filenames. A report is
not bound merely because its source archive was copied to the Pi: the final
manifest must preserve both the archive SHA-256 and the installed payload
fingerprint from `install-state.json`, since they are different digest domains.

Capture bounded read-only identity inputs separately from the manually entered
make/model and power details:

```sh
set -eu
EVIDENCE_DIR="<ABSOLUTE_EVIDENCE_DIRECTORY>"
NETWORK_INTERFACE="<ROUTED_INTERFACE>"
{
  /bin/cat /proc/device-tree/model
  /bin/grep -E '^(Model|Revision|Hardware|processor|MemTotal)' /proc/cpuinfo /proc/meminfo
  /bin/cat /etc/os-release
  /bin/uname -a
  /usr/bin/getconf LONG_BIT
  /usr/bin/dpkg --print-architecture
  /usr/bin/python3 --version
} > "$EVIDENCE_DIR/platform-metadata.txt"
{
  /usr/bin/vcgencmd get_throttled
  /usr/bin/lsblk -b -o NAME,MODEL,SIZE,TYPE,FSTYPE,MOUNTPOINTS
  /usr/bin/findmnt -o TARGET,SOURCE,FSTYPE,OPTIONS,USE%,AVAIL
  /usr/bin/arecord -l
  /usr/sbin/ip -brief address
  /usr/sbin/ip route get 192.0.2.30
  /usr/sbin/iw dev "$NETWORK_INTERFACE" link || true
} > "$EVIDENCE_DIR/hardware-metadata.txt"
{
  /usr/bin/gst-inspect-1.0 --version
  for element in alsasrc audioconvert audioresample capsfilter tee queue level \
    spectrum opusenc flacenc matroskamux srtsink; do
    /usr/bin/gst-inspect-1.0 "$element"
  done
} > "$EVIDENCE_DIR/gstreamer-metadata.txt"
```

`iw` being unavailable or a non-Wi-Fi route is recorded as not applicable; it
is not silently omitted. Hash these raw text inputs and reference them from the
manual metadata JSON.

Record `START_UTC` immediately before the measured interval and `END_UTC`
immediately after cleanup. Journal growth is external to the profiler, so
capture it explicitly:

```sh
for SERVICE in media web; do
  sudo /usr/bin/journalctl \
    --unit "tinypirelay-$SERVICE.service" \
    --since "$START_UTC" \
    --until "$END_UTC" \
    --output short-iso-precise \
    --no-pager > "$EVIDENCE_DIR/services-journal-$SERVICE.txt"
  /usr/bin/wc -c "$EVIDENCE_DIR/services-journal-$SERVICE.txt" \
    > "$EVIDENCE_DIR/services-journal-$SERVICE-bytes.txt"
done
```

Review the retained journal for accidental secrets before producing any public
copy.

### Recording-row evidence

The Step 4 appliance already contains a retained FLAC. Never delete, rename, or
overwrite it. For every recording row, inventory and hash the complete recording
directory immediately before starting the row:

```sh
set -eu
SCENARIO="<SAME_LOWERCASE_SCENARIO_KEY>"
EVIDENCE_DIR="<ABSOLUTE_EVIDENCE_DIRECTORY>"
RECORDING_DIR="/var/lib/tinypirelay/recordings"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -printf '%p\t%s\t%m\t%u\t%g\t%T@\n' | /usr/bin/sort \
  > "$EVIDENCE_DIR/$SCENARIO.recordings.before.txt"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -printf '%p\n' | /usr/bin/sort \
  > "$EVIDENCE_DIR/$SCENARIO.recording-paths.before.txt"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -exec /usr/bin/sha256sum '{}' + | /usr/bin/sort -k2 \
  > "$EVIDENCE_DIR/$SCENARIO.recording-sha256.before.txt"
```

After profiler cleanup has stopped and finalized its owned recording, repeat
the inventory, derive the new path set, hash it, and decode every new file to
end-of-stream without retaining decoded audio:

```sh
set -eu
SCENARIO="<SAME_LOWERCASE_SCENARIO_KEY>"
EVIDENCE_DIR="<ABSOLUTE_EVIDENCE_DIRECTORY>"
RECORDING_DIR="/var/lib/tinypirelay/recordings"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -printf '%p\t%s\t%m\t%u\t%g\t%T@\n' | /usr/bin/sort \
  > "$EVIDENCE_DIR/$SCENARIO.recordings.after.txt"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -printf '%p\n' | /usr/bin/sort \
  > "$EVIDENCE_DIR/$SCENARIO.recording-paths.after.txt"
sudo /usr/bin/find "$RECORDING_DIR" -xdev -type f -name '*.flac' \
  -exec /usr/bin/sha256sum '{}' + | /usr/bin/sort -k2 \
  > "$EVIDENCE_DIR/$SCENARIO.recording-sha256.after.txt"
/usr/bin/comm -13 \
  "$EVIDENCE_DIR/$SCENARIO.recording-paths.before.txt" \
  "$EVIDENCE_DIR/$SCENARIO.recording-paths.after.txt" \
  > "$EVIDENCE_DIR/$SCENARIO.recording-paths.new.txt"

: > "$EVIDENCE_DIR/$SCENARIO.recording-sha256.new.txt"
: > "$EVIDENCE_DIR/$SCENARIO.recording-decode.txt"
while IFS= read -r RECORDING; do
  sudo /usr/bin/test -f "$RECORDING"
  sudo /usr/bin/sha256sum "$RECORDING" \
    >> "$EVIDENCE_DIR/$SCENARIO.recording-sha256.new.txt"
  sudo -u tinypirelay-media /usr/bin/gst-launch-1.0 -q \
    filesrc location="$RECORDING" ! flacparse ! flacdec ! fakesink
  /usr/bin/printf '%s\tdecode-eos-pass\n' "$RECORDING" \
    >> "$EVIDENCE_DIR/$SCENARIO.recording-decode.txt"
done < "$EVIDENCE_DIR/$SCENARIO.recording-paths.new.txt"
```

An empty new-path list fails a recording row. Every pre-existing path and hash
must remain unchanged, each new path must be inside the configured recording
directory, and every new file must have a retained hash and decode pass. A
rotation row must also contain the declared number of newly finalized files.

## Entry checks

Do not begin a measured interval until all checks below pass:

- the Pi is a Zero W Rev 1.1 for the first matrix run;
- both services are active with positive, stable `MainPID` values;
- capture is already `running`, the level queue is visible and bounded, and
  diagnostics show no runtime error;
- the iMM-6C or other named test device is visible through ALSA at the exact
  intended capture mode;
- temperature is below 65 C, `vcgencmd get_throttled` is exactly `0x0`, and at
  least 64 MiB memory is available;
- the recording filesystem is mounted, writable, and below 80% used;
- the route from the Pi reaches the dedicated workstation listener at
  `192.0.2.30` (publication-redacted endpoint), with no SRT passphrase or stream ID in this baseline;
- the workstation's inbound UDP path for the declared listener port is already
  approved and restricted to the Pi/test network; do not create a broad or
  persistent firewall exception implicitly; and
- configuration and service restart counts have been recorded before any
  approved change.

Run a 120-second shakedown before each first-of-kind row with a one-second
sample interval and a 300-second checkpoint interval. Discard it from the
measured result, but retain a failed shakedown as a failure artifact. On the
Zero W, use a ten-second sample interval and a 7,200-second checkpoint interval
for every 30-minute measured row. That yields 181 samples and keeps synchronous
checkpoint serialization outside the measured interval. A different cadence
or retry must use a new scenario key and retain the earlier artifact unchanged.

## Zero W scenario matrix

Use the exact approved stream representations. Run only one streaming
representation at a time. A 30-minute row is a benchmark, not a soak.

| Key stem | Duration | Pre-existing state | Profiler additions | Workstation receiver | Purpose |
|---|---:|---|---|---|---|
| `zw1-capture-meter-30m` | 1,800 s | capture only | none | no | capture, level, and web idle baseline |
| `zw1-pcm-stream-30m` | 1,800 s | PCM stream connected | none | PCM S16LE, 48 kHz, stereo | PCM streaming cost/continuity |
| `zw1-flac-stream-30m` | 1,800 s | FLAC stream connected | none | FLAC, 48 kHz, stereo | FLAC streaming cost/continuity |
| `zw1-opus-stream-30m` | 1,800 s | Opus stream connected | none | Opus, 48 kHz, stereo | Opus 128 kbit/s cost/continuity |
| `zw1-recording-30m` | 1,800 s | capture only | `--start-recording` | no | FLAC recording alone |
| `zw1-pcm-record-30m` | 1,800 s | PCM stream connected | `--start-recording` | PCM | concurrent PCM plus FLAC recording |
| `zw1-flac-record-30m` | 1,800 s | FLAC stream connected | `--start-recording` | FLAC | concurrent FLAC stream/record |
| `zw1-opus-record-30m` | 1,800 s | Opus stream connected | `--start-recording` | Opus | concurrent Opus plus FLAC recording |
| `zw1-opus-bitrate-10m` | 600 s | Opus stream connected | `--start-stream`, exact Opus representation, four bitrate flags 60 s apart | Opus | live 64/96/128/192 kbit/s changes and restoration |
| `zw1-opus-spectrum64x5-30m` | 1,800 s | Opus stream; 64 bands at 5 Hz | `--spectrum` | Opus | minimum spectrum cost |
| `zw1-opus-spectrum512x5-30m` | 1,800 s | Opus stream; 512 bands at 5 Hz | `--spectrum` | Opus | initial Legacy target |
| `zw1-opus-spectrum512x10-30m` | 1,800 s | prior spectrum row passed | `--spectrum` | Opus | upper Legacy update-rate candidate |
| `zw1-opus-web-churn-30m` | 1,800 s | Opus stream; 512 bands at 5 Hz | `--spectrum` | Opus | declared browser connect/disconnect and slow-client schedule |
| `zw1-candidate-soak` | at least 21,600 s | accepted subset connected | row-dependent | required if streaming | six-hour candidate soak and at least two recording rotations |

For a recording soak, use the longer of 21,600 seconds and two configured
rotation intervals plus 600 seconds. Use a 10-second sample interval for a
six-hour run so the profiler remains below its 4,096-sample cap. Other boards
are not started until the Zero W rows either pass or have a precise retained
blocker.

A non-streaming candidate may be extended to 86,400 seconds with a 30-second
cadence (2,881 samples) and checkpoints no more than 7,200 seconds apart. The
current paired receiver is bounded to 86,400 seconds and must run 60 seconds
longer than the Pi collection, so a paired Pi collection is capped at 86,340
seconds (2,879 samples at 30-second cadence). That paired duration is not a full
24-hour claim. Recalculate the sample count before every custom duration; never
rely on the one-second default for a long soak. Extending the receiver contract
requires a separately reviewed and tested source bundle.

The web-churn row needs an independently reviewed client driver or a fully
documented manual schedule. Until one is frozen, record it as pending; do not
invent a client count or call ordinary browser use a churn test.

## Paired streaming sequence

The receiver must remain connected for the whole Pi collection. The installed
Step 4 configuration cannot stream, so prepare each row explicitly:

1. Verify stream and recording are stopped, capture the row's before snapshots,
   and retain the saved configuration revision and private configuration hash.
2. In the authenticated UI, save exactly the row's approved configuration. A
   streaming row uses stream enabled, its one approved representation,
   destination `192.0.2.30:9153` (publication-redacted endpoint), 200 ms latency, the declared Opus bitrate
   and monitoring settings, and blank stream ID/passphrase. A non-streaming row
   keeps stream disabled with representation and destination null. Accept a
   restart-required save only with the approval recorded for that row.
3. Start the receiver command below in a separate workstation terminal before
   any action that can start the stream. If the save requires a restart, run
   the approved `sudo /usr/bin/tinypirelay restart media`; an enabled stream
   starts after capture recovers. If the exact saved configuration is already
   active, start the stopped stream in the UI instead.
4. Wait for healthy capture, the expected representation, an SRT `connected`
   state, complete active queue visibility, and stable service PIDs. Resolve the
   two profiler PIDs only after that restart and readiness check, then begin the
   profiler promptly.

Run the Pi profiler *without* `--start-stream`; this makes the stream
pre-existing, so profiler cleanup cannot stop it before the receiver finishes.
If recording is part of the row, let the profiler own it with
`--start-recording`. The bitrate row is the one syntax exception: its scheduler
requires `--start-stream --expected-representation
opus_128k_48000_stereo_matroska`. Because that stream is already running, the
profiler observes its existing generation, does not own or stop it, and restores
its original bitrate during cleanup.

Set the receiver observation 60 seconds longer than the Pi collection. When the
Pi report completes, leave the stream running until the receiver exits with a
passing report, then stop the stream in the UI and capture the final installed
snapshots. If safety requires stopping earlier, stop it and retain both reports
as a failed/aborted row. After the complete matrix, use the authenticated UI to
restore the exact hashed Step 4 baseline configuration, perform its approved
media restart, and verify the restored hash, disabled stream, healthy capture,
and stable services. A failed exact restore is a cleanup failure, not permission
to overwrite the private configuration ad hoc.

Record `END_UTC` immediately after the Pi profiler completes its measured
interval and cleanup, before the receiver's deliberate later self-exit. The
sender-side disconnect caused by that self-exit is outside the steady measured
interval; retain it as cleanup diagnostics and do not misclassify it as either
steady continuity or a product recovery pass.

The workstation receiver CLI is frozen to the current Step 5 evidence contract:

```powershell
$Scenario = '<SAME_LOWERCASE_SCENARIO_KEY>'
$SourceBundleSha256 = '<64_HEX_SOURCE_BUNDLE_SHA256>'
$PiDurationSeconds = 1800
$MaximumPairedPiDurationSeconds = 86340
if ($PiDurationSeconds -gt $MaximumPairedPiDurationSeconds) {
  throw 'Paired Pi duration exceeds the frozen receiver contract.'
}
$ReceiverDurationSeconds = $PiDurationSeconds + 60
$ReceiverTimeoutSeconds = $ReceiverDurationSeconds + 180
$Ffmpeg = 'C:\ProgramData\chocolatey\bin\ffmpeg.exe'

py -3 -B .\spikes\step5_ffmpeg_receiver.py `
  --output ".\evidence\$Scenario.receiver.json" `
  --port 9153 `
  --latency-ms 200 `
  --duration-seconds $ReceiverDurationSeconds `
  --timeout-seconds $ReceiverTimeoutSeconds `
  --expected-codec opus `
  --expected-rate-hz 48000 `
  --expected-channels 2 `
  --ffmpeg-path $Ffmpeg `
  --scenario $Scenario `
  --source-bundle-sha256 $SourceBundleSha256
```

Change only `--expected-codec` to `pcm_s16le` or `flac` for the matching
representation. Matroska is auto-demuxed, exactly one audio stream is decoded
to a null sink, and no audio is retained. `--scenario` and
`--source-bundle-sha256` are mandatory evidence bindings. Passphrases and stream
IDs are deliberately rejected by this baseline receiver.

The Pi profiler CLI is frozen to the bundled Step 5 contract. Its required
arguments are control socket, distinct positive media/web PIDs, exact 64-hex
source-bundle SHA-256, scenario matching
`^[a-z0-9][a-z0-9._-]{0,63}$`, and output. Duration is 1 second to 7 days;
cadence is 0.2 to 60 seconds with at most 4,096 samples; transition timeout is
1 to 120 seconds; checkpoint cadence is 300 to 7,200 seconds. The profiler uses
fixed shell-free `/usr/bin/vcgencmd get_throttled` observations about every 10
seconds and at both boundaries. The exact base invocation is:

```sh
set -eu
SOURCE_ROOT="<ROOT_OWNED_STAGED_SOURCE_DIRECTORY>"
EVIDENCE_DIR="<ABSOLUTE_EVIDENCE_DIRECTORY>"
SOURCE_BUNDLE_SHA256="<64_HEX_SOURCE_BUNDLE_SHA256>"
SCENARIO="<SAME_LOWERCASE_SCENARIO_KEY>"
DURATION_SECONDS=1800
SAMPLE_INTERVAL_SECONDS=10
CHECKPOINT_INTERVAL_SECONDS=7200
MEDIA_PID="$(/bin/systemctl show tinypirelay-media.service -p MainPID --value)"
WEB_PID="$(/bin/systemctl show tinypirelay-web.service -p MainPID --value)"
/usr/bin/test "$MEDIA_PID" -gt 0
/usr/bin/test "$WEB_PID" -gt 0
/usr/bin/test "$MEDIA_PID" -ne "$WEB_PID"

sudo /usr/bin/env PYTHONPATH="$SOURCE_ROOT/src" \
  /usr/bin/python3 -B "$SOURCE_ROOT/spikes/step5_hardware_profile.py" \
  --control-socket /run/tinypirelay/control.sock \
  --media-pid "$MEDIA_PID" \
  --web-pid "$WEB_PID" \
  --source-bundle-sha256 "$SOURCE_BUNDLE_SHA256" \
  --scenario "$SCENARIO" \
  --duration-seconds "$DURATION_SECONDS" \
  --sample-interval-seconds "$SAMPLE_INTERVAL_SECONDS" \
  --transition-timeout-seconds 15 \
  --checkpoint-interval-seconds "$CHECKPOINT_INTERVAL_SECONDS" \
  --output "$EVIDENCE_DIR/$SCENARIO.pi-profile.json"
```

The Zero W 30-minute contract above expects 181 samples, zero periodic
checkpoint writes, and one final report with `checkpoint.complete=true`.
Final serialization happens after collection and may delay artifact
availability; it must not be counted as measured uptime. Short shakedowns keep
the one-second cadence described under Entry checks.

For steady matrix rows, add only the matrix flags for the row. The pre-existing
bitrate row adds:

```text
--start-stream --expected-representation opus_128k_48000_stereo_matroska --opus-bitrate 64000 --opus-bitrate 96000 --opus-bitrate 128000 --opus-bitrate 192000 --bitrate-interval-seconds 60
```

A profiler-owned stream is permitted only for an unpaired shakedown, using
`--start-stream` together with the exact matching `--expected-representation`.
The only approved values are
`pcm_s16le_48000_stereo_matroska`, `flac_48000_stereo_matroska`, and
`opus_128k_48000_stereo_matroska`. Opus bitrate flags may repeat at most eight
times, use only 64000/96000/128000/192000, and leave at least one sample interval
after the final change. A profiler-owned stream cannot replace workstation
decode evidence.

## Conservative acceptance thresholds

These are entry acceptance gates, not measured product defaults. Do not loosen
them after viewing a failed run. Tune one measured bottleneck, freeze a new
source/config identity, and rerun the complete affected row.

| Area | Pass gate |
|---|---|
| Pairing | all mandatory artifacts present; hashes and scenario keys match exactly; measured intervals overlap as declared |
| Tool results | Pi `execution_status=pass`; receiver `status=pass` for streaming rows; no incomplete checkpoint substituted for a final report |
| Capture/scopes | capture generation unchanged; every intended scope stays running; steady streaming stays connected; no runtime error or warning |
| Decode | expected codec/rate/channels; no FFmpeg error; zero missing/non-monotonic/discontinuous PTS; receiver wall gaps/tail/cleanup pass its built-in gates |
| Queues | every active queue visible and bounded; no `queue_overrun`; no current value exceeds a positive maximum; no three consecutive samples at or above 80% of a positive bound |
| Schedule | zero missed sample slots and every sample delay no greater than one requested interval |
| CPU | over the post-warm-up window defined below, system busy p95 at most 85% and maximum below 95% on the single-core Zero W |
| Thermal/power | maximum below 75 C and every observed `get_throttled` value exactly `0x0` |
| Memory | minimum available memory at least 64 MiB; no OOM; media and web each end no more than 8 MiB above their post-warm-up RSS baseline and have no positive final-window slope above 128 bytes/s |
| Descriptors/threads | each process ends no more than two FDs/threads above its post-warm-up baseline and never exceeds that baseline by more than eight |
| Network | routed-interface transmit drops do not increase in a steady row; reconnect/failure counters do not increase; decoded receiver remains continuous |
| Recording | destination remains below 88% used in a normal row; every stopped/rotated FLAC finalizes and decodes to EOS; expected rotations exist |
| Logs | no error hot loop; each service adds at most 8 MiB of journal text in six hours and no repeated warning/error exceeds one event per minute |
| Browser load | client schedule is retained; media thresholds and receiver continuity remain satisfied; a web-client failure never changes media generations |

Compute these acceptance gates from the retained raw samples; the profiler's
whole-run summary remains a convenience, and its `execution_status` does not
enforce CPU, growth, log, receiver, or product-profile thresholds. For a run of
duration `D`, the warm-up boundary is `min(300 seconds, 0.10 * D)`. The
post-warm-up window contains samples whose `scheduled_elapsed_seconds` is at or
after that boundary, and its baseline is the first such sample. The final window
is the chronological suffix containing `ceil(0.80 * N)` of the `N`
post-warm-up samples. Compute p95 with the profiler's linear-interpolation
definition and the RSS slope by its least-squares `linear_slope` definition over
`(elapsed_seconds, rss_bytes)` points in that final window. Retain the evaluated
values and pass/fail decisions in the scenario manifest.

For 30-minute rows, resource slopes are diagnostic only; an absence of growth
over 30 minutes is not an endurance claim. Apply growth acceptance to the
six-hour candidate soak, and extend the run if the slope is dominated by
warm-up or rotation boundaries.

## Abort and cleanup

Abort a normal row immediately on any of these conditions:

- temperature reaches 75 C, any throttling/undervoltage bit is nonzero, memory
  falls below 64 MiB, or the recording filesystem reaches 88% used;
- CPU remains at or above 98% for 60 seconds;
- an active queue exceeds a bound, reaches 80% for three consecutive samples,
  or reports overrun/loss;
- capture, service PID/start ticks, or an unintended generation changes;
- the receiver exits, reports a decoded gap, or stops making progress;
- an unexpected SRT disconnect/reconnect, recording warning, runtime error, or
  repeated log error appears; or
- the operator cannot account for the current state.

For an approved fault row, only the disconnect/error named by that row is
expected; all other abort conditions remain active.

On abort, press Ctrl+C once and allow the profiler to write its final report and
clean up owned scopes. Do not kill it while cleanup is progressing. Verify its
`cleanup.actions`, `cleanup.errors`, final service generations, configuration
revision, Opus bitrate, spectrum lease expiry, recording finalization, and the
receiver result. A changed generation makes the profiler refuse ownership-based
cleanup; resolve that state manually and document it. If immediate safety
requires stopping a pre-existing stream or recording, do so in the UI and mark
the row aborted. Reboot only with separate approval.

## Controlled failure rows

Fault rows are separate from steady benchmarks. Capture a before, fault, return,
and recovered timestamp. The steady-state profiler may intentionally report
`fail` when connection continuity is broken; that raw status neither passes nor
fails the product fault row. The row is judged from its declared recovery gate
and paired artifacts.

| Row | Approved action | Required outcome/evidence |
|---|---|---|
| Receiver absent/return | with capture running, delay the listener, then start it | bounded retry without hot loop or queue overrun; capture generation stable; connection returns within 30 s; new receiver interval passes |
| Network loss/return | interrupt only the Pi-to-receiver path with out-of-band Pi access | capture and recording continue; no queue overrun; connection returns within 30 s of route restoration; post-return receiver interval passes |
| Audio loss/return | physically remove and return the named capture device | failure is bounded and visible; recording finalizes; no corrupt file; capture/service recovery within 120 s or precise blocker retained |
| Web crash | `sudo systemctl kill --kill-whom=main --signal=KILL tinypirelay-web.service` | systemd restarts web; media PID/generations and receiver continuity remain unchanged; authenticated UI recovers |
| Slow client/churn | reviewed driver or retained manual schedule only | bounded clients and memory; overload is explicit; media generations and receiver continuity unchanged |
| Recording hard stop | dedicated disposable test filesystem only; cross 90% or make that mount read-only | active FLAC finalizes where possible; recording alone stops/refuses restart; capture/SRT continue; warning is visible; recovery only below threshold |
| Rotation | allow at least two configured boundaries | each prior FLAC finalizes/decodes; stream and capture generations remain stable; directory growth is accounted for |
| Reboot recovery | approved `sudo systemctl reboot` with local recovery access | boot ID changes; both services/capture recover within 120 s; private/config hashes persist; separate post-reboot receiver interval passes |

For the receiver-absent/return row, replace the base profiler argument
`--transition-timeout-seconds 15` with `--transition-timeout-seconds 120`.
Launch the profiler while the configured stream is retrying without a listener,
then start the receiver on the declared timeline and require connection within
30 seconds of listener start. The profiler records initial and ready status but
does not take periodic samples until readiness; judge the absent interval from
those statuses, the per-service journals, and `fault-timeline.json`. If sampled
resource behavior during an outage is required, use a separate approved row
that stops and returns an established receiver during collection; that steady
profiler result is expected to fail continuity and must not be relabeled as a
pass.

Do not span reboot with one receiver continuity report: the intentional outage
would correctly fail its wall-gap gate. Bind separate pre- and post-reboot
receiver reports to the same reboot row.

Unsupported caps, invalid SRT settings, DNS failure, corrupt configuration,
missing elements, rapid command races, and service/install interruptions remain
required product cases, but they need their own bounded restore plans. Do not
improvise them on the only installed appliance or reinterpret isolated fixtures
as physical results.

## Final artifact manifest

Use one manifest row per artifact with: relative path, SHA-256, byte count,
producer host, UTC acquisition time, scenario, source-bundle SHA-256, and
classification (`raw`, `manual`, `derived`, or `public-redacted`). At minimum it
contains:

```text
<scenario>.source.tar.gz
<scenario>.source-members.txt
<scenario>.manual-metadata.json
<scenario>.installed-diagnostics.before.json
<scenario>.install-state.before.json
<scenario>.services.before.txt
<scenario>.services-journal-media.txt
<scenario>.services-journal-media-bytes.txt
<scenario>.services-journal-web.txt
<scenario>.services-journal-web-bytes.txt
<scenario>.pi-profile.json
<scenario>.receiver.json                 # streaming rows
<scenario>.recordings.before.txt          # recording rows
<scenario>.recordings.after.txt           # recording rows
<scenario>.recording-paths.before.txt      # recording rows
<scenario>.recording-paths.after.txt       # recording rows
<scenario>.recording-paths.new.txt         # recording rows
<scenario>.recording-sha256.before.txt     # recording rows
<scenario>.recording-sha256.after.txt      # recording rows
<scenario>.recording-sha256.new.txt        # recording rows
<scenario>.recording-decode.txt            # recording rows
<scenario>.fault-timeline.json            # fault rows
<scenario>.installed-diagnostics.after.json
<scenario>.install-state.after.json
<scenario>.services.after.txt
<scenario>.manifest.json
```

The manifest records evaluation separately from raw tool status: `pass`,
`fail`, `aborted`, or `pending`, the exact failed thresholds, reviewer, and UTC
decision time. Never overwrite a failed artifact with a rerun; assign a new
scenario key.

## Profile decisions and pending boards

Legacy/Standard/Full selection uses board model, CPU, RAM, and measured
capability. It never uses 32/64-bit userland alone. Choose the smallest spectrum
and UI settings that pass with headroom, preserving the priority order:
capture, SRT, recording, diagnostics, meters, then spectrum. Under pressure,
reduce/suspend spectrum first, then nonessential UI telemetry; never silently
change audio, stop SRT, or discard recording.

| Target | Userland | Current Step 5 status |
|---|---|---|
| Pi Zero W Rev 1.1 | 32-bit armhf | pending this procedure; existing finite Step 2-4 evidence is not a Step 5 profile claim |
| Pi Zero 2 W | 32-bit | Likely compatible / untested; pending |
| Pi Zero 2 W | 64-bit | Likely compatible / untested; pending |
| Pi 3-class | representative 32-bit and 64-bit | Likely compatible / untested; pending |
| Pi 4-class | representative 32-bit and 64-bit | Likely compatible / untested; pending |
| Pi 5-class | representative 64-bit, 32-bit only if supported/relevant | Likely compatible / untested; pending |

## Explicit nonclaims and deferrals

- No result exists merely because this procedure or either tool exists.
- A 120-second shakedown or 30-minute benchmark is not a soak.
- A Pi profiler pass without the paired decoded receiver is not streaming
  acceptance.
- The disposable FFmpeg receiver is not the planned receiving appliance,
  MediaMTX/BirdNET bridge, or proof that those containers ingest the feed.
- Decoded continuity is not a packet-loss, WAN-impairment, or RF-quality claim.
- Stream ID and encryption/passphrase interoperability remain unverified.
- One Zero W result does not generalize to another board, OS release, userland,
  power supply, storage device, audio interface, or network.
- No Legacy/Standard/Full default is selected until the complete physical
  matrix and threshold review support it.
- MPEG-TS, SMPTE ST 302M, `gstreamer1.0-libav`, FRAME ingest, and expansion of
  the MediaMTX-to-BirdNET receiving bridge remain deferred until after Step 6.
  Step 5 continues to test only the three approved Matroska-over-SRT
  representations.
