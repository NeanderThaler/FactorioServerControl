# Configuration And Operations

This guide continues [INSTALL.md](INSTALL.md) after prerequisites, SSH access,
and the local encrypted vault are ready.

## Configure Instances

Shared settings are in `ansible/group_vars/factorio.yml`. Every instance is
placed under:

```text
/srv/factorio/instances/<id>/
  state/    saves, mods, scenarios, and script output
  config/   generated Factorio settings and fixed helper scripts
  runtime/  console FIFO
```

Copy the tracked example to the ignored local configuration file:

```bash
cd ansible
cp group_vars/instances_custom.example.yml group_vars/instances_custom.yml
```

Then configure at least one server suitable for the host:

```yaml
factorio_custom_instances:
  - id: default
    name: My Factorio Server
    base_dir: "{{ factorio_instances_base_dir }}/default"
    port: 34197
    save_name: default
    scenario_name: ""
    manage_mods: false
    mod_sync_enabled: true
    description: A Factorio multiplayer server
    max_players: 10
    public: false
    autosave_interval: 20
    autosave_slots: 5
    game_password: "{{ vault_factorio_game_password | default('', true) }}"
```

Ansible derives `state_dir`, `config_dir`, and `runtime_dir` from `base_dir`.
IDs, base directories, and UDP ports must be unique across custom instances and
upload slots. `legacy_state_dir` and `legacy_config_dir` are only for migrating
an existing installation; omit them on a new server.

### Existing Save

Place a save ZIP in the ignored local `saves/` directory and add:

```yaml
initial_save: "{{ playbook_dir }}/../saves/my-world.zip"
```

It is copied only when the destination save does not exist.

### Scenario

Place scenario source in an ignored local child directory under `scenarios/`
and set `scenario_name` to that exact directory name. Use `scenario_name: ""`
for freeplay. Only `scenarios/README.md` is tracked, so scenario implementations
must be supplied separately.

### Upload Slots

Edit `ansible/group_vars/instances_uploads.yml`:

```yaml
factorio_upload_slot_count: 2
factorio_upload_slot_first_port: 34199
```

Set the count to `0` to disable upload capacity. Slots use consecutive UDP
ports. The playbook rejects invalid ranges and collisions. Without factorio.com
credentials, also set `factorio_upload_slot_public: false`.

## Configure The Web Endpoint

Review the nginx and FUI settings in `ansible/group_vars/factorio.yml`:

```yaml
fui_base_path: /fui
fui_cookie_secure: false
nginx_listen_port: 80
nginx_server_name: "{{ ansible_host }}"
nginx_default_server: false
nginx_remove_default_site: false
```

The defaults expose FUI at `http://<server-address>/fui/` and preserve existing
nginx sites. On a dedicated host, the default-site options may be enabled after
reviewing the existing nginx configuration.

For internet-facing deployments, add DNS and HTTPS, then set
`fui_cookie_secure: true`. Secure cookies do not work over plain HTTP.

## Review Firewall Settings

Firewall management defaults to off:

```yaml
common_manage_firewall: false
```

Before enabling it, inventory every listening service with `ss -tulpn` and
extend `firewall_allowed_rules`. The role adds each generated Factorio UDP port.
Cloud firewalls or security groups must allow the same ports separately.

## Validate

From `ansible/`:

```bash
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-playbook site.yml --syntax-check
ansible-playbook site.yml --ask-vault-pass --check --diff
```

Add `--ask-become-pass` when required. Resolve assertions and path conflicts
before deployment. Migration fails if both an old and new instance directory
exist, preventing accidental merges.

Optional application checks from the repository root:

```bash
PYTHONPATH=fui python3 -m unittest discover -s fui/tests -v
python3 -m compileall -q fui/app
```

## Deploy

```bash
cd ansible
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
ansible-playbook site.yml --ask-vault-pass
```

The first run downloads Factorio, creates accounts and instance folders,
installs systemd units, deploys FUI, configures nginx, and starts custom
instances. Empty upload slots remain stopped until FUI installs a save.
Re-running the playbook applies configuration and application updates.

## Verify

On the target host:

```bash
systemctl status factorio-servers.target --no-pager
systemctl status factorio@default.service --no-pager
systemctl status fui.service --no-pager
systemctl status nginx.service --no-pager
ss -lunp | grep 34197
```

From the control machine:

```bash
curl -I http://<server-address>/fui/login
```

Open `http://<server-address>/fui/`, sign in, and verify:

1. Configured servers appear in the selector.
2. Status, uptime, players, and logs load.
3. Controls affect only the selected instance.
4. The Mods view shows the current inventory.
5. New Server appears when an unused upload slot exists.

Connect Factorio to `<server-address>:<instance-port>`. For public instances,
verify the configured name appears in the multiplayer browser.

## Back Up And Upgrade

Back up `/srv/factorio/instances/`, especially each `state/saves/` directory,
before Factorio, mod, or scenario changes. Securely back up the local encrypted
vault and ignored save/scenario sources separately because Git does not contain
them.

To pin or upgrade Factorio, change `factorio_version` in
`ansible/group_vars/factorio.yml`, run check mode, then deploy the full playbook.
