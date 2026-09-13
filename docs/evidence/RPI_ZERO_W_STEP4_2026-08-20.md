# Raspberry Pi Zero W Physical Step 4 Evidence — 2026-08-20

## Record identity

| Field | Value |
|---|---|
| Test date | 2026-08-20 UTC; the full session start/end boundary was not retained |
| Operator | Codex through user-authorized SSH and locally entered `sudo`/web prompts; operator identity, web username, passwords, key path, and key comment are intentionally omitted |
| Target | Physical Raspberry Pi Zero W Rev 1.1, revision `9000c1`, BCM2835/one CPU, `armv6l`, 32-bit `armhf` |
| OS/runtime | Raspbian GNU/Linux 13 Trixie; kernel `6.18.34+rpt-rpi-v6`; Python 3.13.5; GStreamer 1.26.2 |
| Capture device | Dayton Audio iMM-6C at `hw:CARD=iMM6C,DEV=0`, native mono S16LE/48 kHz |
| Installed version | `0.4.0-dev` |
| Working identity | Unborn `master`; the working tree is uncommitted/untracked and has no commit identity |
| Final source archive | SHA-256 `9505ac00966b0015289d0f214cf824e280ee24371083a892a608fa424b044b10`; 347804 bytes; 119 tar members |
| Installed payload | SHA-256 `287ccd1436e112f012dd1610cab9f8a81da7d18c336b6804e7f045a8f02cc339` |
| Final documented source bundle | SHA-256 `9cc1833f2fe7bbb6ea42e5f0b676c15c5cb02308126fe2e3b52c3ba79c2bb0d0`; 361337 bytes; 121 tar members; excludes `.git`, `__pycache__`, and `*.pyc` |
| Machine-readable summary | [Step 4 physical installation evidence](../../evidence/step4_install_rpi_zero_w_imm6c_2026-08-20.json), SHA-256 `78422d82fcef15453c6cc766a3a3552302d61aed8c3720a0251e2d3641ccf609` |
| Scoped result | **Pass — Hardware tested** for the eight finite rows in this report |
| Overall board label | **Likely compatible / untested**; this was not endurance or performance acceptance |

This report and its machine-readable summary were written after the physical
run and were not members of the installed archive. The archive and installed
payload hashes above identify the source, configuration validation, tests, and
packaging that actually ran on the board.

The final documented source bundle included the complete Step 4 evidence and
documentation except this final bundle-identity row and the matching
`PROJECT_STATE.md` identity/readiness prose. On Pi Python 3.13.5 it ran all 333
tests successfully in 237.246 seconds (252.22 seconds shell wall), with one
expected missing-Node skip. After that target validation, only those two
documentation identity/readiness locations changed; source, configuration,
tests, and the machine-evidence JSON did not change.

## Evidence boundary and result

The finite installed-appliance scope passed:

| Physical row | Retained observation | Result |
|---|---|---|
| Fresh install | Read-only preflight passed; no package transaction was needed; a versioned immutable release, validated private configuration, first credential, operator wrapper, and two systemd services were installed and activated | **Hardware tested pass** |
| Repeat install | Plan and apply both returned `noop`; source/current link, private hashes, state, timestamps, PIDs, and restart counters stayed unchanged | **Hardware tested pass** |
| Least privilege/systemd | Separate non-root media/web identities, audio/control group boundary, private file modes, socket mode, cross-identity read denials, unit properties, and sandbox analysis passed | **Hardware tested pass** |
| Installed iMM-6C capture | The media service continuously owned mono S16LE/48 kHz ALSA capture while streaming remained intentionally disabled | **Hardware tested pass** |
| Authenticated dashboard | Login through an SSH loopback tunnel worked; the live meter moved and numeric spectrum telemetry was present | **Hardware tested pass** |
| Web-only restart | Media PID `3437` and capture generation `1` stayed unchanged; only the web PID changed from `3438` to `5162` | **Hardware tested pass** |
| Real FLAC lifecycle | A real finalized 1511181-byte FLAC was retained with mode `0600`, hashed, and decoded to end-of-stream | **Hardware tested pass** |
| Reboot recovery | The retained adjacent boot IDs changed; both services auto-started with zero restarts, ALSA capture and loopback HTTP recovered, and configuration, credential, install-state, and recording hashes were preserved | **Hardware tested pass** |

Upgrade, reverse-version rollback, uninstall/preservation, explicit purge, and
mid-transaction interruption/resume remain isolated POSIX fixture evidence.
They were deliberately not performed on this working appliance.

## Preflight and mutation safety

The initial read-only snapshot at `2026-08-20T14:57:40Z` reported boot ID
`177ec369-1c45-4fba-bdbf-2ee6efdf1c10`, 447647744 bytes RAM, 337846272 bytes
available, an ext4 root on `/dev/mmcblk0p2` at approximately 3% use, 39.008 C,
and `get_throttled=0x0`. Port 8080 was free. No TinyPiRelay service, service
account, managed release, configuration, credential, state, cache, runtime
socket, wrapper, or unit was present.

The 849-package manifest was
`0ac57f1d4fd0bb69bcabcfd84512d389a6ad1188a84e7e75ebd8a1ce0a4da245`
before and after dependency preflight. The APT simulation reported 0 upgraded,
0 newly installed, 0 removed, and 82 not upgraded. All direct dependencies
were already installed, so the actual Step 4 run did not add a package.
In particular, it did not install `gstreamer1.0-libav`.

The private input configuration validated with SHA-256
`679ea6737e406abbda976fcff381db38d0c3da8e1ff1a5533600419c0de0ff96`.
It selected the iMM-6C at mono S16LE/48 kHz, 5 Hz monitoring with 64 spectrum
bands, `/var/lib/tinypirelay/recordings` on `/` with 3600-second rotation, and
128 kbit/s as the dormant Opus setting. Stream enablement, representation,
destination, stream ID, and passphrase were all null/disabled. Thus this run
did not send SRT traffic or test a receiver.

## Interactive terminal failure and root-cause fix

The first approved apply recalculated a valid install plan and reached the
privileged credential stage, then failed with the generic message that a
controlling `/dev/tty` was required. A focused target diagnostic proved that
stdin, stdout, and stderr were PTYs and that raw `/dev/tty` open succeeded.
The real failure was Python 3.13 rejecting this combined wrapper on a
non-seekable terminal:

```python
os.fdopen(descriptor, "r+", encoding="utf-8", buffering=1)
```

It raised `io.UnsupportedOperation: File or stream is not seekable`, which the
older code masked as the generic terminal error. Inspection immediately after
the failure found the package hash unchanged and every managed account,
service, and path still absent.

The root-cause patch uses separate terminal read and write descriptors,
explicitly flushes the username prompt, pins hidden password input only to the
read side, and uses `O_NOCTTY | O_CLOEXEC` where available. The first regression
version then exposed that a PTY test itself could acquire the controlling
terminal; adding `O_NOCTTY` fixed that at the shared opener. The focused pair
passed on the Pi in 1.661 seconds.

Artifact chronology is retained so the tested source is unambiguous:

| Artifact | SHA-256 | Bytes/members | Disposition |
|---|---|---|---|
| Initial plan/failing interactive bundle | `949d6e1560edc2cfdcb034f1adb9b77822c5c91045da4e2e1174bf37ce936bd3` | 347239 / 119 | Payload `d29cf0320f74806a1ffb49f50f9fd1b35e3e23258e35528061079b290d152361`; not installed |
| Superseded first TTY-fix bundle | `9b5fb45cce77e23793de4642e3be10517346f71f2f6982bb2dcab0bd0317f7cb` | 347721 / 119 | Target regression only; not installed |
| Final installed bundle | `9505ac00966b0015289d0f214cf824e280ee24371083a892a608fa424b044b10` | 347804 / 119 | Payload `287ccd1436e112f012dd1610cab9f8a81da7d18c336b6804e7f045a8f02cc339` |

Before the successful apply, the final installer also rejected a password
shorter than its 12-character minimum and exited before installation mutation.
Neither the attempted web username nor either password is retained here. The
next locally entered valid credential completed installation without logging
plaintext.

The exact final source then completed 333 Pi test cases in 187.106 seconds:
332 passed and one expected Node-absent static-asset case was skipped. The same
tree completed successfully on Windows in 33.791 seconds with 18 expected
platform skips and in a network-disabled Alpine container in 60.821 seconds
with one expected Node skip. The latter two are supporting software evidence,
not hardware claims.

## Installed identity and least privilege

The installation journal remained byte-for-byte stable at SHA-256
`96658cff6fac3c7c92d5bd2d3134d98d54c094ae5abdac0db11e119660829540`:
schema 2, phase `complete`, installed/target version `0.4.0-dev`, source SHA-256
`287ccd1436e112f012dd1610cab9f8a81da7d18c336b6804e7f045a8f02cc339`,
and no previous version. `/opt/tinypirelay/current` resolved to
`releases/0.4.0-dev`, and the release content matched that payload identity.

| Boundary | Physical observation |
|---|---|
| Media identity | Dedicated UID 984; member of its private primary group, `audio`, and the shared control group |
| Web identity | Dedicated UID 983; member of its private primary group and the shared control group, but not `audio` |
| Media configuration | Regular file, media-owned, control group, mode `0600`, safe path/type |
| Web credential | Regular file, web-owned/web-group, mode `0600`, safe path/type |
| Control socket | Unix socket, media-owned, control group, mode `0660`, safe path/type |
| Private reads | Cross-identity direct reads of the private configuration/credential were denied |
| Listener | Dashboard bound only to `127.0.0.1:8080`; LAN access used SSH forwarding |
| Media unit | Enabled/active/running; `systemd-analyze security` exposure 3.3, `OK` |
| Web unit | Enabled/active/running; `systemd-analyze security` exposure 2.9, `OK` |

The security scores are the systemd tool's finite policy assessment, not a
claim of Internet-safe deployment or a substitute for a broader security
review.

The installed private hashes stayed constant through no-op and reboot:

| File | SHA-256 |
|---|---|
| Media configuration | `679ea6737e406abbda976fcff381db38d0c3da8e1ff1a5533600419c0de0ff96` |
| Credential file | `0a29a5dbcb592d5b2aea4bd4b5d1c47468571586e4abbe00cfc31b0f16e86287` |

Only the credential-file digest was retained; its content and the web identity
were not copied into repository evidence.

## No-op, dashboard, web isolation, and recording

The repeat read-only plan reported version `0.4.0-dev`, mode `noop`, installed
version `0.4.0-dev`, no planned changes, and no preflight errors. Applying that
plan completed as a no-op without another credential prompt. Media PID `3437`,
web PID `3438`, both zero restart counters, file timestamps, release/current
identity, journal, configuration hash, and credential hash were unchanged.

The workstation already used local port 8080, so the authenticated check
forwarded local `127.0.0.1:18080` to target `127.0.0.1:8080`. Login succeeded,
the live meter moved, and the retained diagnostics contained numeric spectrum
telemetry. Restarting only the web unit changed its PID from `3438` to `5162`.
Media PID `3437`, capture generation `1`, active ALSA capture, and live
telemetry continued. The dashboard recovered without logged errors.

The operator then started and stopped a real recording from the dashboard.
The UI progressed through running/stopping/stopped while primary capture and
meter/spectrum telemetry remained active. The finalized artifact was:

| Field | Value |
|---|---|
| File | `/var/lib/tinypirelay/recordings/tinypirelay-20260820T160654.758798Z.flac` |
| Size | 1511181 bytes |
| Approximate operator-observed duration | 62 seconds; an exact STREAMINFO duration was not retained |
| Ownership/mode | `tinypirelay-media:tinypirelay-control`, `0600` |
| SHA-256 | `71109b89b40940d3d904877207461566d03be484576d4319fb482a61a6962a41` |
| Decode | `filesrc ! flacparse ! flacdec ! fakesink`, exit 0 at end-of-stream |

## Reboot chronology and current state

The immediately adjacent retained reboot pair is exact. At
`2026-08-20T18:16:02Z`, pre-reboot boot ID
`44dd7ebe-40f8-4454-bfdd-867d7bf4fa11` had media PID `817`, web PID `480`,
zero restarts, and ALSA `RUNNING` under PID `817` with `hw_ptr=223528896`.
After the requested reboot, the boot ID was
`dd5a1116-5966-4542-8903-8c7c7e06bc5a`; systemd automatically restored both
enabled services, media PID `816`, web PID `478`, and zero restarts.

The initial preflight boot ID was `177ec369-1c45-4fba-bdbf-2ee6efdf1c10`, not
the adjacent pre-reboot ID. The cause and timing of that intervening boot-ID
transition were not retained. This report therefore proves the explicit
adjacent `44dd...` to `dd5...` reboot/recovery but does not claim exactly one
reboot across the entire validation session.

During the 30-second recovery observation, the two PIDs stayed stable, ALSA's
hardware pointer advanced from 1271088 to 5251056, loopback HTTP returned 200,
and the installed payload matched. Approximate RSS was 28.8 MiB for media and
19.5 MiB for web; temperature was about 41–42 C; throttling stayed `0x0`; and
no warning/error journal entries appeared.

A closing read-only snapshot at `2026-08-20T18:28:58Z` on the same `dd5...`
boot still found PIDs `816`/`478`, zero restarts, 28984/19532 KiB RSS,
loopback HTTP 200, 39.0 C, `get_throttled=0x0`, and zero warning-or-higher
entries for either unit in the current boot. ALSA remained `RUNNING` under PID
`816`; `hw_ptr` advanced from 30362832 to 30509040 in approximately three
seconds. The install-state, configuration, credential, and FLAC digests still
matched their pre-reboot values.

## Executed command forms

After the one-time password-authenticated public-key bootstrap, every validation
SSH command used strict host-key checking, a single ephemeral operator key,
`IdentitiesOnly=yes`, and `BatchMode=yes`. The connection identity, LAN address,
key path/comment, staging paths, web identity, and secrets are redacted. The
post-bootstrap connection envelope was:

```sh
ssh -tt -i '<ephemeral-operator-key>' \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  '<operator>@<device>' '<remote-command>'
```

With `SOURCE`, `CONFIG`, and `OUT` standing only for the validated final source,
private input configuration, and operator-owned validation directory, the
material remote command bodies were:

```sh
cd "$SOURCE" && /bin/sh ./install.sh plan --config "$CONFIG"

cd "$SOURCE" && sudo /bin/sh ./install.sh install \
  --config "$CONFIG" --apply

sudo /usr/bin/tinypirelay diagnostics > "$OUT/root-diagnostics.json" &&
sudo /bin/cat /var/lib/tinypirelay/install-state.json > "$OUT/root-install-state.json" &&
sudo /usr/bin/sha256sum \
  /etc/tinypirelay/media/config.json \
  /etc/tinypirelay/web/credential.json > "$OUT/root-private-sha256.txt" &&
cd "$SOURCE" && sudo /bin/sh ./install.sh plan > "$OUT/repeat-plan.txt"

cd "$SOURCE" && sudo /bin/sh ./install.sh install --apply &&
sudo /bin/cat /var/lib/tinypirelay/install-state.json > "$OUT/noop-install-state.json" &&
sudo /usr/bin/sha256sum \
  /etc/tinypirelay/media/config.json \
  /etc/tinypirelay/web/credential.json > "$OUT/noop-private-sha256.txt"

sudo /usr/bin/tinypirelay restart web &&
sudo /usr/bin/tinypirelay diagnostics > "$OUT/after-web-restart.json"

sudo /usr/bin/tinypirelay diagnostics > "$OUT/after-recording.json" &&
sudo /usr/bin/find /var/lib/tinypirelay/recordings -xdev -type f \
  -name '*.flac' -printf '%s %m %u %g %p\n' > "$OUT/recording-files.txt" &&
sudo /usr/bin/find /var/lib/tinypirelay/recordings -xdev -type f \
  -name '*.flac' -exec /usr/bin/sha256sum '{}' + > "$OUT/recording-sha256.txt"

sudo -u tinypirelay-media /usr/bin/gst-launch-1.0 -q \
  filesrc location=/var/lib/tinypirelay/recordings/tinypirelay-20260820T160654.758798Z.flac \
  ! flacparse ! flacdec ! fakesink

{
  date -u +%FT%TZ
  cat /proc/sys/kernel/random/boot_id
  systemctl show tinypirelay-media.service tinypirelay-web.service \
    -p Id -p MainPID -p NRestarts -p ActiveState -p SubState
  cat /proc/asound/card0/pcm0c/sub0/status
} > "$OUT/pre-reboot.txt" && sudo /usr/bin/systemctl reboot

sudo /usr/bin/tinypirelay diagnostics > "$OUT/post-reboot-diagnostics.json" &&
sudo /bin/cat /var/lib/tinypirelay/install-state.json > "$OUT/post-reboot-install-state.json" &&
sudo /usr/bin/sha256sum \
  /etc/tinypirelay/media/config.json \
  /etc/tinypirelay/web/credential.json \
  /var/lib/tinypirelay/recordings/tinypirelay-20260820T160654.758798Z.flac \
  > "$OUT/post-reboot-private-sha256.txt"
```

The dashboard tunnel used the same redacted key and connection identity:

```sh
ssh -N -i '<ephemeral-operator-key>' \
  -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -L 127.0.0.1:18080:127.0.0.1:8080 '<operator>@<device>'
```

All `sudo`, web username, and web password input occurred locally on controlling
terminals. No secret was requested or copied into chat or repository evidence.

## Retained artifact manifest

Fourteen raw outputs totaling 73525 bytes remained in the operator-owned target
validation directory at evidence capture. Their full sizes and SHA-256 values
are in the machine-readable summary. Important identities include:

- baseline diagnostics:
  `9cd4c083044010a8ec365ac8f767607027d646fa59aec713a45267ccf6484278`;
- repeat/no-op plan:
  `118ccf8b7d3163b71f1e22c8be683ac64e7aa23bf2b30b4c4beeab6957fd2f0b`;
- web-restart diagnostics:
  `fae10b1197830717faa508b8b8ffd0ad3125974b93af10ee4c424419bc339403`;
- post-recording diagnostics:
  `1e220ba3077b9e947e84d34639798d1fb200bff66e32a08648b535ec2f970a26`;
- adjacent pre-reboot record:
  `da671dc71e0842032315e6c3e25198b6a436e7738f46436494a0cbf84b7b6ace`;
- post-reboot diagnostics:
  `a599fa507cdbe42a3b37a2579018a7bc9d564211d7ee1588f56071e23d080761`;
  and
- post-reboot private-hash list:
  `cfce9963060469db39a95c59d1d84947f2bbb7f7deb7a541a895568d9059ac45`.

The raw outputs are summarized rather than copied because some require root to
read on the appliance. Their contents do not include the credential or web
username; only the credential file's digest is retained.

## Limitations and non-claims

- No physical upgrade, reverse-version rollback, default uninstall, reinstall
  after uninstall, or explicit data purge was performed. Their current evidence
  is isolated-fixture evidence only.
- The two rejected interactive attempts ended before installation mutation;
  there was no physical interruption during a committed transaction and no
  physical resume test.
- The microphone was present before services started. Physical late audio
  arrival, unplug/return, or ALSA device renumbering was not exercised.
- The network was present. Physical late network return, Wi-Fi loss/recovery,
  SRT retry, packet impairment, and receiver recovery were not exercised.
- Streaming was disabled. This run adds no SRT continuity, receiver, codec,
  FRAME ingest, or MediaMTX/BirdNET-Go bridge claim.
- Step 4 did not add MPEG-TS, SMPTE ST 302M, or `gstreamer1.0-libav`; the
  deferred receiving bridge and format change remain outside this run.
- The 30-second reboot observation and short closing snapshot are not
  endurance, sustained thermal, log-growth, leak, or Zero W performance-
  headroom evidence. The Step 3 queue/headroom risk remains for Step 5.
- Other boards, Bookworm, 64-bit systems, Internet exposure, TLS deployment,
  and broad hardware compatibility remain untested.
- The ephemeral validation SSH key was not part of TinyPiRelay. This report
  deliberately makes no key-cleanup claim; removal is an operator-evidence
  finalization action, not a product behavior tested here.

## Conclusion

TinyPiRelay `0.4.0-dev` passed the finite Step 4 physical installation gate on
this Raspberry Pi Zero W and iMM-6C: safe fresh and repeat installation,
least-privilege system integration, active capture, authenticated loopback UI,
web-process isolation, real FLAC finalization/decode, and explicit reboot
recovery/preservation. That finite scope is sufficient to begin Step 5
hardware profiling. The overall board remains **Likely compatible / untested**
until performance and endurance work is complete, and destructive/version-
transition lifecycle rows remain fixtures rather than physical claims.
