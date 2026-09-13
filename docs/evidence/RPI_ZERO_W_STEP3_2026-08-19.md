# Raspberry Pi Zero W Physical Step 3 Evidence — 2026-08-19

Publication copy: LAN endpoints use documentation-only addresses. The original
report is retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

## Record identity

| Field | Value |
|---|---|
| Test date | 2026-08-19 UTC; exact start/end timestamps were not retained |
| Operator | Codex via user-authorized SSH; credentials are intentionally omitted |
| Target | Physical Raspberry Pi Zero W Rev 1.1, `armv6l`, 32-bit `armhf`, Raspberry Pi OS Trixie |
| Kernel and runtime | `6.18.34+rpt-rpi-v6`; Python 3.13.5; GStreamer 1.26.2 |
| Physical capture | Dayton Audio iMM-6C at `hw:CARD=iMM6C,DEV=0`, native mono S16LE/48 kHz |
| External receiver | Workstation `192.0.2.30`, FFplay 8.0.1 with libsrt, listening for SRT on port 9141 |
| Scoped result | **Hardware tested** for the finite physical rows below; current assessor passes 7/7 checks |
| Overall board label | **Likely compatible / untested** |
| TinyPiRelay identity | Unborn `master`; the working tree is uncommitted/untracked and has no commit identity |
| Original Pi test bundle | SHA-256 `12d81834309cecaaa9d429e3ebbcb436625048a8654164f3716a1b354359d50d`; 92 members, 269041 bytes; excluded `.git`, `__pycache__`, and `*.pyc` |
| Final target-tested source bundle | SHA-256 `407c5d4c524d7d315d05de2e279f426c22a612f1a4f39a9a9cb784ba22ec0bed`; 96 members, 282826 bytes; excluded `.git`, `__pycache__`, and `*.pyc`. Pi Python 3.13.5 passed all 269 tests in 80.432 seconds (89.832 seconds wall) with one expected missing-Node skip; compatibility validation returned exactly three representations; the probe exited 0 with status `present`, one capture device, full GStreamer status, and zero actions (report SHA-256 `2168efb7b299b5e0ef3e66d3f3f0cc6010082d9acc6345fb6a0f4601715239da`); 34 Python files and 13 JSON files parsed; media/web help smokes exited 0; the immutable physical artifact reassessed 7/7 pass; temperature was 36.3 C before and 39.5 C after with `get_throttled=0x0`. After target validation, only final identity/readiness prose in this report and `PROJECT_STATE` changed; no source, configuration, test, or retained machine-evidence payload changed. |

Retained machine-readable artifacts:

- [simulated Step 3 control-plane run](../../evidence/step3_web_simulated_rpi_zero_w_2026-08-19.json), SHA-256 `aa066e04494e5fa016789a4ccdf721e8668a0d7ad74845facafa754a0b4e0be1`;
- [physical iMM-6C/web-isolation run](../../evidence/step3_web_physical_rpi_zero_w_imm6c_2026-08-19.json), SHA-256 `68d0d426150aa3461a12720dc54c51245d55d851e3178f60971dd3c7d84f7997`; and
- [physical spectrum reattachment run](../../evidence/step3_spectrum_reattach_rpi_zero_w_imm6c_2026-08-19.json), SHA-256 `6366e0e044594cf5762f3bc8b56b52aefb4bc3236dbe1aae35bc490315d02ca2`.

The first bundle predates the final spectrum-teardown and evidence-assessor
patches. Its full target suite passed, and the patched spectrum behavior then
passed the focused physical run. The final bundle recorded above contains the
frozen source/configuration/test/evidence payload that was subsequently tested
on the Pi.

## Physical setup and stream

The Pi was reachable at `192.0.2.10`; the workstation receiver was
`192.0.2.30`. The exact power supply, hub arrangement, storage medium,
filesystem capacity, Wi-Fi link metrics, and ambient temperature were not
retained and remain pending rather than assumed. Preflight temperature was
37.4 C and the post-test observation was 40.1 C, both with
`get_throttled=0x0`. The installed-package manifest was retained by hash only:
`751fa57492e9353bc02c08e3453797897938224575c23f3e865320d8ffe2bce7`.

The production media service captured native mono S16LE/48 kHz from the
iMM-6C, converted the stream branch to stereo, encoded Opus at 128 kbit/s,
muxed streamable Matroska, and called the workstation over SRT with 200 ms
latency. Stream ID and encryption/passphrase were disabled. FFplay was a live
diagnostic listener used to move decode load off the Pi, not part of
TinyPiRelay. Its stdout/stderr was not retained or hashed, so it does not add a
receiver-decoding or codec-compatibility claim:

```powershell
ffplay -hide_banner -loglevel info -nodisp -autoexit "srt://0.0.0.0:9141?mode=listener&latency=200000"
```

The sender command below is the reproducible repository entry point with a
private mode-`0600` configuration:

```sh
export PYTHONPATH=src
TPR_RUN="$(mktemp -d /tmp/tinypirelay-step3-lan.XXXXXX)"
chmod 0750 "$TPR_RUN"
TPR_SOCKET="$TPR_RUN/control.sock"
TPR_CONFIG="/replace/with/private-imm6c-config.json"

python3 -B -m tinypirelay.media_service \
  --config "$TPR_CONFIG" \
  --evidence evidence/stream_compatibility.json \
  --audio-capabilities evidence/audio_capabilities.json \
  --control-socket "$TPR_SOCKET"

```

The physical harness's actual resolved invocation used the temporary control
socket below and wrote `/tmp/step3-web-physical-lan.json`; that file was copied
to the checked-in evidence name without changing its JSON payload:

```sh
PYTHONPATH=src python3 -B spikes/step3_web_integration.py \
  --control-socket /tmp/tinypirelay-step3-lan.6wtFii/control.sock \
  --evidence-kind physical-hardware \
  --manage-media \
  --startup-timeout 20 \
  --continuity-seconds 4 \
  --slow-client-seconds 12 \
  --gst-launch gst-launch-1.0 \
  --output /tmp/step3-web-physical-lan.json
```

The simulator-only fault-state run used the real web subprocess and real
AF_UNIX protocol, but no physical audio or network media:

```sh
PYTHONPATH=src python3 -B spikes/step3_web_integration.py \
  --evidence-kind simulated-control-plane \
  --slow-client-seconds 12 \
  --output evidence/step3_web_simulated_rpi_zero_w_2026-08-19.json
```

## Finite results

The physical harness ran for approximately 91 seconds. Its web-failure segment
was evaluated while SRT was connected and recording was active.

| Scenario | Retained observation | Evidence result |
|---|---|---|
| Independent processes | Real web subprocess and real AF_UNIX socket; web was sent `SIGKILL` and restarted while the media PID remained independent | **Hardware tested** finite pass |
| Primary-media isolation | Capture, logical stream, and recording remained running; their generations were unchanged and SRT bytes advanced through the strict web-kill segment | **Hardware tested** finite pass |
| Spectrum crash lease | Numeric spectrum existed before web failure; after 6.245 seconds it was absent while level telemetry remained present | **Hardware tested** finite pass |
| Session restart behavior | The old in-memory session was invalid after web restart; a fresh login and status request succeeded | **Hardware tested** finite pass |
| Authentication and request security | Five failed logins then `429`, an established session survived throttling, logout invalidated its session, CSRF failed with `403`, malformed/non-Cartesian settings failed with `400`, and secrets stayed redacted | **Hardware tested** finite web/control pass |
| Slow SSE and churn bounds | Two unread clients remained open for 12.088 seconds; a third received `503`; 12 churn attempts completed and a slot recovered | **Hardware tested** finite web-resource pass |
| Web-process resources | RSS changed by +159744 bytes during slow clients and +176128 bytes after; file descriptors changed by +4 and +2 respectively, within the harness bounds | **Hardware tested** finite bound pass, not endurance |
| Media-socket unavailable/recovery | Stopping only a temporary AF_UNIX proxy produced explicit HTTP `503`; restoring it produced HTTP `200`, available state, and unchanged media generations | **Hardware tested** finite control-boundary pass |
| FLAC finalization | The harness-started recording stopped/finalized cleanly: 933821 bytes, 36.29 seconds, mono 48 kHz, SHA-256 `78284bf8a24120bb34bf457b976db7812db0c9f29d9fc2affbc74bf63c388b17`; GStreamer decoded it to EOS | **Hardware tested** finite pass |

The immutable physical JSON embeds `status: fail` for one historical
`media_unavailable_and_recovery_are_explicit` predicate. That older predicate
also required `connection == connected` while the temporary control proxy was
stopped. SRT happened to be retrying then, although the proxy operation did not
stop or change media. The current assessor tests what this scenario actually
claims: explicit `503`, recovery `200`, restored availability, and unchanged
media generations. It returns `pass` with all seven checks true without
rewriting the retained artifact. The strict connected/running requirement
remains in the separate web-`SIGKILL` isolation check and passed there.

Reassess the immutable artifact with the current tree:

```sh
PYTHONPATH=src python3 -B - <<'PY'
import importlib.util, json
from pathlib import Path

path = Path("spikes/step3_web_integration.py")
spec = importlib.util.spec_from_file_location("step3_web_integration", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
report = json.loads(Path("evidence/step3_web_physical_rpi_zero_w_imm6c_2026-08-19.json").read_text())
print(json.dumps(module.assess_report(report), indent=2, sort_keys=True))
PY
```

## Spectrum teardown and reattachment

An initial physical run exposed that immediate `NULL` teardown of the live
spectrum tee branch could prevent a later lease from producing samples. The
engine now removes the disposable branch through the same owner-context IDLE
unlink path used for live branches and accepts telemetry only from the current
active branch. The focused physical artifact passed three consecutive leases:
release, lease expiry, then release. Spectrum sequences were 1, 6, and 34; capture,
stream, and recording generations stayed at 1; and SRT bytes advanced from
1497708 to 1660933 while the final state remained running/connected.

The following is an illustrative public-protocol smoke sequence against a
running media owner. It does not reproduce the artifact's full generation,
sample-advance, and SRT-byte assertions:

```sh
PYTHONPATH=src python3 -B - "$TPR_SOCKET" <<'PY'
import json, sys, time
from tinypirelay.control_protocol import ControlClient

client = ControlClient(sys.argv[1])
for removal in ("release", "expiry", "release"):
    print(json.dumps(client.request("monitoring.spectrum_lease", {}), sort_keys=True))
    time.sleep(1)
    print(json.dumps(client.request("status", {}), sort_keys=True))
    if removal == "expiry":
        time.sleep(5.5)
    else:
        client.request("monitoring.spectrum_release", {})
    print(json.dumps(client.request("status", {}), sort_keys=True))
PY
```

## Tests and browser evidence

- Original Pi bundle:
  `PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v`
  passed 263 tests with one expected Node.js static-asset skip in 80.394
  seconds (90 seconds shell wall time). Compatibility validation reported
  exactly three representations. The read-only probe exited 0; its report
  SHA-256 was
  `f7fb05686193a477ae92ed538d5835a6d60c96c19fc43865875b682626af0459`.
- Simulator artifact: all 7/7 checks passed with the real web subprocess and
  AF_UNIX socket. Device loss, SRT retry, and storage hard-stop displays were
  controlled simulator injections, not hardware faults.
- Browser QA used a simulated POSIX container, not the Pi. Login, dashboard,
  exact capabilities, telemetry canvases, unavailable/recovery state,
  390x844 and 320x568 layouts, contrast, focus visibility, and reduced-motion
  behavior passed with no console errors or observed secret exposure. A full
  automated Tab-order traversal was not completed.
- The final target-tested Pi bundle in the identity table validates the later
  patches. The post-documentation Windows tree passed 269 tests in 11.178
  seconds with eight expected POSIX-only skips.

## Limitations, failures, and non-claims

- Later in the 91-second physical run, SRT stopped advancing about 36.84
  seconds after recording began. The cause was not proven; receiver lifecycle
  is a confounder, and reconnect count later reached 10 after FFplay exited.
  Therefore this run does **not** prove full-run SRT continuity, slow-client
  noninterference with media, or receiver endurance.
- A separate earlier same-Pi sender/receiver diagnostic reached the production
  stream queue bound and emitted exact `queue_overrun` after 50.953 seconds.
  Together with the Step 2 7.56-second queue observation, this is a material
  Zero W headroom risk for Step 5, not a Step 3 web-isolation failure.
- Physical microphone removal/return, real disk-full/read-only/unmount faults,
  network interruption, reboot recovery, CPU load, long-term temperature,
  log growth, and endurance were not tested. Their UI state representations
  used controlled simulation where stated.
- The live FFplay listener was not retained as a receiver-decode artifact. The
  physical JSON proves sender connection and byte progress only during the
  strict web-failure slice; it does not establish receiver continuity, packet
  loss, new codec interoperability, VLC, encryption, stream ID, broad receiver
  support, or FRAME ingest compatibility. The supplied FRAME endpoint on port
  4001 was not accepted as evidence.
- Step 3 did not add MPEG-TS, SMPTE ST 302M, or `gstreamer1.0-libav`. The three
  exact Step 1 representations remain streamable Matroska.
- No installer, users/groups, systemd services, TLS, reverse proxy, Internet
  exposure, installed password-reset wrapper, reboot, upgrade, or uninstall
  behavior was exercised. Those are Step 4 or later concerns.

## Conclusion and Step 4 gate

The finite Step 3 web/authentication/control-plane scope is complete, including
physical proof that a killed web process did not restart primary media during
the strict connected segment, bounded slow-client behavior, finalized FLAC,
and repeatable spectrum release/expiry reattachment. The Zero W remains
**Likely compatible / untested** overall. The passing final bundle identity is
recorded, so Step 4 is ready. Step 5 must retain the unresolved queue/headroom
risk.
