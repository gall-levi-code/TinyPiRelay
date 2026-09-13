# TinyPiRelay installation and lifecycle

The `0.5.17-dev` development prerelease installs without a microphone, prepared
configuration, or terminal password creation. First access to a new device's
GUI creates its administrator account; subsequent access uses normal login.
This prerelease is not yet validated on a freshly imaged physical Pi. Historical
hardware results remain in [project state](PROJECT_STATE.md); general release
gates remain in [release readiness](RELEASE_READINESS.md).

## Fresh installation

1. Image Raspberry Pi OS Bookworm or Trixie. Configure a unique hostname, an
   administrator account, SSH and networking in Imager. For the current Pi 4
   evaluation rig, use Trixie 64-bit. No microphone is required.
2. Connect with the OS administrator account:

   ```sh
   ssh <admin-user>@<device-ip>
   ```

3. Follow [Download, verify and install](#download-verify-and-install) below.
   It uses the pinned
   [v0.5.17-dev release](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev)
   rather than a moving branch or `latest` URL. For a transferred archive, use
   the [manual/offline alternative](#manualoffline-alternative).
4. Open `http://<hostname>.local/` or `http://<device-ip>/`. Create a username
   and password, confirm the password, then sign in normally. Passwords require
   at least 12 characters. No default account, setup code or SSH password reuse.
5. Audio can wait. Later, attach the input and open **Audio → Refresh audio
   inputs**. Select a listed device and validated mode, save, then use the
   confirmed media-restart action to apply it. Detecting an unknown microphone
   does not approve its modes or streaming conversions.

The applying shell launcher refreshes APT metadata and can install missing Python
on supported Raspberry Pi OS. The installer checks platform, storage and package
plans before installing missing Python/PyGObject, ALSA and GStreamer dependencies,
FFmpeg for recovery, and Avahi for hostname discovery. No distribution upgrade,
package removal or blanket upgrade is requested. Network/APT failures stop the
workflow; resolve them and rerun the same artifact.

A read-only `plan` never installs packages or refreshes system APT indexes.
Stale indexes can therefore make a plan fail before the applying launcher
refreshes them and performs its own full preflight.

### Download, verify and install

Run these commands **on the Pi after SSH**, not in Windows PowerShell. They assume
the supported Raspberry Pi OS image from step 1 and an administrator with `sudo`.
If already logged in as root, omit `sudo`. The conditional first step installs
only the tools needed to download over HTTPS; the launcher handles the remaining
prerequisites automatically.

```sh
if ! command -v curl >/dev/null 2>&1 || [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
    sudo apt-get -o APT::Update::Error-Mode=any -o Acquire::Retries=2 \
        -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update &&
    sudo apt-get --yes --no-remove --no-upgrade --no-install-recommends install curl ca-certificates
fi &&
umask 077 &&
tpr_download_dir=$(mktemp -d /tmp/tinypirelay-download.XXXXXX) &&
cd "$tpr_download_dir" &&
curl --disable --fail --silent --show-error --location \
    --proto '=https' --proto-redir '=https' --connect-timeout 20 --max-time 300 \
    --output install-tinypirelay-0.5.17-dev.sh \
    https://github.com/gall-levi-code/TinyPiRelay/releases/download/v0.5.17-dev/install-tinypirelay-0.5.17-dev.sh
```

Do not continue after a download error. Review the saved script before granting
it root access, and compare this digest with **Installer SHA-256** in the
[release notes](https://github.com/gall-levi-code/TinyPiRelay/releases/tag/v0.5.17-dev):

```sh
sha256sum ./install-tinypirelay-0.5.17-dev.sh
```

Only after verification, run the installer in the same shell/directory:

```sh
sudo /bin/sh ./install-tinypirelay-0.5.17-dev.sh
```

No arguments means `install --apply`. It downloads the version-pinned archive,
checks its embedded SHA-256, validates archive paths and sizes, and installs it.
It never streams a download directly into a privileged shell. Optional `plan`
is read-only, but requires the bootstrap tools to exist already. For SSH-only
onboarding, append `install --web-access loopback --apply` instead.

The launcher and release-note checksums rely on trust in this GitHub repository,
its publisher account and HTTPS. A checksum alongside a compromised download
does not independently authenticate its publisher; no release-signing key is
provided. Do not substitute an unreviewed fork, branch URL or third-party script.

### Manual/offline alternative

Download the release's
[archive](https://github.com/gall-levi-code/TinyPiRelay/releases/download/v0.5.17-dev/tinypirelay-0.5.17-dev.tar.gz)
and [archive checksum](https://github.com/gall-levi-code/TinyPiRelay/releases/download/v0.5.17-dev/tinypirelay-0.5.17-dev.tar.gz.sha256).
After verifying the digest against trusted release metadata, transfer/extract
the archive and enter its directory on the Pi:

```sh
cd tinypirelay-0.5.17-dev
/bin/sh ./install.sh plan
sudo /bin/sh ./install.sh install --apply
```

No Git checkout or hardcoded workstation path is required. Transferring the
archive avoids the GitHub download, but the applying installer still refreshes
APT indexes. A device without Internet access needs a reachable trusted local
APT mirror; cached packages alone do not bypass that refresh.

## LAN access and first-account security

Fresh installations default to trusted-LAN **IPv4 HTTP port 80**. Avahi advertises
the OS hostname. Use unique hostnames; guest isolation, VLAN boundaries and clients
without mDNS support may require the IP fallback.

HTTP is not encrypted. Other parties able to observe or alter LAN traffic may
compromise passwords or sessions. The first person reaching an unconfigured
device can create its account. Set it up promptly on a trusted network; there is
no claim code by design. Host/Origin and request-metadata checks, bounded
authentication work and request sizes, and restricted LAN peers reduce other
risks, but do not provide encryption or identify the intended administrator.

The web process remains unprivileged. A LAN-specific systemd drop-in grants only
service-scoped `CAP_NET_BIND_SERVICE`, changes the bind to port 80, and restricts
source ranges. The Python interpreter receives no global capability.
`--lan-only` provides a second peer check. No router, firewall or UPnP changes
are made. Direct public Internet exposure is unsupported.

For SSH-only access from the beginning:

```sh
sudo /bin/sh ./install.sh install --web-access loopback --apply
```

From the workstation:

```sh
ssh -N -L 18082:127.0.0.1:8080 <admin-user>@<device-ip>
```

Open `http://127.0.0.1:18082/`. The same first-account screen is used, with
remote traffic protected by SSH. Reviewed HTTPS on standard port 443 remains an
additional deployment option; this installer does not provision certificate trust.

## Existing installations, upgrades and rollback

Accounts, media settings and recordings are preserved. A missing access-policy
file identifies a legacy localhost-only installation: upgrading does **not**
automatically expose it to the LAN.

From the new verified artifact, without `--config`:

```sh
sudo /bin/sh ./install.sh upgrade
sudo /bin/sh ./install.sh upgrade --apply
```

With the verified downloaded launcher, the equivalent is
`sudo /bin/sh ./install-tinypirelay-0.5.17-dev.sh upgrade --apply`.

To explicitly migrate an existing device to LAN access:

```sh
sudo /bin/sh ./install.sh upgrade --web-access lan
sudo /bin/sh ./install.sh upgrade --web-access lan --apply
```

Use `--web-access loopback` to return to SSH-only access. Port conflicts block
the operation; unrelated services are not stopped.

The same artifact safely repairs/resumes an interrupted operation. Changed code
needs a new version. Configuration is backed up before version transitions, and
failed activation attempts to restore the previous release and access integration.
Rollback uses the upgrade commands from the still-trusted earlier artifact.
Resolve an interrupted journal using its original target before switching versions.

Before rolling back to a **pre-0.5.17** artifact, use the current installer to
apply `--web-access loopback` first, ensure an account exists, and select a
validated microphone configuration. Older installers do not understand the LAN
drop-in, first-run marker or unconfigured-audio state. Preserve a backup before
such a rollback; do not leave a new LAN drop-in pointing at an older web server.

## Account persistence and password recovery

Only a fresh installation creates the private mode-0600
`/etc/tinypirelay/web/setup-pending` marker. It contains no code or password.
The setup endpoint validates credentials, publishes a salted scrypt record
atomically without overwriting a concurrent claimant, and closes registration.
Account creation is followed by normal sign-in, not automatic login.

Reboots/upgrades do not reopen registration. Existing credentials take precedence
over an interrupted marker. Corrupt or lost established credentials require an
explicit administrative reset:

```sh
sudo tinypirelay passwd
sudo tinypirelay passwd --username NEW_USERNAME
```

Reset requires a controlling terminal, writes as the web identity and restarts
only the web service. Existing sessions are invalidated. If restart fails, the
new credential is already committed. Never put passwords in command arguments,
environment variables, tracing or logs.

Automation may still pre-provision with `--credential-fd N` on install/upgrade.
The inherited descriptor must be at least 3, point to a single-link regular
mode-0600 file, and contain at most 4096 UTF-8 JSON bytes with exactly `username`
and `password`. Open it after elevation so sudo cannot close it. Use a secret
manager for creation/cleanup. It never replaces an existing account and is not
required for browser onboarding.

## Optional prepared audio configuration

Advanced fresh installs can use `--config /absolute/path/config.json`. Otherwise
the bundled example supplies paired-null capture and disabled streaming.
Selected modes still pass the exact capability gate; no new transport or blanket
USB-audio support is implied.

Recordings default to `/var/lib/tinypirelay/recordings`. External destinations
must be mounted and writable by `tinypirelay-media`; protected login homes are
not recording destinations. Unconfigured audio stays idle. A missing configured
input reports an audio failure while keeping control available. Reconnect and use
Start capture/restart; automatic USB-return acceptance remains unproven.

## Release packaging and pinned download

Source and releases use
[gall-levi-code/TinyPiRelay](https://github.com/gall-levi-code/TinyPiRelay).
A maintainer can build this version's deterministic archive, checksum and pinned
launcher using only the standard library. Build from a clean checkout of the
release tag: `.gitattributes` keeps source text LF while preserving the raw
bytes of checksum-linked evidence. Do not build a release from a dirty or
platform-renormalized working tree:

```sh
PYTHONPATH=src python3 -B packaging/build_release.py --output dist/github-v0.5.17-dev \
  --base-url https://github.com/gall-levi-code/TinyPiRelay/releases/download/v0.5.17-dev
```

Choose an unused output directory. Upload the generated archive, `.sha256` and
`install-tinypirelay-0.5.17-dev.sh` to the matching GitHub prerelease. The launcher
embeds the artifact URL and SHA-256; publish its own SHA-256 in the release notes
after building. Do not embed that launcher digest into files inside its archive:
the archive and launcher digests would depend on each other. The builder never
uploads, signs or replaces existing output. GitHub's automatically generated
source archives are not substitutes for the builder's installation bundle.

Run a separately verified launcher with
`sudo /bin/sh ./install-tinypirelay-VERSION.sh`; it defaults to `install --apply`
without configuration/path arguments. Pass `plan` for a read-only plan.
Keep development builds marked as prereleases until their acceptance gates pass;
hosting does not change the hardware-validation claims.

The reviewed generic bootstrap remains available:

```sh
/bin/sh ./bootstrap.sh VERSION HTTPS_ARTIFACT_URL SHA256 plan
sudo /bin/sh ./bootstrap.sh VERSION HTTPS_ARTIFACT_URL SHA256
```

It installs missing bootstrap tools only for an applying deployment on supported
Raspberry Pi OS as root. HTTPS downloads/redirects are bounded and digest-verified
before extraction. Links, special files, unsafe paths, oversized archives and
incorrect version roots are rejected. A checksum from the same untrusted location
proves consistency, not publisher authenticity: use a trusted metadata channel.
Installing a transferred archive still requires reachable APT repositories or a
trusted local mirror, as described above.

## Installed components

| Component/path | Purpose |
|---|---|
| `tinypirelay-media.service` | Unprivileged ALSA/media/control owner |
| `tinypirelay-web.service` | Separate unprivileged HTTP/authentication process |
| `tinypirelay-maintenance.socket` | Separate privileged helper for fixed confirmed actions |
| `/opt/tinypirelay/releases/<version>` | Immutable release |
| `/opt/tinypirelay/current` | Atomically selected active release |
| `/etc/tinypirelay/media/config.json` | Private media configuration |
| `/etc/tinypirelay/web/credential.json` | Private scrypt credential |
| `/etc/tinypirelay/web-access.json` | Root-owned durable LAN/loopback choice |
| `/run/tinypirelay/control.sock` | Group-protected local control socket |
| `/var/lib/tinypirelay/install-state.json` | Lifecycle journal |
| `/var/lib/tinypirelay/backups` | Configuration backups |

## Diagnostics and removal

```sh
tinypirelay status
sudo tinypirelay diagnostics
sudo tinypirelay config validate
sudo tinypirelay restart web
sudo tinypirelay restart media
sudo journalctl -u tinypirelay-web.service -n 200 --no-pager
sudo journalctl -u tinypirelay-media.service -n 200 --no-pager
```

Status reports the chosen URL. Diagnostics distinguishes pending account/audio
setup from software readiness. Review output before sharing; redaction does not
remove all private operational metadata.

Default uninstall preserves configuration, credentials, access choice, recordings,
backups, journal and service accounts:

```sh
sudo tinypirelay uninstall
sudo /bin/sh /opt/tinypirelay/current/install.sh uninstall --apply
```

Reinstall a verified artifact without `--config` to reuse preserved state.
A service-stop failure halts removal before deleting program files.
Permanent deletion is separate and requires explicit confirmation:

```sh
sudo /bin/sh /opt/tinypirelay/current/install.sh uninstall --apply --remove-data \
  --confirm-remove-data 'REMOVE TINYPIRELAY DATA'
```

That deletes `/etc/tinypirelay` and `/var/lib/tinypirelay`, including all
recordings/accounts. Service accounts are not automatically removed.
