# Public evidence redactions — 2026-09-13

The public documentation and finite media fixture are reviewed publication
copies. Before redaction, the affected originals were copied to a private
workstation folder outside the repository. That backup is not a release asset.

Observed LAN endpoints are replaced consistently with documentation-only
addresses: `192.0.2.10` for the Zero W, `192.0.2.20` for the Pi 4, and
`192.0.2.30` for the workstation receiver. These are not reachable setup
defaults: replace them with the addresses of your own devices when using a
command recipe. Port numbers and codec parameters are unchanged.

Operator-specific workstation paths are omitted. The completed BirdNET report
also omits its obsolete operational handoff, including SSH-key locations,
terminal-session identifiers and cleanup instructions. Experiment results,
scope limitations and artifact digests remain.

The changed reports and
`evidence/live_media_rpi_zero_w_imm6c_2026-08-18.json` are **not byte-identical
original evidence**. The JSON includes explicit publication-redaction metadata.
Recorded hashes continue to describe the historical artifacts named beside
them, not a newly redacted report or newly built release. The linked probe and
Opus-mutation JSON artifacts were not modified, so their checked hashes remain
valid. Retaining these originals privately does not recover the raw temporary
logs that the original reports already record as deleted.
