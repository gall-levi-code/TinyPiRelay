# Step 4 — Installer and System Integration

## Goal

Deliver a safe, reviewable, idempotent Raspberry Pi OS installation and lifecycle with least-privilege systemd services and recovery tools.

## Required work

- Read the shared documents, current repository/install state, threat decisions, and `docs/PROJECT_STATE.md`.
- Implement a repository-local installer with a read-only preflight before mutations. Detect OS/release, architecture/userland, model/resources, storage, repository/package reachability, ALSA, required GStreamer elements, port conflicts, existing installation, and controlling TTY.
- Print the planned changes and fail clearly on unsupported or unresolved requirements. Never perform a distribution upgrade.
- Use distro packages and the Step 1 package/element map. Separate package installation from post-install element validation.
- Create dedicated least-privilege user/group ownership, directories, restrictive secret/config permissions, log policy, and hardened systemd units for independent media and web services.
- Prompt for initial web credentials via `/dev/tty` before installation completes. Never use defaults, command-line password arguments, pipe input, or logs. Provide a documented safe noninteractive secret-file/FD mechanism only if needed.
- Implement `tinypirelay` status/diagnostics and `sudo tinypirelay passwd`. Include configuration validation and useful service/log pointers.
- Implement safe upgrade with version awareness, config backup/migration, rollback guidance, and no blind overwrite.
- Implement uninstall that preserves recordings and configuration by default and requires explicit confirmation for data removal.
- Create a pinned HTTPS bootstrap that downloads a versioned repository artifact/installer and verifies the strongest practical release integrity. Do not pipe a mutable default branch directly into a privileged shell.
- Write exact SSH install, GUI access/discovery, password recovery, update, troubleshooting, offline/manual install, and uninstall instructions.

## Required scenario checks

- Dry-run/preflight on supported and unsupported fixture hosts; no mutations occur in preflight.
- Fresh install, repeated install, interrupted install then resume, upgrade from a fixture version, and uninstall with data preservation.
- Missing `/dev/tty`, package/repository failure, missing post-install GStreamer element, port conflict, existing config, weak/invalid credentials, and service-start failure.
- Inspect units and filesystem permissions; confirm neither service runs as root and secrets are absent from process arguments/logs.
- Reboot/service-order simulation and recovery when audio/network is initially unavailable.

## Safety boundary

Do not run the installer against the development host or a real Pi, enable services, install packages, publish releases, or test `curl | bash` against a remote URL without explicit authorization. Use isolated fixtures/containers where safe and provide physical-host commands for review.

## Definition of done

- Installer/lifecycle checks are repeatable and idempotent in the available test environment.
- A failed step leaves a recoverable state and an actionable message.
- Credentials and service privileges meet the specification.
- Documentation matches the actual commands, and `docs/PROJECT_STATE.md` is ready for Step 5.
