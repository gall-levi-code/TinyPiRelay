# ADR 0004: Staged installer and least-privilege system integration

- Status: Accepted
- Date: 2026-08-20
- Physical deployment status: Finite Raspberry Pi Zero W acceptance passed

## Context

Steps 1 through 3 produced a programmable media owner and an independent web
control plane, but no installed identities, boot integration, lifecycle
journal, or safe operator command. Installing directly into one mutable source
directory, running either process as root, or accepting credentials through
arguments would turn routine recovery into a security and data-loss risk.

The Raspberry Pi Zero W also constrains the design. The system integration must
use Raspberry Pi OS facilities, avoid another supervisor or package manager,
start the web interface even when audio/network are initially absent, and allow
recordings on operator-managed external mounts.

## Decision

### Artifact and lifecycle

Each artifact carries one validated `VERSION`. Releases are copied to
`/opt/tinypirelay/releases/<version>` and activated through the atomic
`/opt/tinypirelay/current` link. The artifact itself, not an argument or mutable
branch, determines the target version.

`install.sh` exposes separate planning and application boundaries:

```text
plan --config PATH
install [--config PATH] [--credential-fd N] [--apply]
upgrade [--credential-fd N] [--apply]
uninstall [--apply] [--remove-data --confirm-remove-data PHRASE]
```

Without `--apply`, install, upgrade, and uninstall remain plan-only. Preflight
is read-only and package installation is separated from post-install GStreamer
element validation. The plan resolves, temporarily download-checks, and
fingerprints the complete no-upgrade/no-remove APT transaction; application
must observe the same transaction. No path performs a distribution upgrade.

A mode-`0600` lifecycle journal in `/var/lib/tinypirelay/install-state.json`
records staged progress. Repeating the same artifact resumes or becomes a no-op;
an unfinished different target is rejected. Existing configuration is
validated and preserved. Version transitions create a backup before atomic
activation. Failed service activation attempts to restore the prior release and
integration files; a failed fresh activation disables the services and removes
the active link. A previously trusted artifact can perform a reviewed reverse
version transition after an upgrade completes.

### Identity and filesystem separation

The media and web processes run as `tinypirelay-media` and
`tinypirelay-web`. They share only `tinypirelay-control` for the local socket.
The media service alone receives membership in `audio`.

Mutable security domains are split:

```text
/etc/tinypirelay/media/   0700 tinypirelay-media
/etc/tinypirelay/web/     0700 tinypirelay-web
/run/tinypirelay/         0750 tinypirelay-media:tinypirelay-control
control.sock              0660 tinypirelay-media:tinypirelay-control
```

The split directories are necessary because atomic replacement requires parent
directory write access. A flat shared writable directory would let one service
replace the other service's private file even if both files were mode `0600`.

The default recording directory is media-owned. The media unit uses
`ProtectSystem=full`, rather than a strict allowlist of one recording path, so
an operator can use an appropriately mounted and owned external destination.
Unix ownership remains the write boundary. `ProtectHome=true` excludes login
home directories. A dedicated media cache prevents GStreamer from relying on a
writable login home.

### Service behavior and sandbox

Both systemd units use an empty capability bounding set, no new privileges,
private temporary storage, kernel/control-group protections, namespace and
address-family restrictions, bounded tasks/file descriptors, and journal rate
limits.

The media unit receives only `char-alsa` device access and the address families
needed by AF_UNIX and SRT. It starts after `network.target`, not
`network-online.target`, and retries transient runtime failure every five
seconds. Exit status 2 denotes pre-runtime validation/setup failure and is not
restarted, preventing a corrupt configuration from hot-looping.

The web unit has private devices, no audio group, and no dependency or ordering
edge on the media unit. It binds only `127.0.0.1:8080`, while still reporting
media unavailability through the existing control-plane behavior. An SSH
tunnel is the default remote-access mechanism. Direct LAN and Internet
exposure are outside this decision.

Service output stays in journald; per-unit rate limits bound floods. This
project does not create parallel application log files. Host-wide journal
retention remains an operating-system policy.

### Credentials and privileged operations

There are no default credentials. Initial interactive credentials require a
controlling `/dev/tty`; password input is never taken from piped stdin. The
explicit automation interface is a bounded inherited file descriptor, never a
password argument or environment variable. New installation/reset passwords
share one policy: at least 12 characters within the existing 1024-byte bound,
with no artificial composition rules.

The installed CLI keeps privilege crossings explicit:

- status and reports are read-only;
- diagnostics reuse the host probe and redact secrets;
- config validation writes nothing;
- password reset runs the credential writer as `tinypirelay-web`, then restarts
  only the web unit;
- service restart requires root and never grants systemd privileges to the web
  process;
- upgrade is report-only because a new pinned artifact must drive its own
  transition; and
- uninstall defaults to a preservation plan.

Default uninstall removes code and integration files but preserves config,
credential, recordings, backups, state, and service accounts. Data purge
requires both `--remove-data` and the exact confirmation phrase
`REMOVE TINYPIRELAY DATA`. A later reinstall reuses that validated preserved
configuration and rejects a competing `--config` input.

### Bootstrap integrity

The repository contains a reviewed bootstrap mechanism, not a live one-line
installer URL. It accepts an explicit version, HTTPS artifact URL, SHA-256, and
installer arguments; constrains download/extraction and verifies the embedded
version. A checksum must arrive through separately trusted release metadata.
Fetching a mutable branch into a privileged shell is rejected.

No bootstrap URL, signed manifest, or release artifact has been published by
this milestone. Offline/manual operation starts from an artifact whose trust
and digest the operator established independently.

## Consequences

- A web failure or password reset does not restart capture or streaming.
- Missing audio does not prevent the web interface from starting; missing
  networking does not delay capture startup.
- Permanent configuration errors require an operator correction and explicit
  media restart instead of producing an endless log loop.
- Atomic config/credential updates retain service-specific ownership because
  each writer owns its private parent directory.
- External recording destinations remain possible without granting the media
  process unrestricted writes to `/etc` or login homes.
- Upgrade and reverse-version rollback require possession of the corresponding
  verified artifact; the current installation never self-updates.
- Preserving users on uninstall avoids unsafe account deletion/reuse and makes
  reinstall ownership predictable, at the cost of leaving dormant system
  identities. The disposable GStreamer cache is removed.
- Loopback-only HTTP requires an SSH tunnel until a separately reviewed TLS/LAN
  deployment exists.
- Journald retention is not project-specific beyond rate limiting; operators
  with strict retention requirements must set a reviewed host policy.

## Rejected alternatives

### 2026-09-09 maintenance amendment

The original rejection of web-triggered systemd restart below is superseded for
three explicitly requested actions only: restart the media service, reboot,
and shut down. Neither web nor media gains root, sudo, or arbitrary systemd
access. A separate socket-activated root helper accepts an exact action name
from the web UID, independently verified with Linux `SO_PEERCRED`. It accepts
no command, arguments, unit names, filenames, or environment from its caller.

The HTTP boundary requires a session, CSRF token, current password and explicit
confirmation. The helper serializes actions with the installer lifecycle lock,
verifies recording finalization and clean media shutdown before a power action,
and fails closed on missing or uncertain state. Its socket has a separate
root-owned parent; the helper has no network, device or ambient capability
access. Browser reconnect feedback polls actual helper/media state and boot ID.

The web unit may now write its own private `/etc/tinypirelay/web` directory for
atomic password changes. All sessions are revoked after replacement, including
when replacement succeeded but directory durability is uncertain. Other private
directories remain inaccessible. File playback/download is mediated through
bounded media-owner requests; web receives no direct recording-directory access.

- **One root service:** rejected because the web parser would inherit ALSA,
  recording, config, and system-control authority.
- **One service account or flat writable config directory:** rejected because
  either process could atomically replace the other's private state.
- **Web-triggered systemd restart:** rejected because it would cross the local
  privilege boundary from an HTTP process.
- **Wait for `network-online.target`:** rejected because local capture and web
  control must start without receiver/network availability.
- **Strict media write allowlist for only the default recordings path:**
  rejected because supported operator-managed external mounts would fail.
- **Password argument/environment variable:** rejected because process lists,
  shell history, service metadata, and diagnostics can expose them.
- **Mutable `curl | sudo sh`:** rejected because neither script identity nor
  release content is pinned and reviewed before privilege.
- **Installer-driven receiver bridge or media conversion:** rejected as a scope
  change. Step 4 packages the Step 2/3 behavior unchanged.

## Validation and evidence boundary

The installer, CLI, service templates, security modes, interruption states,
upgrade/uninstall preservation, and boot/late-resource logic have automated and
isolated fixture checks. A separately retained finite physical run on a
Raspberry Pi Zero W Rev 1.1 demonstrated:

1. fresh installation and repeat no-op installation;
2. actual users/groups, modes, ownership, socket access, unit properties, and
   least-privilege sandboxing;
3. iMM-6C capture through the installed media service;
4. authenticated loopback-dashboard access through an SSH tunnel;
5. web-only restart with the media PID, capture generation, and capture
   continuity unchanged;
6. a finalized private FLAC recording decoded to end-of-stream; and
7. reboot auto-start/recovery with configuration, credential, installation
   state, and recording hashes preserved.

Those exact rows are **Hardware tested**, so Step 4 is complete for its finite
acceptance scope and Step 5 is ready. Interruption/resume, package/element/
service failures, upgrade/reverse-version rollback, preservation uninstall,
and explicit purge remain isolated-fixture results rather than physical-board
claims. Late audio/network return and active SRT were not exercised. The run is
not endurance, sustained-resource, thermal-headroom, or blanket Zero W
compatibility evidence; the overall board remains **Likely compatible /
untested**.

## References

- [Installation and lifecycle guide](../INSTALLATION.md)
- [Project specification](../PROJECT_SPEC.md)
- [Media service contract](../MEDIA_SERVICE.md)
- [Web control-plane contract](../WEB_CONTROL.md)
- [Raspberry Pi Zero W Step 4 physical report](../evidence/RPI_ZERO_W_STEP4_2026-08-20.md)
- [Step 4 milestone prompt](../../codex/STEP_4_INSTALLER_SYSTEM.md)
- [Prior architecture decision](0001-minimal-architecture.md)
- [Media-engine decision](0002-media-engine.md)
- [Web control-plane decision](0003-web-control-plane.md)
