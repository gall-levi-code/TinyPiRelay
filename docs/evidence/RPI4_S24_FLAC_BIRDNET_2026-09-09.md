# Pi 4 S24 capture / S16 FLAC bridge — 2026-09-09

Publication copy: private endpoints, operator paths and the obsolete operational
handoff are redacted. The original report is retained privately. See
[redaction notes](PUBLICATION_REDACTIONS.md).

## Finite conversion validation

The user selected native 24-bit local capture/recording while retaining the
existing 16-bit SRT representation. The normal configuration validator correctly
rejected that previously untested capture/stream pairing. It was not bypassed.

An isolated 60-second physical GStreamer probe temporarily stopped only the
media service, used the iMM-6C at S24LE / 48 kHz / mono, and restored the service
afterward. The saved 24-bit configuration was preserved and became active.
The probe used the existing stream branch's conversion, queue limits, FLAC
quality 5, streamable Matroska, and SRT caller parameters. A parallel FLAC
recording retained native capture without conversion.

- Board: Raspberry Pi 4 B; GStreamer 1.26.2; installed runtime 0.5.9-dev.
- Native file: 3,221,611 bytes; STREAMINFO reports 24-bit / 48 kHz / mono,
  2,875,200 samples (59.9 s); GStreamer decoded it through EOS successfully.
- LAN receiver: FLAC / S16 / 48 kHz / stereo, 59.712 decoded seconds,
  60 observation blocks, no non-monotonic PTS or discontinuities,
  maximum observed progress wall gap 1.063 s.
- The sender reached EOS after its planned 60-second interrupt. Two existing
  `gst_buffer_list_foreach` critical assertions appeared at startup, without a
  bus error or observed decoded discontinuity; this is not a clean-log claim.
- The receiver was deliberately configured for 80 s while the sender ended at
  60 s. Its endurance assessor therefore reports **fail**, including an I/O
  error at sender shutdown. This artifact establishes finite negotiated format
  and decoded progress, not a passing 80-second continuity/endurance row.
- BirdNET's distinct `TinyPiRelay TEST` source reported receiving healthy PCM
  through the temporary RTSP bridge and received 3,581,638 bytes. Its downstream
  source normalizes to mono; RTSP output from the bridge is S16BE stereo.

Only the exact iMM-6C S24LE / 48 kHz / mono to S16 stereo FLAC option is admitted
in 0.5.10-dev. No runtime graph code or compatibility guard changed. Local FLAC
is lossless relative to capture; SRT FLAC is lossless only after 24-to-16-bit
conversion. PCM/Opus from S24 and Scarlett streaming remain unvalidated.

## Temporary receiving setup

Pi SRT caller sends to workstation 192.0.2.30:9153. The existing disposable
FFmpeg receiver optionally republishes decoded audio as PCM S16BE over RTSP
TCP. A separate cached MediaMTX 1.19.3 container exposes only loopback RTSP
8555 and joins BirdNET's existing Docker network. It has one publisher path,
`tinypirelay-test`. The original MediaMTX configuration is byte-for-byte
restored; no existing feeds were restarted. BirdNET 20260716 adds only one
named test source. BirdWeather, MQTT and push integrations were checked off;
artificial detections may remain in local history, never treated as wild birds.

Raw probe artifacts are retained privately on the workstation and Pi;
operator-specific paths are omitted. The following installed combined
recording/SRT/spectrum run is separate evidence; its completed results are below.
This does not establish Step 6 acceptance or species-recognition accuracy.

## Installed shakedown

0.5.10-dev was installed through the normal immutable upgrade from a full source
archive, 520,878 bytes, SHA-256
`33a35610dc360c96472afd9d785a320cad039ef70e096c8819ac3376c02986bb`.
The Windows suite ran 414 tests without failures (19 platform skips); all 17
receiver tests passed. Configuration and web credential hashes were unchanged
by upgrade. Only afterward was the user's S24 setting combined with the
temporary stream destination through the normal revision-checked config API.

The 120-second installed shakedown passed every profiler invariant (121 samples)
with simultaneous native FLAC recording, SRT FLAC and 60 Hz / 512-band spectrum.
System CPU averaged 12.715%, media CPU 48.317% of one core; temperature peaked
at 63.783 C. No throttling, runtime errors or process restarts occurred.
The profiler-owned recording finalized and decoded through EOS. The independent
180-second bridge receiver passed all checks with no FFmpeg errors and
continuous PTS. Receiver shutdown occurred after profiling; subsequent SRT
retries were outside the shakedown window, and the stream was stopped before
the main run. Browser consumption was not active during the shakedown (web
CPU near zero); spectrum production was kept active by the profiler lease.

## Completed 30-minute results

The installed combined run passed all profiler checks: 181 samples over 1800 s,
no missed slots, stable media/web processes, continuous capture/recording/SRT,
bounded queues, spectrum production observed, no runtime errors and no
throttling. This is a finite success, not general hardware-profile assignment
or Step 6 acceptance.

| Measurement | Result |
|---|---:|
| Total Pi CPU, mean / maximum | 23.00% / 27.74% |
| Media CPU, mean (one-core scale) | 56.02% |
| Web CPU, mean (one-core scale) | 29.78% |
| Temperature, mean / maximum | 68.89 / 70.60 C |
| Main recording | 1800.07 s, 93,551,339 bytes |
| Native file format | FLAC, 24-bit, 48 kHz, mono |
| Receiver observation | 1861 decoded one-second blocks |
| Receiver PTS discontinuities / non-monotonic PTS | 0 / 0 |
| Maximum receiver progress wall gap | 1.079 s |
| BirdNET detections during main 30-minute window | 183 across 36 labels |

The independent receiver completed naturally at 05:05:33 UTC; every check
passed, with no FFmpeg warnings/errors, no timeout and no forced termination.
Its extra minute supplies coverage around Pi profiler startup/finalization;
it is not part of the 30-minute detection count. These measurements do not
establish screen refresh rate or sample-perfect waveform equality. Unlike the
shakedown, the main run included active web-server traffic.

Both newly finalized recordings decoded through EOS with exit 0. The main
file contains 86,403,360 samples, SHA-256
`f40afb8a18fd2ed134c2e957879cd311e543004f281d0441a5d214c746abb57c`.
The shakedown file contains 5,783,040 samples (120.48 s), 6,478,091 bytes,
SHA-256 `c850fee891072b054787785d669130f397b5ae5aa2ce51377d07a68725ea4eb6`.
Both pre-existing September3 recordings retained their exact size and SHA-256.

BirdNET's collector captured 181 healthy/receiving samples over 1800 seconds,
with no request errors, source restarts or stalled-byte intervals. Maximum
sample interval was 10.013 s, maximum reported data age 0.108 s, and the
received-byte counter increased by 172,781,568 bytes. Its collection begins
04:35:02 UTC, 30.38 seconds after the Pi run begins, and ends 05:05:02 UTC;
it is sampled downstream evidence, not continuous coverage of the initial
30 seconds. Complete September8/9 detection pagination was filtered to the
test source and exact UTC windows. The main window contains 183 detections
and 36 labels; the extended bridge window contains 188 and 37. Examples include
White-breasted Nuthatch, House Finch, Canada Goose and Barred Owl. Classifier
confidence is not recognition accuracy, and these are playback-test detections,
not observations of birds in the environment.

Guarded Pi cleanup succeeded at 05:07:33 UTC. The pre-test S24LE48kmono,
stream-disabled configuration and original credential both match their exact
pre-test hashes; capture remains running without errors and recording/SRT are
stopped. Only the added BirdNET source was disabled; both existing source
objects remained unchanged. Only `tinypirelay-test-mediamtx` was stopped (not
deleted), and the original MediaMTX configuration hash remains unchanged.
All recordings, clips, detection history and evidence were retained.

Final raw artifact SHA-256 values, retained privately on the workstation:

- Pi profiler: `bb0ece7d1db214f537cb41723351c0e6adc9faee8775c52e4da65ea9e3d80e7f`.
- Receiver: `b3635201ebc29a0f4d10f5fef541c2601b48b0d1f072f563c880fb6f716e79da`.
- BirdNET JSONL: `04f2f549df50eeb650cb899187681ce1af9ab1520716e81cff489ad74310081d`.
- Recording verification: `c59346d60e55699114416914f42bfbad5b86bd9211570a5ffdb5f1a62d5950e6`.

The obsolete private operational handoff is omitted from this publication copy.

Supporting helper SHA-256: receiver
`2e92304b2f6976b84f6b933ce86a038f21d23ce5fa67d04cf1c878088565373e`;
BirdNET observer `398acf6296f15de119b8fcfe6086b09c30b09907b12e2292e447b68fcdf9200c`;
Pi cleanup `c07b59efe213524af2cc47926b1ca200e1e29251fb22adcdd0bbe9b026bb2c72`.
The historical report was updated after freezing the source archive; runtime,
capability map, tests and receiver in that archive are unchanged. Publication
redaction changes this report, not the historical archive.
