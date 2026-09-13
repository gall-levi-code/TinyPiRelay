# Step 6: FLAC decoder success does not prove completeness

Local synthetic check, 2026-09-11. Windows FFmpeg/FFprobe
`8.0.1-full_build-www.gyan.dev`; no Pi or user recordings were accessed.

The existing FFmpeg fallback returned exit code zero for both a complete
one-second FLAC and a copy cut at a frame boundary. Both STREAMINFO headers
declare 44,100 mono samples; the truncated copy decodes only 18,432 samples.

| Fixture | Bytes | Declared / decoded samples | Checker exit |
|---|---:|---:|---:|
| Complete | 20,322 | 44,100 / 44,100 | 0 |
| Truncated at fifth-frame start | 13,282 | 44,100 / 18,432 | 0 |

SHA-256 of complete bytes:
`1b429f6ae1a872154cb06da0cdf65eea2a75f4ab4d32864a6ec56cbac8060cea`.
SHA-256 of truncated bytes:
`b467545e98ee4dabfb82171e6c17cade016e1ef941bb784ed374902e376db180`.
Neither decode emitted an error. Other FFmpeg builds may produce different
bytes; the declared-versus-decoded sample mismatch is the relevant result.

Reproduction requires Python, FFmpeg and FFprobe. The Python body below was
run locally with `python -B -c`; this equivalent command uses a POSIX shell.
All audio stays in memory. The STREAMINFO sample count is filled explicitly
because FFmpeg cannot seek back to update a pipe output header.

```sh
python3 -B - <<'PY'
import hashlib, json, subprocess

def run(command, data=None):
    return subprocess.run(command, input=data, capture_output=True, check=True)

audio = bytearray(run([
    'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
    'sine=frequency=440:duration=1', '-c:a', 'flac', '-f', 'flac', 'pipe:1'
]).stdout)
packets = json.loads(run([
    'ffprobe', '-v', 'error', '-show_packets', '-show_entries',
    'packet=pos,size', '-of', 'json', '-f', 'flac', 'pipe:0'
], audio).stdout)['packets']
field = int.from_bytes(audio[18:26], 'big')
audio[18:26] = ((field & ~((1 << 36) - 1)) | 44100).to_bytes(8, 'big')
cut = int(packets[4]['pos'])
checker = [
    'ffmpeg', '-nostdin', '-hide_banner', '-v', 'error', '-xerror',
    '-err_detect', 'crccheck+bitstream+buffer+explode', '-f', 'flac',
    '-i', 'pipe:0', '-map', '0:a:0', '-f', 'null', '-'
]
for label, data in [('complete', audio), ('truncated', audio[:cut])]:
    result = subprocess.run(checker, input=data, capture_output=True)
    pcm = run([
        'ffmpeg', '-v', 'error', '-f', 'flac', '-i', 'pipe:0',
        '-c:a', 'pcm_s16le', '-f', 's16le', 'pipe:1'
    ], data).stdout
    print(label, len(data), hashlib.sha256(data).hexdigest(),
          'declared=44100', 'decoded=' + str(len(pcm) // 2),
          'exit=' + str(result.returncode), result.stderr.decode())
PY
```

The fix keeps decoder functionality but returns `decoded` / “Decode only”
for FFmpeg and GStreamer success, with an explicit incomplete-tail warning.
Only `flac --test` retains `passed`, with no original-completeness, provenance
or recovery guarantee. GStreamer truncation behavior was not measured here;
its plain decode pipeline also lacks an explicit completeness-validation
contract, so its success receives the same conservative label.

Focused regression coverage in `test_recording_library.py` checks both
fallback labels, preserves `needs_check` metadata, and verifies cleanup after
descriptor/thread startup failures. All 16 library tests passed in a
network-disabled Python 3.12 Alpine container, with the source mounted read-only.
