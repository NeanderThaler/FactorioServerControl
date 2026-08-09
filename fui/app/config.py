"""Runtime configuration, sourced from environment variables."""

from __future__ import annotations

import os
import json
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# Only ever used to build fixed argv lists, but validated anyway so a bad
# environment file cannot smuggle extra arguments into systemctl.
_UNIT_PATTERN = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
_INSTANCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass(frozen=True)
class ServerInstance:
    id: str
    name: str
    service_name: str
    reset_script_path: str
    mod_sync_script_path: str
    mod_sync_enabled: bool
    mods_dir: str
    upload_slot: bool
    saves_dir: str
    server_settings_path: str
    port: int
    reset_enabled: bool


@dataclass(frozen=True)
class Settings:
    service_name: str
    systemctl_path: str
    journalctl_path: str
    sudo_path: str
    reset_script_path: str
    root_path: str
    command_timeout: int
    reset_timeout: int
    mod_sync_timeout: int
    password: str
    secret_key: str
    cookie_name: str
    cookie_secure: bool
    session_ttl: int
    instances: tuple[ServerInstance, ...]

    def instance(self, instance_id: str) -> ServerInstance:
        for instance in self.instances:
            if instance.id == instance_id:
                return instance
        raise KeyError(instance_id)


def _require_executable(env_var: str, default_name: str) -> str:
    configured = os.environ.get(env_var)
    resolved = configured or shutil.which(default_name)
    if not resolved:
        raise RuntimeError(f"Could not locate {default_name}; set {env_var}.")
    if not os.path.isabs(resolved):
        raise RuntimeError(f"{env_var} must be an absolute path, got {resolved!r}.")
    return resolved


# The script is deployed by the factorio role, so it may legitimately be absent
# while this app starts; only the path shape is checked here.
def _require_absolute_path(env_var: str, default: str) -> str:
    value = os.environ.get(env_var) or default
    if not os.path.isabs(value):
        raise RuntimeError(f"{env_var} must be an absolute path, got {value!r}.")
    return value


def _require_secret(env_var: str, min_length: int) -> str:
    value = os.environ.get(env_var, "")
    if len(value) < min_length:
        raise RuntimeError(f"{env_var} must be set and at least {min_length} characters.")
    return value


def _load_password() -> str:
    if "FUI_PASSWORD" not in os.environ:
        raise RuntimeError("FUI_PASSWORD must be set; use an empty value to disable authentication.")
    value = os.environ["FUI_PASSWORD"]
    if value and len(value) < 12:
        raise RuntimeError("FUI_PASSWORD must be empty or at least 12 characters.")
    return value


def _load_instances() -> tuple[ServerInstance, ...]:
    path = os.environ.get("FUI_INSTANCES_FILE")
    if path:
        if not os.path.isabs(path):
            raise RuntimeError(f"FUI_INSTANCES_FILE must be absolute, got {path!r}.")
        try:
            raw_instances = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Could not load FUI instances: {exc}") from exc
    else:
        raw_instances = [
            {
                "id": "default",
                "name": "Factorio",
                "service_name": os.environ.get("FUI_SERVICE_NAME", "factorio.service"),
                "reset_script_path": os.environ.get(
                    "FUI_RESET_SCRIPT", "/etc/factorio/factorio-reset.sh"
                ),
                "mod_sync_script_path": os.environ.get(
                    "FUI_MOD_SYNC_SCRIPT", "/etc/factorio/factorio-sync-mods.sh"
                ),
                "mod_sync_enabled": True,
                "mods_dir": "/var/lib/factorio/mods",
                "upload_slot": False,
                "saves_dir": "/var/lib/factorio/saves",
                "server_settings_path": "/etc/factorio/server-settings.json",
                "port": 34197,
                "reset_enabled": True,
            }
        ]

    if not isinstance(raw_instances, list) or not raw_instances:
        raise RuntimeError("FUI instances must be a non-empty JSON array.")

    instances: list[ServerInstance] = []
    seen: set[str] = set()
    for raw in raw_instances:
        if not isinstance(raw, dict):
            raise RuntimeError("Every FUI instance must be a JSON object.")
        instance_id = str(raw.get("id", ""))
        name = str(raw.get("name", ""))
        service_name = str(raw.get("service_name", ""))
        reset_path = str(raw.get("reset_script_path", ""))
        sync_path = str(raw.get("mod_sync_script_path", ""))
        sync_enabled = raw.get("mod_sync_enabled", True)
        mods_dir = str(raw.get("mods_dir", ""))
        upload_slot = raw.get("upload_slot", False)
        saves_dir = str(raw.get("saves_dir", ""))
        server_settings_path = str(raw.get("server_settings_path", ""))
        port = raw.get("port")
        reset_enabled = raw.get("reset_enabled", True)
        if not _INSTANCE_ID_PATTERN.fullmatch(instance_id) or instance_id in seen:
            raise RuntimeError(f"Invalid or duplicate FUI instance id: {instance_id!r}")
        if not name or len(name) > 80:
            raise RuntimeError(f"Invalid name for FUI instance {instance_id!r}")
        if not _UNIT_PATTERN.fullmatch(service_name):
            raise RuntimeError(f"Invalid service for FUI instance {instance_id!r}")
        if not all(
            os.path.isabs(path)
            for path in (reset_path, sync_path, mods_dir, saves_dir, server_settings_path)
        ):
            raise RuntimeError(f"Helper paths for FUI instance {instance_id!r} must be absolute")
        if not isinstance(sync_enabled, bool):
            raise RuntimeError(f"Invalid mod sync setting for FUI instance {instance_id!r}")
        if not isinstance(upload_slot, bool) or not isinstance(reset_enabled, bool):
            raise RuntimeError(f"Invalid capability setting for FUI instance {instance_id!r}")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            raise RuntimeError(f"Invalid port for FUI instance {instance_id!r}")
        seen.add(instance_id)
        instances.append(
            ServerInstance(
                instance_id,
                name,
                service_name,
                reset_path,
                sync_path,
                sync_enabled,
                mods_dir,
                upload_slot,
                saves_dir,
                server_settings_path,
                port,
                reset_enabled,
            )
        )
    return tuple(instances)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    instances = _load_instances()
    default_instance = instances[0]
    return Settings(
        service_name=default_instance.service_name,
        systemctl_path=_require_executable("FUI_SYSTEMCTL_PATH", "systemctl"),
        journalctl_path=_require_executable("FUI_JOURNALCTL_PATH", "journalctl"),
        sudo_path=_require_executable("FUI_SUDO_PATH", "sudo"),
        reset_script_path=default_instance.reset_script_path,
        root_path=os.environ.get("FUI_ROOT_PATH", "/fui").rstrip("/"),
        command_timeout=int(os.environ.get("FUI_COMMAND_TIMEOUT", "30")),
        reset_timeout=int(os.environ.get("FUI_RESET_TIMEOUT", "180")),
        mod_sync_timeout=int(os.environ.get("FUI_MOD_SYNC_TIMEOUT", "900")),
        password=_load_password(),
        secret_key=_require_secret("FUI_SECRET_KEY", 32),
        cookie_name=os.environ.get("FUI_COOKIE_NAME", "fui_session"),
        cookie_secure=os.environ.get("FUI_COOKIE_SECURE", "false").lower() == "true",
        session_ttl=int(os.environ.get("FUI_SESSION_TTL", str(7 * 24 * 3600))),
        instances=instances,
    )
