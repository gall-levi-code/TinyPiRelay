# Raspberry Pi Zero W Physical Step 2 Evidence — 2026-08-18

Publication copy: LAN endpoints use documentation-only addresses. The original
report is retained privately. See [redaction notes](PUBLICATION_REDACTIONS.md).

## Record identity

| Field | Value |
|---|---|
| Test window | 2026-08-18T19:00:30Z through 2026-08-18T19:03:37Z |
| Operator | Codex via user-authorized SSH; the user connected the device and completed package installation |
| Target | Physical Raspberry Pi Zero W Rev 1.1, `armv6l`, 32-bit `armhf`, Raspberry Pi OS Trixie |
| Kernel and runtime | `6.18.34+rpt-rpi-v6`; Python 3.13.5; GStreamer 1.26.2 |
| Capture paths | Synthetic live audio and Dayton Audio iMM-6C at `hw:CARD=iMM6C,DEV=0`, native mono S16LE/48 kHz |
| Scoped result | **Hardware tested** finite Step 2 scenarios; all 11 checks passed on both paths |
| Overall board label | **Likely compatible / untested** |
| TinyPiRelay identity | Unborn `master`; uncommitted/untracked Step 1 and Step 2 working tree |
| Pi test bundle | SHA-256 `98da0704437e4ad9ce41e3700bf17bbb75f94d6fdc6e63e504733d3c99f66abc` |
| Final target-tested source bundle | SHA-256 `7f0b4d39ff49275ebdcbc79805def61cedd1b0782373ab095f38361ccb141565`; excluded `.git`, `__pycache__`, and `*.pyc`; passed 143 tests in 3.417 seconds (10.357 seconds wall), compatibility validation with three capabilities, a zero-action/exit-0 probe, and the media-service help smoke check at 38.5 C with `get_throttled=0x0`. Documentation was complete except this row and the matching `PROJECT_STATE` identity line; only those two fields changed afterward. |

Retained machine-readable artifacts:

- [synthetic Step 2 run](../../evidence/step2_synthetic_rpi_zero_w_2026-08-18.json), 44.708 seconds, SHA-256 `e2760ab1715136c3fca8eae84455c8febc78e68c224c98421562fa7450dcd80c`;
- [physical iMM-6C Step 2 run](../../evidence/step2_alsa_rpi_zero_w_imm6c_2026-08-18.json), 45.782 seconds, SHA-256 `e55ad31be48aa8e5d1061191f9e903c8cedca1a30e46dcf87e7a73f59b788f68`; and
- [Step 1 physical evidence](RPI_ZERO_W_2026-08-18.md), which separately establishes the three approved stream representations and native capture modes.

The retained Step 2 reports contain no passphrase. Their receiver is a disposable local GStreamer listener used only for evidence; the sender is the production `GStreamerEngine` and `MediaRuntime` on one GLib context.

## Platform and finite setup

The board was a Zero W Rev 1.1 with ARMv6 single-core hardware, 32-bit `armhf` userland, Raspberry Pi OS Trixie, and GStreamer 1.26.2. The physical path used the iMM-6C as mono S16LE/48 kHz capture. The stream was the approved `opus_128k_48000_stereo_matroska` representation, converted to stereo where needed, sent in SRT caller mode to a disposable listener on loopback with 200 ms configured latency. Monitoring was 5 spectrum updates/sec and 64 bands.

Both runs placed sender and receiver on the same Pi. That makes the finite end-to-end result useful, but adds receiver/demux/decoder load that a normal remote receiver would not impose. It is not an endurance or capacity benchmark.

### Reproducible invocations

Run these from the unpacked repository root after creating the two recording directories. They are reconstructed from the complete argument values retained in each JSON artifact and the current harness CLI; the original interactive shell transcript was not retained, so they are not represented as verbatim shell-log excerpts.

```sh
mkdir -p ../synthetic-recordings ../alsa-recordings

PYTHONPATH=src python3 -B spikes/step2_media_integration.py \
  --source-factory audiotestsrc \
  --capture-format S16LE --rate-hz 48000 --channels 2 \
  --port 9113 --latency-ms 200 \
  --recording-directory ../synthetic-recordings \
  --storage-poll-seconds 5 --minimum-duration-seconds 35 \
  --timeout-seconds 120 --min-buffer-delta 25 \
  --output evidence/step2_synthetic_rpi_zero_w_2026-08-18.json

PYTHONPATH=src python3 -B spikes/step2_media_integration.py \
  --source-factory alsasrc --device-id 'hw:CARD=iMM6C,DEV=0' \
  --capture-format S16LE --rate-hz 48000 --channels 1 \
  --port 9113 --latency-ms 200 \
  --recording-directory ../alsa-recordings \
  --storage-poll-seconds 5 --minimum-duration-seconds 35 \
  --timeout-seconds 140 --min-buffer-delta 25 \
  --output evidence/step2_alsa_rpi_zero_w_imm6c_2026-08-18.json
```

Post-run observations were 36.3-37.9 C with `get_throttled=0x0`. The recorded post-run memory figures were 447647744 bytes total, 148332544 used, 299315200 available, and 4280320 bytes swap used. `/tmp` was 12% used (26423296 of 223825920 bytes). The tested route was loopback `lo`.

## Scenario results

All eleven retained checks passed in both artifacts:

| Check | Retained evidence | Result |
|---|---|---|
| Capture and Opus connection | Capture, stream, and connection reached running/connected; decoded buffers advanced | **Hardware tested** finite pass |
| Runtime bitrate mutation | 128 -> 64 -> 192 kbit/s readback; capture generation, stream generation, and physical stream token stayed unchanged | **Hardware tested** finite pass |
| FLAC start, rotation, stop, and decode | Two native FLAC fragments per run finalized with FLAC headers and decoded to EOS | **Hardware tested** finite pass |
| Concurrent branches and SRT statistics | Stream, recording, level, and spectrum ran together; both engine and runtime reported positive `bytes-sent-total` | **Hardware tested** finite pass |
| 90% recording hard stop | Injected exactly 90.0% status stopped/finalized recording, latched `storage_unsafe`, preserved capture/SRT, then cleared after a safe poll | **Hardware tested** control/isolation path with a **simulated threshold status** |
| Receiver loss and reconnect | Clean physical removal, bounded retry, a distinct replacement token, restored connection, and decoded progress | **Hardware tested** finite pass |
| No telemetry consumers | Zero consumers were declared while latest-only meter/spectrum sequences advanced and queues stayed within their configured bounds | **Hardware tested** finite pass |
| Uninterrupted pre-loss phases | No reconnect, replacement stream token, missing PTS, or non-monotonic PTS during bitrate, recording, threshold, and monitoring phases | **Hardware tested** finite pass |
| Error channels | No unexpected engine fatal error or receiver bus error | **Hardware tested** finite pass |
| Ordered shutdown | Recording removal/finalization preceded stream removal, then capture stop and shutdown completion | **Hardware tested** finite pass |
| Retained recording output | Explicit recording directories were preserved on the authorized Pi at evidence time and every assessed file existed | **Hardware tested** finite pass |

The synthetic run observed 1704 decoded receiver buffers overall; the ALSA run observed 1430. Both reported zero missing and zero non-monotonic PTS.

### Reconnect detail

After the established listener was stopped, the first retry occurred after 1.378 seconds (synthetic) and 1.407 seconds (ALSA). The offline physical attempt ended through the native GStreamer/SRT error path after 2.951 and 2.974 seconds respectively, with normalized code `srt_gstreamer_error_10` and factual SRT connection-timeout detail. Each exact token was removed with `forced=false` and `missing=false`.

The listener was restored while the controller remained in `retry_wait`. The next attempts occurred after 2.204 and 2.320 seconds, used a new token, connected, and advanced decoded buffers by 26 and 38. Each run recorded two reconnect attempts in total. The configured five-second runtime watchdog remained as a bounded fallback but did not win these two native-error races.

### Recording detail

Every file below had a FLAC header and decoded to EOS:

The binary FLAC files were retained on the authorized Pi at evidence time and were not committed to this repository. Their sizes and hashes are retained here.

| Path | Purpose | Bytes / SHA-256 |
|---|---|---|
| Synthetic | first fragment | 24829 / `2583643ed2c503e0f9e2c429a644709be2cd8e4a298a095d9dabf9f3ad0f7f75` |
| Synthetic | rotated fragment | 31798 / `1ad028bf2eee931ec6c4d5630cbdf852f75c00746eb5bc2acc1fb43d2dead81e` |
| Synthetic | threshold-finalized fragment | 116638 / `a18f7e812c45792253a4175a3970045467a06f74b3506e7119b24fe943c12f93` |
| Synthetic | active-shutdown fragment | 12700 / `dc8ee207747e225a33a8a3d9eba510d360eb7c93400b1c533af70b311373e34f` |
| iMM-6C | first fragment | 29899 / `93112c1ea970eb7f8862e58df01a9c5826461781f14569278eea3e02dd800adb` |
| iMM-6C | rotated fragment | 39871 / `2756576269efd0625fe0ff7f8e937e85b3cae07a40037ffeac851da1999b8137` |
| iMM-6C | threshold-finalized fragment | 127418 / `f4acaa85f2ecd687d2f0a507dd044c6c4b8b553e72f616c671d2ca53b446867c` |
| iMM-6C | active-shutdown fragment | 15567 / `d8428f69e7995c35b0756fb5513582ca30b4813b1db5a0739b8cc91091ca07df` |

### Queue and monitoring detail

The configured contracts were a non-leaky 10-second stream queue, a non-leaky 5-second recording queue, and downstream-leaky level/spectrum queues capped at four buffers. The contracts and nonnegative current levels were retained while all four branches were active.

The most important performance observation is the stream queue during the no-consumer window:

- synthetic source: 1.579 seconds current level of the 10-second limit;
- iMM-6C source: 7.560 seconds current level of the 10-second limit.

The ALSA result passed the finite bound but used 75.6% of it. This is an explicit performance risk, not headroom evidence. The same-Pi SRT receiver added load, and no sustained CPU profiling was performed. Longer runs and Step 5 profiling must determine whether this reflects transient test topology or inadequate Zero W margin.

## Repository tests

Bundle SHA-256 `98da0704437e4ad9ce41e3700bf17bbb75f94d6fdc6e63e504733d3c99f66abc` ran on the Pi with Python 3.13.5. `PYTHONPATH=src python3 -B -m unittest discover -s tests -p 'test_*.py' -v` passed all 143 tests in 3.422 seconds (9.576 seconds wall time).

Host tests use controlled fakes to cover capture-device loss, read-only/unmounted destination behavior, invalid caps/configuration, repeated commands, stale callbacks/timers, and shutdown edge cases. Those tests validate controller behavior but are not physical hardware evidence.

## Limitations and non-claims

- The two production-engine scenarios were only 44.708 and 45.782 seconds. Endurance, memory/file-descriptor/log growth, packet impairment, and sustained performance remain untested.
- The 90% event was a simulated destination-status injection. The real recording filesystem was not filled to 90%, made read-only, or unmounted.
- Capture-device unplug/return was covered by host fakes, not performed on the physical microphone.
- No external telemetry client was attached. The absence-of-consumer path and bounded/latest-only behavior were exercised; slow-client and client-churn behavior belongs to Step 3.
- Stream ID and encryption/passphrase were disabled in these loopback runs.
- An exploratory send to the supplied workstation ingest `srt://192.0.2.30:4001?streamid=live_birdmic` did not accept the approved streamable-Matroska representation. It is not counted as a receiver or codec result, and no capability claim is made for that endpoint.
- Each artifact contains a harmless duplicate `capture-stopped` acknowledgement during ordered shutdown: the reducer ignored the stale second event and ordering/finalization still passed. Exactly-once terminal event cleanup remains a controller-contract precision item for later integration.
- No installer, systemd service, web process, device-recovery hardware run, reboot recovery, or power-interruption test was performed.

## Conclusion

The Step 2 programmable media engine passed all eleven finite scenarios with both synthetic capture and the physical iMM-6C on the Zero W, including dynamic bitrate, FLAC lifecycle, simulated storage isolation, bounded monitoring, reconnect, and ordered shutdown. These rows are **Hardware tested** only for their exact finite conditions. The board remains **Likely compatible / untested** overall because endurance, performance profiling, real storage/device failures, and lifecycle installation evidence remain open.
