# Changelog

All notable changes to Factorio Server Control are documented here.

## Unreleased

### Added

- FUI **Factorio** tab: upgrade the shared headless engine to the latest stable
    release or roll back to the previous version. Downloads are verified against
    factorio.com SHA-256 checksums, each version lives in its own directory, and
    `/opt/factorio` is switched atomically through a symlink. Mods that would no
    longer load are updated, and only previously running servers are restarted.
- FUI **Update mods**: install the newest portal release of every enabled mod
    for the installed Factorio version, add missing required dependencies, and
    keep replaced archives in `state/mods-backup/`.

### Changed

- The Ansible role installs Factorio into versioned directories. With
    `factorio_version: stable` or `latest` it only installs when nothing is
    installed, so it never undoes an upgrade or rollback made in FUI; a pinned
    version is switched to exactly. Downloads are checksum-verified.
- Mod lists always name every built-in mod. Factorio 2.1's new `recycler` mod
    is enabled only with Quality or Space Age, because an unlisted built-in mod
    is enabled by default and would make a vanilla server require Space Age.

### Fixed

- `ansible-playbook --tags ...` skipped building the instance registry, which
    rendered an empty FUI instance list. The registry tasks are now tagged
    `always`.

## 1.1.0 - 2026-08-09

### Added

- One-command Docker Compose deployment with persistent saves, mods, settings,
    credentials, and bounded Factorio log history.
- Rootless Podman support on WSL with Docker-compatible API control and native
    Linux persistent storage.
- Automatic recovery of occupied upload slots after container or host restarts.
- Optional passwordless FUI mode when the administrator password is explicitly
    empty.

### Changed

- Container builds normalize executable line endings for Windows worktrees.
- FUI helper scripts are isolated from Factorio-writable persistent storage.
- Docker and Podman deployments retain logs, credentials, and saves across
    container replacement.

## 1.0 - 2026-08-09

### Added

- Ansible provisioning for Factorio, systemd, nginx, and the FUI web console.
- Multiple isolated Factorio instances sharing one read-only installation.
- Configurable dormant slots for creating servers from uploaded saves.
- Server controls, status, uptime, player activity, and journal-backed logs.
- Human-readable log events and current-run filtering.
- Save-aware mod inventory and transactional mod synchronization.
- Factorio-styled responsive dashboard and password login.
- Signed sessions, login throttling, fixed command allow-lists, and scoped sudo.
- Consolidated instance storage under `/srv/factorio/instances/<id>`.
- Local-only vault, custom-instance, key, save, scenario, and deployment files.
- Installation, operations, development, scenario, license, and attribution docs.
- Factorio Server Control SVG and PNG application icons.
- Upload progress with separate transfer and server-processing states.
- Secure deletion of uploaded servers to return their slots to the available pool.

### Changed

- Rebranded the project and web console as Factorio Server Control.
- Split reusable settings, custom instances, and generated upload slots.
- Save mod synchronization now disables built-in DLC mods absent from the save.