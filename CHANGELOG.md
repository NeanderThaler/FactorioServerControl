# Changelog

All notable changes to Factorio Server Control are documented here.

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
- One-command Docker Compose deployment with persistent saves, mods, settings,
  credentials, and bounded Factorio log history.

### Changed

- Rebranded the project and web console as Factorio Server Control.
- Split reusable settings, custom instances, and generated upload slots.
- Save mod synchronization now disables built-in DLC mods absent from the save.