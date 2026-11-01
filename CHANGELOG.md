# Changelog

All notable changes to Factorio Server Control are documented here.

## Unreleased

### Changed

- Factorio units stop restarting after five failed starts within five minutes,
    so a server that cannot start no longer floods the journal and slows down
    the FUI server list.

### Fixed

- Players whose connection dropped or timed out stayed online in FUI, because
    Factorio prints no `[LEAVE]` line for them. Sessions now also end when the
    player's network peer is removed.
- **Reset map** is no longer offered for instances without a scenario. Resetting
    such an instance archived its only save and left it unable to start.

## 1.2.0 - 2026-10-31

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

- Mod sync read the stale `level-init.dat` of saves whose level data is split
    into `level.dat0..N` chunks, installing the mods the map was created with
    instead of the current ones. Chunked level data is now read first.
- Built-in mods are discovered from the Factorio `data/` directory, so a newly
    bundled mod such as `recycler` is no longer requested from the Mod Portal,
    and the Ansible and Docker mod lists name every bundled mod explicitly.
- A pinned `factorio_version` change now installs and activates that version;
    previously the unpack step was skipped because the install already existed.
- `test_uses_journal_time_for_shutdown` no longer depends on the local time
    zone.
- The nginx location for engine upgrades redirected `GET /fui/api/engine` in a
    loop, so the **Factorio** tab showed a network error.
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