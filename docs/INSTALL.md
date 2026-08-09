# Installation

Prepare a new Debian or Ubuntu host and its Ansible control machine here.
Server configuration, deployment, and maintenance continue in
[OPERATIONS.md](OPERATIONS.md).

## 1. Prepare The Machines

Requirements:

- Debian or Ubuntu server with systemd, Python 3, and SSH access.
- Linux or WSL control machine with OpenSSH and Ansible.
- A remote root or sudo-capable account.
- One UDP port per Factorio instance and one TCP port for nginx.
- factorio.com credentials only when public listing is enabled.

Install control-machine tools:

```bash
sudo apt update
sudo apt install -y ansible openssh-client python3
ansible-galaxy collection install ansible.posix community.general
```

Clone and enter the repository:

```bash
git clone <repository-url> FactorioServer
cd FactorioServer/ansible
export ANSIBLE_CONFIG="$PWD/ansible.cfg"
```

Set `ANSIBLE_CONFIG` explicitly under world-writable mounts such as WSL's
`/mnt/c`, where Ansible otherwise ignores the configuration file.

## 2. Configure SSH

Create a dedicated key when needed:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_factorio -C factorio-server
ssh-copy-id -i ~/.ssh/id_ed25519_factorio.pub <admin>@<server-address>
```

Copy the example and edit the local inventory:

```bash
cp inventory.example.ini inventory.ini
```

```ini
[factorio]
factorio-01 ansible_host=<server-address>

[factorio:vars]
ansible_user=<admin>
ansible_port=22
ansible_ssh_private_key_file=~/.ssh/id_ed25519_factorio
ansible_python_interpreter=/usr/bin/python3
```

Use root only when intentional. For password-protected sudo, add
`--ask-become-pass` to playbook commands. Verify access and accept the host key:

```bash
ssh -i ~/.ssh/id_ed25519_factorio <admin>@<server-address>
ansible factorio -m ping
```

Additional public keys may be placed in the ignored local `keys/` directory.
Never place private keys in the repository.

## 3. Create The Local Vault

`ansible/vault.yml` is local-only, ignored by Git, and rejected by the
pre-commit hook even if force-added.

```bash
cp vault.example.yml vault.yml
ansible-vault encrypt vault.yml
ansible-vault edit vault.yml
```

Set a FUI password of at least 12 characters and a signing key of at least 32
characters. Generate the signing key with:

```bash
python3 -c 'import secrets; print(secrets.token_hex(32))'
```

Public servers also need the factorio.com username and token from
<https://factorio.com/profile>. Keep the vault password in a password manager.
Confirm the vault is excluded:

```bash
git check-ignore -v ansible/vault.yml
```

Continue with [OPERATIONS.md](OPERATIONS.md) to configure instances, validate,
deploy, verify, and maintain the server.
