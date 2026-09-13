# Step 1 Media Spike

Status: canonical commands retained; finite Step 1 evidence passed for the three exact representations.

These commands test proposed representations. They are not the production media engine and do not establish broad hardware or production acceptance. Run the receiver first, then its matching sender, preserve both logs, and stop each run cleanly with `Ctrl+C`. The authoritative command/result records are `evidence/stream_compatibility.json`; finite variants and summarized results are retained separately in `evidence/live_media_rpi_zero_w_imm6c_2026-08-18.json`. The completed physical run retained metrics and available log hashes, not the deleted raw logs.

## Current host boundary

The Windows development host has Python 3.12.6 and FFmpeg/ffplay 8.0.1 with libsrt, but no local GStreamer, ALSA, VLC or APT. Physical tests ran on a Raspberry Pi Zero W Rev 1.1 (`armv6l`, 32-bit `armhf`) with GStreamer 1.26.2, PyGObject 3.50.0 and a Dayton Audio iMM-6C. FFplay interoperability used version 7.1.5 from an already-cached Linux/amd64 Docker image on the Windows receiver; VLC was unavailable on both systems.

## Representations and endpoints

| ID | Codec and sender parser | Framing/muxer | Physical capture relationship | Local test port |
|---|---|---|---|---:|
| `pcm_s16le_48000_stereo_matroska` | PCM S16LE, 48 kHz stereo; no parser | streamable Matroska; `matroskamux` | Converted from iMM-6C native mono S16LE/48 kHz | 9101 |
| `flac_48000_stereo_matroska` | FLAC quality 5 from 48 kHz stereo S16LE; no sender parser | streamable Matroska; `matroskamux` | Converted from iMM-6C native mono S16LE/48 kHz | 9102 |
| `opus_128k_48000_stereo_matroska` | Opus 128 kbit/s from 48 kHz stereo S16LE; `opusparse` | streamable Matroska; `matroskamux` | Converted from iMM-6C native mono S16LE/48 kHz | 9103 |

The loopback commands use 200 ms SRT latency. For two hosts, replace `127.0.0.1` in the sender URI with the receiver address and permit only the selected UDP port. The iMM-6C exposed native mono `S16_LE` and `S24_3LE` at 48 kHz; GStreamer negotiated these as `S16LE` and `S24LE`. The declared streams used native mono S16LE and converted channel count to stereo while retaining 48 kHz/S16LE. Native S24 capture also passed separately but was not the source mode for these three stream runs.

## Synthetic sender commands

PCM:

```sh
gst-launch-1.0 -e -v audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9101?mode=caller&latency=200' wait-for-connection=true
```

FLAC:

```sh
gst-launch-1.0 -e -v audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! flacenc quality=5 ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9102?mode=caller&latency=200' wait-for-connection=true
```

Opus:

```sh
gst-launch-1.0 -e -v audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! opusenc bitrate=128000 ! opusparse ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9103?mode=caller&latency=200' wait-for-connection=true
```

## ALSA sender commands

Replace `<ALSA_DEVICE>` with an identifier reported by the probe, such as `hw:1,0`. These commands intentionally convert to the representation caps after capture; the evidence record must separately state the selected device's native capture mode. For the iMM-6C tests, mono-to-stereo was a software conversion and must never be presented as native stereo capture.

PCM:

```sh
gst-launch-1.0 -e -v alsasrc device='<ALSA_DEVICE>' ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9101?mode=caller&latency=200' wait-for-connection=true
```

FLAC:

```sh
gst-launch-1.0 -e -v alsasrc device='<ALSA_DEVICE>' ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! flacenc quality=5 ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9102?mode=caller&latency=200' wait-for-connection=true
```

Opus:

```sh
gst-launch-1.0 -e -v alsasrc device='<ALSA_DEVICE>' ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! opusenc bitrate=128000 ! opusparse ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9103?mode=caller&latency=200' wait-for-connection=true
```

## GStreamer receiver commands

PCM:

```sh
gst-launch-1.0 -v srtsrc uri='srt://:9101?mode=listener&latency=200' ! matroskademux ! queue ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! fakesink sync=false
```

FLAC:

```sh
gst-launch-1.0 -v srtsrc uri='srt://:9102?mode=listener&latency=200' ! matroskademux ! queue ! flacparse ! flacdec ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! fakesink sync=false
```

Opus:

```sh
gst-launch-1.0 -v srtsrc uri='srt://:9103?mode=listener&latency=200' ! matroskademux ! queue ! opusparse ! opusdec ! 'audio/x-raw,format=S16LE,layout=interleaved,rate=48000,channels=2' ! fakesink sync=false
```

For each run, record negotiated caps, element/library versions, sender and receiver exit status, warnings/errors, duration and evidence that decoded buffers continued to arrive. `fakesink` avoids assuming an output sound device; a clean process start alone is not a passing result.

## FFmpeg/ffplay receiver attempts

Use the port matching the sender. FFmpeg's SRT latency value is in microseconds here.

```sh
ffplay -hide_banner -loglevel info -nodisp -autoexit 'srt://0.0.0.0:9101?mode=listener&latency=200000'
ffplay -hide_banner -loglevel info -nodisp -autoexit 'srt://0.0.0.0:9102?mode=listener&latency=200000'
ffplay -hide_banner -loglevel info -nodisp -autoexit 'srt://0.0.0.0:9103?mode=listener&latency=200000'
```

Record demux/decoder selection, audible or decoded continuity, errors and exact `ffplay -version` output. An initial direct-host receiver path was blocked before codec evaluation by the Windows firewall. The retained successful attempts used an already-cached Docker image with explicit UDP port publication and dummy audio; their summary records version, progress and process status, but the raw receiver logs were deleted after review. They prove decoding, not audible quality.

## VLC receiver attempts

Use the port matching the sender:

```sh
cvlc --play-and-exit --no-video 'srt://@:9101?mode=listener'
cvlc --play-and-exit --no-video 'srt://@:9102?mode=listener'
cvlc --play-and-exit --no-video 'srt://@:9103?mode=listener'
```

Record the exact VLC version, module/codec selection, continuity and errors. VLC/cvlc was unavailable on both tested systems, so its result is `unavailable` and no VLC compatibility claim is made.

## Finite physical evidence

The canonical commands above remain long-running recipes. The executed finite variants added bounded buffer counts/timeouts and retained argv, timestamps, exit codes, summarized continuity metrics and available sender/receiver log hashes in [`evidence/live_media_rpi_zero_w_imm6c_2026-08-18.json`](../evidence/live_media_rpi_zero_w_imm6c_2026-08-18.json). In this publication copy, the private receiver endpoint in those argv strings is replaced with a documentation-only address; see [redaction notes](evidence/PUBLICATION_REDACTIONS.md). Original raw logs were deleted from the temporary target directories after review and are not repository artifacts.

| Evidence | Result |
|---|---|
| Native iMM-6C capture | Mono S16LE and S24LE at 48 kHz passed in ALSA and GStreamer; ALSA's 24-bit container was `S24_3LE` |
| Synthetic GStreamer loopback | PCM, FLAC and Opus passed in approximately 5-6 seconds |
| Physical ALSA GStreamer loopback | All three passed after mono S16LE to stereo S16LE conversion; PCM/Opus delivered 241 buffers and FLAC 51 |
| FFplay LAN interoperability | All three passed with cached-container FFplay 7.1.5; reported decoded progress was 4.77, 3.32 and 4.74 seconds for PCM, FLAC and Opus. The final FLAC audit used the exact canonical sender with no parser before `matroskamux` |
| VLC interoperability | Unavailable; VLC was not installed and no package was added for this attempt |

These are scoped **Hardware tested** short functional results for the exact Pi/device/representation paths, not a blanket board claim. Each finite listener logged an SRT warning when the caller intentionally closed. Each LAN sender and the queue/mutation sender logged two non-fatal `gst_buffer_list_foreach` critical assertions; no bus error, nonzero exit, missing decoded stream or PTS discontinuity was observed. The cause remains an implementation risk.

## Runtime Opus bitrate mutation procedure

This cannot be established with `gst-launch-1.0`. A retained programmable PyGObject spike passed on the physical ARMv6 Pi; it is evidence tooling, not the production controller.

1. Build the Opus pipeline with `opusenc name=encoder bitrate=128000`, then start a GStreamer listener that records continuous decoded-buffer timestamps.
2. From the spike's control thread, while the pipeline remains `PLAYING`, call `encoder.set_property("bitrate", 192000)`; later repeat with 64000, 96000 and 128000.
3. Record each property readback, pipeline state, all bus errors/EOS, and receiver buffer/PTS progress for every bitrate phase.
4. Pass this scoped mutation check only if every readback matches, both pipelines remain `PLAYING`, decoded buffers advance in every phase, PTS stays monotonic, and no bus error or unexpected EOS occurs. This does not directly establish SRT connection identity or reconnect count; those remain separate Step 2 lifecycle evidence.

The observed sequence was 128000 -> 192000 -> 64000 -> 96000 -> 128000 bit/s. The 4.744-second run received 126 buffers with monotonic PTS, a 20 ms maximum gap, no bus error and no unexpected EOS. It recorded readbacks and uninterrupted decoded progress, not `notify::bitrate`, SRT connection identity/statistics or reconnect counts. Full details are in [`evidence/opus_bitrate_mutation_rpi_zero_w_2026-08-18.json`](../evidence/opus_bitrate_mutation_rpi_zero_w_2026-08-18.json). Do not turn this spike into the production media controller.

## Independent monitoring-queue stall procedure

This test exercises only a disposable monitoring branch. A leaky queue is not appropriate for recording or stream data; Step 2 must explicitly handle and remove a failed non-disposable branch.

Start the Opus GStreamer receiver above on port 9103, then run:

```sh
GST_DEBUG=queue:6 gst-launch-1.0 -e -m audiotestsrc is-live=true wave=sine ! audioconvert ! audioresample ! 'audio/x-raw,format=S16LE,rate=48000,channels=2,layout=interleaved' ! tee name=t t. ! queue ! opusenc bitrate=128000 ! opusparse ! matroskamux streamable=true ! srtsink uri='srt://127.0.0.1:9103?mode=caller&latency=200' t. ! queue max-size-buffers=8 max-size-bytes=0 max-size-time=0 leaky=downstream ! identity sleep-time=1000000 ! fakesink sync=false
```

Run long enough to fill the slow branch queue. For future runs, retain queue debug output showing its bound/drop behavior, sender SRT state and receiver buffer timestamps. A finite variant passed over approximately 15 seconds wall time while the slow branch drained: the stream receiver observed all 251 handoffs (about 5 seconds of stream content) while the one-second-per-buffer slow branch handled 13, with both processes exiting 0. The repository retains those counts and sender/receiver log hashes, not the deleted raw debug logs. This proves short-run isolation for a disposable leaky branch only, not 15 seconds of stream endurance.

## Raspberry Pi OS package verification without installation

Run these commands on each target image before changing packages. Do not run `apt update`, `apt install`, or a distribution upgrade as part of Step 1.

```sh
cat /etc/os-release
uname -m
getconf LONG_BIT
dpkg --print-architecture
uname -srv
grep -E '^(Model|Revision|Hardware|processor|model name)' /proc/cpuinfo
awk '/MemTotal/ {print}' /proc/meminfo
df -hT
```

From the repository root, run the read-only project probe in both formats:

```sh
PYTHONPATH=src python3 -m tinypirelay.probe
PYTHONPATH=src python3 -m tinypirelay.probe --format json
```

Query the configured APT repositories without installing anything:

```sh
apt-cache policy python3 python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 alsa-utils libgstreamer1.0-0 gstreamer1.0-tools gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
apt-get --simulate --no-install-recommends install python3 python3-gi gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 alsa-utils libgstreamer1.0-0 gstreamer1.0-tools gstreamer1.0-alsa gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad
```

Then retain detailed plugin output:

```sh
gst-inspect-1.0 --version
for element in alsasrc audioconvert audioresample capsfilter tee queue level spectrum opusenc flacenc srtsink audiotestsrc matroskamux matroskademux srtsrc flacparse opusparse flacdec opusdec fakesink identity; do gst-inspect-1.0 "$element"; done
arecord -l
arecord -L
```

Repeat on the current stable Raspberry Pi OS first, and on Bookworm where practical, with a real Zero W ARMv6 plus representative 32-bit and 64-bit systems. Record results using `docs/evidence/COMPATIBILITY_EVIDENCE_TEMPLATE.md`. The tested Zero W package installation was completed by the user, not by this repository work. Its GStreamer registry was initialized only in a validated temporary directory under `/tmp`, then queried with registry updates disabled. Other boards, 64-bit systems and Bookworm remain untested.

## Evidence gate

A candidate may enter `declared_capabilities` only when its retained record has a real GStreamer sender pass, a real GStreamer receiver pass, and resolved FFmpeg/ffplay and VLC attempt results. All three exact candidates now satisfy that gate: GStreamer and FFplay passed, while unavailable VLC is explicitly resolved without claiming VLC support. Synthetic and physical ALSA evidence are retained separately.

Validate the retained record and inspect the fail-closed result from the repository root:

```sh
PYTHONPATH=src python3 -m tinypirelay.compatibility validate evidence/stream_compatibility.json
PYTHONPATH=src python3 -m tinypirelay.compatibility list evidence/stream_compatibility.json
PYTHONPATH=src python3 -m tinypirelay.compatibility capabilities evidence/stream_compatibility.json
```

The argument arrays in that JSON are the canonical command source; the shell renderings above must stay token-for-token equivalent.

The evidence permits Step 2 to begin implementing only these three exact representations in a programmable PyGObject media process. Approximately five-second passes do not establish reconnect behavior, endurance, packet-loss tolerance, recording, simultaneous monitoring, production lifecycle behavior or audible quality.
