#!/usr/bin/env python3
"""Initialize persistent Docker-mode FUI configuration and fixed helpers."""

from __future__ import annotations

import json
import os
import secrets
import stat
import subprocess
import time
from pathlib import Path

DATA = Path("/data")
FUI = DATA / "fui"
HELPERS = Path("/run/fui/helpers")
INSTANCES = (
    ("default", "Docker Factorio Server", "FUI_DEFAULT_PORT", 34197, False),
    ("upload1", "Upload Slot 1", "FUI_UPLOAD1_PORT", 34198, True),
    ("upload2", "Upload Slot 2", "FUI_UPLOAD2_PORT", 34199, True),
)
SYSTEMCTL = "/usr/local/bin/systemctl"


def persistent_value(env_name: str, filename: str, factory) -> str:
    configured = os.environ.get(env_name, "")
    path = FUI / filename
    if configured:
        path.write_text(configured + "\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return configured
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = factory()
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return value


def write_helper(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def main() -> None:
    FUI.mkdir(parents=True, exist_ok=True)
    HELPERS.mkdir(parents=True, exist_ok=True)
    HELPERS.chmod(stat.S_IRWXU)
    password_path = FUI / "admin-password"
    password_existed = password_path.exists()
    password_configured = bool(os.environ.get("FUI_PASSWORD", ""))
    password = persistent_value("FUI_PASSWORD", "admin-password", lambda: secrets.token_urlsafe(18))
    secret = persistent_value("FUI_SECRET_KEY", "session-secret", lambda: secrets.token_hex(32))
    os.environ["FUI_PASSWORD"] = password
    os.environ["FUI_SECRET_KEY"] = secret

    registry = []
    for instance_id, name, port_env, default_port, upload_slot in INSTANCES:
        port = int(os.environ.get(port_env, str(default_port)))
        root = DATA / "instances" / instance_id
        saves = root / "state" / "saves"
        mods = root / "state" / "mods"
        config = root / "config"
        config.mkdir(parents=True, exist_ok=True)

        deadline = time.monotonic() + 30
        while not (config / "server-settings.json").exists() and time.monotonic() < deadline:
            time.sleep(0.25)

        helper_dir = HELPERS / instance_id
        helper_dir.mkdir(mode=stat.S_IRWXU, exist_ok=True)
        sync = helper_dir / "docker-sync-mods.sh"
        delete = helper_dir / "factorio-delete-upload.sh"
        reset = helper_dir / "docker-reset-map.sh"
        service = f"factorio@{instance_id}.service"

        write_helper(
            sync,
            rf'''#!/bin/sh
set -eu
was_running=false
if {SYSTEMCTL} is-active --quiet {service}; then
    was_running=true
    {SYSTEMCTL} stop {service}
fi
restart() {{ [ "$was_running" = false ] || {SYSTEMCTL} start {service}; }}
trap restart EXIT
result=$(/usr/local/bin/python /opt/fui/app/mods.py --saves {saves} --mods {mods} --server-settings {config / "server-settings.json"})
printf '%s\n' "$result"
''',
        )
        write_helper(
            delete,
            rf'''#!/bin/sh
set -eu
{SYSTEMCTL} stop {service} || true
find {saves} -maxdepth 1 -type f -name '*.zip' -delete
find {mods} -maxdepth 1 -type f \( -name '*.zip' -o -name mod-list.json \) -delete
rm -f {root / "state" / "factorio-current.log"} {root / "state" / "factorio-history.log"}
''',
        )
        write_helper(
            reset,
            rf'''#!/bin/sh
set -eu
{SYSTEMCTL} stop {service} || true
stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive={saves}/archive/$stamp
mkdir -p "$archive"
find {saves} -maxdepth 1 -type f -name '*.zip' -exec mv {{}} "$archive"/ \;
{SYSTEMCTL} start {service}
''',
        )

        registry.append(
            {
                "id": instance_id,
                "name": name,
                "service_name": service,
                "reset_script_path": str(reset),
                "mod_sync_script_path": str(sync),
                "mod_sync_enabled": True,
                "mods_dir": str(mods),
                "upload_slot": upload_slot,
                "saves_dir": str(saves),
                "server_settings_path": str(config / "server-settings.json"),
                "port": port,
                "reset_enabled": not upload_slot,
            }
        )

    (FUI / "instances.json").write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")

    # Empty upload containers exit normally. Restart occupied slots after a
    # Docker daemon or host restart so their persisted saves come back online.
    for instance_id, _, _, _, upload_slot in INSTANCES:
        saves = DATA / "instances" / instance_id / "state" / "saves"
        if upload_slot and any(saves.glob("*.zip")):
            subprocess.run(
                [SYSTEMCTL, "start", f"factorio@{instance_id}.service"],
                check=False,
            )
    if not password_existed and not password_configured:
        print(f"Generated FUI password: {password}", flush=True)
    print("FUI password is stored in /data/fui/admin-password", flush=True)

    os.execvp(
        "uvicorn",
        [
            "uvicorn",
            "app.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            "8000",
            "--proxy-headers",
            "--forwarded-allow-ips=*",
        ],
    )


if __name__ == "__main__":
    main()
