"""Safe, FUI-owned save uploads into pre-provisioned server slots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import BinaryIO

from .mods import ModSyncError, read_save_mods

MAX_SAVE_BYTES = 512 * 1024 * 1024


class SaveUploadError(RuntimeError):
    pass


def validate_server_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80 or any(ord(character) < 32 for character in name):
        raise SaveUploadError("Server name must contain 1 to 80 printable characters")
    return name


def validate_server_description(value: str) -> str:
    description = value.strip()
    if (
        not description
        or len(description) > 200
        or any(ord(character) < 32 for character in description)
    ):
        raise SaveUploadError(
            "Server description must contain 1 to 200 printable characters"
        )
    return description


def install_save(source: BinaryIO, saves_dir: Path) -> Path:
    target = saves_dir / "uploaded.zip"
    temporary = saves_dir / ".uploaded.zip.tmp"
    if target.exists() or any(saves_dir.glob("*.zip")):
        raise SaveUploadError("This upload slot is already in use")

    total = 0
    temporary.unlink(missing_ok=True)
    try:
        with temporary.open("xb") as output:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_SAVE_BYTES:
                    raise SaveUploadError("Save exceeds the 512 MiB upload limit")
                output.write(chunk)
        if total == 0:
            raise SaveUploadError("Uploaded save is empty")
        read_save_mods(temporary)
        os.chmod(temporary, 0o660)
        os.replace(temporary, target)
        return target
    except ModSyncError as exc:
        raise SaveUploadError(f"Invalid Factorio save: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def update_server_settings(
    path: Path, name: str, description: str, public: bool
) -> bytes:
    try:
        original = path.read_bytes()
        settings = json.loads(original)
    except (OSError, ValueError) as exc:
        raise SaveUploadError(f"Cannot read slot settings: {exc}") from exc
    if not isinstance(settings, dict):
        raise SaveUploadError("Slot settings are not a JSON object")

    visibility = settings.get("visibility")
    if not isinstance(visibility, dict):
        visibility = {}
        settings["visibility"] = visibility
    settings["name"] = validate_server_name(name)
    settings["description"] = validate_server_description(description)
    visibility["public"] = public
    visibility["lan"] = True

    try:
        with path.open("w", encoding="utf-8") as output:
            output.write(json.dumps(settings, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError as exc:
        raise SaveUploadError(f"Cannot update slot settings: {exc}") from exc
    return original