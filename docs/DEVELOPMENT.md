# Development

Contributor setup, validation, architecture, and security invariants for
Factorio Server Control.

## Repository Layout

```text
ansible/                   Provisioning and deployment
  site.yml                 Entry-point playbook
  group_vars/factorio.yml  Shared non-secret configuration
  group_vars/instances_custom.example.yml  Custom-server template
  group_vars/instances_uploads.yml Pre-provisioned upload slots
  vault.example.yml        Secret-key template
  roles/common             Base packages and SSH access
  roles/factorio           Factorio services and state
  roles/fui                Web console service
  roles/nginx              Reverse proxy
docs/                      Installation, operations, and development guides
fui/                       FastAPI application
  app/main.py              Routes
  app/config.py            Validated environment settings
  app/systemd.py           Fixed-argument systemd and journal access
  app/players.py           Journal-derived player sessions
  app/logs.py              Human-readable log events
  app/mods.py              Save parsing, mod synchronization, and mod updates
  app/engine.py            Factorio engine versions, upgrade, and rollback
  app/uploads.py           Transactional save upload handling
  app/templates/           Server-rendered interface
  tests/                   Unit tests
githooks/pre-commit        Enforces local-file and vault safety policy
mods/                      Repository-managed mod archives
scenarios/                 Local scenarios; only README.md is tracked
```

`keys/`, `saves/`, scenario implementations, `MYDEPLOYMENT.md`,
`ansible/vault.yml`, and `ansible/group_vars/instances_custom.yml` are
local-only and ignored by Git.

## Local FUI

From Linux or WSL:

```bash
cd fui
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
FUI_ROOT_PATH="" \
FUI_PASSWORD="local-dev-password" \
FUI_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  .venv/bin/uvicorn app.main:app --reload
```

`FUI_PASSWORD` must be empty or at least 12 characters. An empty value disables
authentication, so the dashboard and API open directly without a session.
`FUI_SECRET_KEY` must be at least 32 characters. Service controls fail off-host unless matching
`factorio@<id>.service` units exist locally.

## Tests And Checks

Run the FUI tests from the repository root:

```bash
PYTHONPATH=fui python3 -m unittest discover -s fui/tests -v
python3 -m compileall -q fui/app
```

Validate Ansible from its directory:

```bash
ansible-playbook site.yml --syntax-check
```

Optional lint tools can live outside the repository:

```bash
python3 -m venv /tmp/lintenv
/tmp/lintenv/bin/pip install ansible-lint yamllint
export ANSIBLE_COLLECTIONS_PATH=/usr/lib/python3/dist-packages
/tmp/lintenv/bin/ansible-lint --offline site.yml roles
/tmp/lintenv/bin/yamllint -d relaxed site.yml group_vars roles
```

Rendered host files are validated before activation:

| File | Validation |
| --- | --- |
| `/etc/sudoers.d/fui` | `visudo -cf %s` through template `validate` |
| nginx configuration | `nginx -t` in the reload handler |
| systemd services | `systemd-analyze verify <unit>` |

## Architecture

Factorio binaries are shared from `/opt/factorio`. Instance data is normalized
under `/srv/factorio/instances/<id>/{state,config,runtime}`. Each instance has
isolated saves, mods, runtime FIFO, UDP port, and systemd unit. The
`factorio@.service` template runs as the unprivileged `factorio` account with
`ProtectSystem=strict` and instance-specific `ReadWritePaths` for state and
runtime only.

`site.yml` derives the three paths from each source record's `base_dir`.
Optional `legacy_state_dir` and `legacy_config_dir` values drive a guarded,
one-time migration: the unit is stopped and the old directory is moved only
when the destination does not exist. If both exist, deployment fails rather
than merging data.

FUI is a FastAPI application behind nginx. Its instance allow-list is rendered
from the same `factorio_instances` data used by the Factorio role, keeping unit
names and filesystem paths out of browser-controlled input.

### API Surface

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/fui/login` | GET, POST | Sign in; redirects when authentication is disabled |
| `/fui/logout` | POST | Clear session |
| `/fui/` | GET | Dashboard |
| `/fui/api/servers` | GET | Instance summaries |
| `/fui/api/servers/{id}/status` | GET | Unit state and uptime |
| `/fui/api/servers/{id}/logs` | GET | Journal entries |
| `/fui/api/servers/{id}/players` | GET | Player presence and sessions |
| `/fui/api/servers/{id}/mods` | GET | Mod inventory |
| `/fui/api/servers/{id}/service/{action}` | POST | Start, stop, or restart |
| `/fui/api/servers/{id}/reset` | POST | Archive and reset a map |
| `/fui/api/servers/{id}/mods/sync` | POST | Synchronize save-required mods |
| `/fui/api/servers/{id}/mods/update` | POST | Update mods for the installed Factorio |
| `/fui/api/engine` | GET | Installed, previous, and latest stable Factorio |
| `/fui/api/engine/{upgrade,rollback}` | POST | Change the shared Factorio version |
| `/fui/api/upload-slots` | GET | Available upload slots |
| `/fui/api/upload-slots/{id}` | POST | Install an uploaded save |

### Player Presence

Factorio has no presence query API. FUI reconstructs sessions from journal
`[JOIN]` and `[LEAVE]` lines. Run starts and shutdown markers close open
sessions because crashes and restarts do not emit leave events. A stopped unit
also closes all remaining sessions at its final event.

Patterns are anchored to Factorio's timestamp and event-tag position, so chat
cannot impersonate a join. Player names are rendered with `textContent`.
Totals are lower bounds when journal rotation removes old entries.

### Logs

The default log query is scoped to the unit's current
`_SYSTEMD_INVOCATION_ID`. This is exact across fast restarts and avoids deleting
unrelated system journal history. The full unit history remains available with
`current_run=false`.

An idle server auto-pauses, so a quiet log and delayed autosave can be normal
when no players are connected.

### Mod Synchronization

FUI reads the save header from `level.dat`, or from the `level.dat0..N` chunks
that large maps write instead (each chunk is a separate zlib stream). It falls
back to `level-init.dat` only when neither exists, because that file records
the mods the map was created with. FUI then resolves exact mod versions through
the Factorio Mod Portal, verifies SHA-1 values, and transactionally replaces
the selected instance's mods. Built-in mods are not downloaded. The previous
set is restored if installation fails.

`manage_mods: true` gives ownership to Ansible; `manage_mods: false` allows FUI
or an operator to own the instance mod directory. Do not combine both flows.

Mod lists always name every built-in mod (`base`, `elevated-rails`, `quality`,
`recycler`, `space-age`). Factorio enables unlisted built-in mods, and 2.1 added
the Space Age-only `recycler`, so it is enabled only with Quality or Space Age.
The privileged mod helpers, the Ansible mod list, and the Docker seed also add
every mod bundled under `<factorio>/data/`, so a future built-in mod is never
requested from the Mod Portal.

### Mod Updates

`update_mods` picks, for each enabled mod, the newest portal release whose
`info.json` `factorio_version` equals the engine's major.minor version.
Factorio 2.1 refuses to load mods built for 2.0. Required dependencies,
including `~` and `+` prefixed ones, are resolved recursively and added to
`mod-list.json`; `!`, `?`, and `(?)` dependencies are ignored. If any mod has no
compatible release, nothing is changed. Replaced archives go to the sibling
`mods-backup/` directory, outside the mods directory where Factorio would try to
load them. The engine upgrade runs the same code with `only_incompatible`, which
leaves mods that already match the new version untouched.

### Engine Versions

`/opt/factorio` is a symlink to `/opt/factorio-<version>`, and
`/opt/factorio.previous` points at the rollback target. `engine.py` runs as a
script under the root helper `/etc/fui/factorio-engine.sh`, which sudo permits
only with the literal argument `upgrade` or `rollback`. The target version comes
from factorio.com (`stable.headless`) or the previous symlink, never from the
request. A plain `/opt/factorio` directory from older deployments is renamed to
its versioned name on the first switch. Docker deployments leave
`FUI_ENGINE_SCRIPT` empty and `FUI_MOD_UPDATE_ENABLED` unset, which hides both
features because the engine is part of the image there.

### Uploads

Upload slots are provisioned declaratively. FUI validates the ZIP, installs it
transactionally, updates the slot settings, synchronizes mods, and starts the
unit. FUI owns mutable upload files; Ansible retains ownership of service
definitions, helpers, and sudo policy.

## Security Invariants

- uvicorn binds only to `127.0.0.1`.
- Subprocesses use fixed argument arrays and allow-listed actions.
- Browser input cannot select arbitrary units, paths, commands, or arguments.
- Sudo permits only generated unit actions, argument-free instance helpers, and
  the engine helper with exactly `upgrade` or `rollback`.
- `fui.service` must not use `NoNewPrivileges`; it would prevent required sudo.
- State-changing routes require `X-Requested-With: fui`.
- FUI reads journals through `systemd-journal` group membership, not sudo.
- Sessions contain an expiring HMAC token, never the login password.
- Login attempts receive exponential per-IP throttling.

## Secrets And Git Policy

`ansible/vault.yml` is local-only and must never be committed, even when
encrypted. The root `.gitignore` excludes it, and the pre-commit hook rejects
a forced addition. Enable the repository hook once per clone:

```bash
git config core.hooksPath githooks
```

Edit and rotate encrypted values with:

```bash
ansible-vault edit ansible/vault.yml
ansible-vault rekey ansible/vault.yml
```

Keep the vault password in a password manager. Never commit private keys,
passwords, tokens, save archives, scenarios, server backups, or private
deployment notes. Any plaintext secret that enters Git history must be rotated;
deleting the current file is not sufficient.
