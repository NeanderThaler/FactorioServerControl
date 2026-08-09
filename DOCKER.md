# Docker Deployment

This deployment runs Factorio Server Control with Docker Compose on x86-64
hosts. Apple Silicon and other ARM hosts use Docker's `linux/amd64` emulation.
It starts:

- One regular Factorio server.
- Two dormant upload slots.
- The FastAPI FUI.
- An nginx reverse proxy.

All mutable configuration, saves, mods, generated credentials, and logs survive
container replacement under `docker/data/`.

## Requirements

- Docker Engine 24 or newer, or Docker Desktop.
- Docker Compose v2 (`docker compose`).
- About 2 GB of free disk space for images and the Factorio installation.
- UDP ports `34197-34199` and TCP port `8080` available by default.

## Start

From the repository root:

```bash
docker compose -f docker/compose.yml up --build -d
```

Open <http://localhost:8080/fui/>. On the first start, a random FUI password is
generated. Read it with:

```bash
docker compose -f docker/compose.yml exec fui \
  cat /data/fui/admin-password
```

The default server is created automatically. Upload slots remain stopped until
a save is uploaded through **New Server**.

## Configuration

Defaults work without configuration. To override them, copy
`docker/.env.example` to `docker/.env` and edit the local file. It is ignored by
Git. Start with the explicit environment file:

```bash
docker compose --env-file docker/.env -f docker/compose.yml up --build -d
```

Wrap values containing a literal `$` in single quotes in `docker/.env`.

Important values include:

| Variable | Default | Purpose |
| --- | --- | --- |
| `FSC_HTTP_PORT` | `8080` | Web console host port |
| `FACTORIO_PORT` | `34197` | Default server UDP host port |
| `FACTORIO_UPLOAD1_PORT` | `34198` | Upload slot 1 UDP host port |
| `FACTORIO_UPLOAD2_PORT` | `34199` | Upload slot 2 UDP host port |
| `FACTORIO_VERSION` | `stable` | Factorio image version |
| `FACTORIO_SERVER_PUBLIC` | `false` | List servers publicly |
| `FACTORIO_USERNAME` | empty | factorio.com username |
| `FACTORIO_TOKEN` | empty | factorio.com service token |
| `FUI_PASSWORD` | generated | Web console password |
| `FUI_SECRET_KEY` | generated | Session-signing key |

Public listing and automatic mod downloads require `FACTORIO_USERNAME` and
`FACTORIO_TOKEN`. Recreate containers after changing `.env`:

```bash
docker compose --env-file docker/.env -f docker/compose.yml up --build -d --force-recreate
```

Existing generated server settings are intentionally retained. To change an
existing server's listing credentials, update its
`docker/data/instances/<id>/config/server-settings.json` or recreate that
instance directory after backing up its saves.

## Persistent Data

The bind-mounted `docker/data/` directory has this layout:

```text
docker/data/
  fui/
    admin-password
    session-secret
    instances.json
  instances/
    default/
      config/
      state/saves/
      state/mods/
      state/factorio-current.log
      state/factorio-history.log
    upload1/
    upload2/
```

`docker compose down` removes containers and networks but leaves this directory
untouched. Do not use `docker compose down --volumes` as a substitute for a
backup policy, even though this deployment uses a bind mount rather than named
volumes.

To back up safely, stop the stack and copy `docker/data/`:

```bash
docker compose -f docker/compose.yml down
cp -a docker/data docker/data.backup
```

Start it again with the same one-command invocation.

## Operations

View all logs:

```bash
docker compose -f docker/compose.yml logs -f
```

Stop or remove the stack:

```bash
docker compose -f docker/compose.yml stop
docker compose -f docker/compose.yml down
```

Upgrade the repository and rebuild:

```bash
git pull
docker compose -f docker/compose.yml up --build -d
```

Add `--env-file docker/.env` when using the optional environment file.

The FUI controls the Factorio containers by translating its fixed
systemd/journald command interface to Docker operations. Server status, logs,
player activity, mod synchronization, uploads, resets, and upload-slot deletion
therefore use the same application code as the Ansible deployment.

## Security

The FUI container mounts `/var/run/docker.sock`. Access to this socket is
root-equivalent on the Docker host. Do not expose the FUI directly to the
internet without TLS and additional network controls, and do not run untrusted
code in the FUI container.

The web console still validates instance IDs, uses fixed container names, and
never turns request values into shell commands. The Docker socket nevertheless
has a larger blast radius than the narrowly scoped sudo policy used by the
native Ansible deployment. Use the Ansible deployment when host-level privilege
separation is the priority.

Keep `docker/.env` and `docker/data/` private. They contain credentials, session
secrets, saves, and downloaded mods and are excluded from Git.

## Troubleshooting

Check container state:

```bash
docker compose -f docker/compose.yml ps -a
```

An empty upload-slot container exits successfully by design. It starts when the
FUI installs a save. If a game server fails, inspect its logs directly:

```bash
docker compose -f docker/compose.yml logs factorio-default
docker compose -f docker/compose.yml logs factorio-upload1
```

If Docker reports a port conflict, change the corresponding host-port variable
in `docker/.env`. Container-side Factorio ports remain fixed.
