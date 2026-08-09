"""Synchronize exact mod versions recorded in a Factorio save.

The save-header decoding is adapted from OpenFactorioServerManager's
src/factorio/save.go and src/factorio/version.go; see ../THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import subprocess
import tempfile
import urllib.parse
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import BinaryIO, NamedTuple


class ModVersion(NamedTuple):
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


class SaveMod(NamedTuple):
    name: str
    version: ModVersion


class ModSyncError(RuntimeError):
    pass


_BUILTIN_MODS = {"base", "elevated-rails", "quality", "space-age"}
_MAX_STRING_BYTES = 1_048_576
_MAX_MODS = 10_000


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    value = stream.read(size)
    if len(value) != size:
        raise ModSyncError("Unexpected end of save header")
    return value


def _read_optimized_uint(stream: BinaryIO, game: tuple[int, ...], size: int) -> int:
    if game < (0, 14, 14, 0):
        return int.from_bytes(_read_exact(stream, size), "little")
    first = _read_exact(stream, 1)[0]
    if first != 0xFF:
        return first
    return int.from_bytes(_read_exact(stream, size), "little")


def _read_string(stream: BinaryIO, game: tuple[int, ...], optimized: bool) -> str:
    if game >= (0, 16, 0, 0) or optimized:
        length = _read_optimized_uint(stream, game, 4)
    else:
        length = struct.unpack("<I", _read_exact(stream, 4))[0]
    if length > _MAX_STRING_BYTES:
        raise ModSyncError(f"Save header string is unreasonably large: {length} bytes")
    try:
        return _read_exact(stream, length).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ModSyncError("Save header contains an invalid mod name") from exc


def _read_version48(stream: BinaryIO, game: tuple[int, ...]) -> ModVersion:
    return ModVersion(
        *(_read_optimized_uint(stream, game, 2) for _ in range(3))
    )


def read_save_mods(save_path: Path) -> list[SaveMod]:
    try:
        archive = zipfile.ZipFile(save_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModSyncError(f"Cannot open save {save_path.name}: {exc}") from exc

    with archive:
        candidates = [
            entry for entry in archive.infolist()
            if Path(entry.filename).name in {"level.dat", "level-init.dat"}
        ]
        if not candidates:
            raise ModSyncError("Save contains neither level.dat nor level-init.dat")

        save_header = min(
            candidates,
            key=lambda entry: Path(entry.filename).name != "level.dat",
        )
        with archive.open(save_header) as stream:
            game = struct.unpack("<4H", _read_exact(stream, 8))
            if game >= (0, 17, 0, 0):
                _read_exact(stream, 1)

            _read_string(stream, game, False)  # campaign
            _read_string(stream, game, False)  # level name
            _read_string(stream, game, False)  # base mod
            _read_exact(stream, 3)  # difficulty, finished, player won
            _read_string(stream, game, False)  # next level
            if game >= (0, 12, 0, 0):
                _read_exact(stream, 2)  # can continue, finished but continuing
            _read_exact(stream, 1)  # saving replay
            if game >= (0, 16, 0, 0):
                _read_exact(stream, 1)  # allow non-admin debug options
            _read_version48(stream, game)  # loaded-from version
            _read_exact(stream, 3)  # loaded-from build, allowed commands

            if game < (0, 13, 0, 42):
                raise ModSyncError("Factorio saves older than 0.13 are not supported")

            if game >= (2, 0, 0, 0):
                _read_exact(stream, 6)  # Factorio 2.0 header extension

            if game >= (0, 16, 0, 0):
                count = _read_optimized_uint(stream, game, 4)
            else:
                count = struct.unpack("<I", _read_exact(stream, 4))[0]
            if count > _MAX_MODS:
                raise ModSyncError(f"Save header declares too many mods: {count}")

            mods: list[SaveMod] = []
            for _ in range(count):
                name = _read_string(stream, game, True)
                version = _read_version48(stream, game)
                if game > (0, 15, 0, 91):
                    _read_exact(stream, 4)  # mod CRC
                mods.append(SaveMod(name, version))
            return mods


def _portal_release(mod: SaveMod, timeout: int) -> dict[str, str]:
    name = urllib.parse.quote(mod.name, safe="")
    request = urllib.request.Request(
        f"https://mods.factorio.com/api/mods/{name}/full",
        headers={"User-Agent": "Factorio-FUI-ModSync/1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            metadata = json.load(response)
    except (OSError, ValueError) as exc:
        raise ModSyncError(f"Could not retrieve metadata for {mod.name}: {exc}") from exc

    for release in metadata.get("releases", []):
        if release.get("version") == str(mod.version):
            return release
    raise ModSyncError(f"The portal has no {mod.name} {mod.version} release")


def _stage_release(
    mod: SaveMod,
    release: dict[str, str],
    stage: Path,
    username: str,
    token: str,
    timeout: int,
) -> str:
    filename = release.get("file_name", "")
    download_path = release.get("download_url", "")
    expected_sha1 = release.get("sha1", "")
    if Path(filename).name != filename or not filename.endswith(".zip"):
        raise ModSyncError(f"Portal returned an invalid filename for {mod.name}")
    if not download_path.startswith("/") or len(expected_sha1) != 40:
        raise ModSyncError(f"Portal returned invalid release data for {mod.name}")

    query = urllib.parse.urlencode({"username": username, "token": token})
    request = urllib.request.Request(
        f"https://mods.factorio.com{download_path}?{query}",
        headers={"User-Agent": "Factorio-FUI-ModSync/1"},
    )
    target = stage / filename
    digest = hashlib.sha1()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise ModSyncError(
            f"Portal download for {mod.name} {mod.version} returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise ModSyncError(f"Could not download {mod.name} {mod.version}") from exc
    if digest.hexdigest().lower() != expected_sha1.lower():
        target.unlink(missing_ok=True)
        raise ModSyncError(f"Checksum mismatch for {mod.name} {mod.version}")
    return filename


def _desired_mod_list(required: list[SaveMod], mods_dir: Path) -> dict[str, object]:
    enabled = {mod.name for mod in required}
    builtins = {"base": True}
    try:
        current = json.loads((mods_dir / "mod-list.json").read_text(encoding="utf-8"))
        for entry in current.get("mods", []):
            name = entry.get("name")
            if name in _BUILTIN_MODS:
                builtins[name] = bool(entry.get("enabled"))
    except (OSError, ValueError, AttributeError):
        pass
    builtins["base"] = True
    for name in enabled & _BUILTIN_MODS:
        builtins[name] = True

    entries = [
        {"name": name, "enabled": is_enabled}
        for name, is_enabled in sorted(builtins.items())
    ]
    entries.extend(
        {"name": mod.name, "enabled": True}
        for mod in required
        if mod.name not in _BUILTIN_MODS
    )
    return {"mods": entries}


def read_mod_inventory(mods_dir: Path) -> list[dict[str, object]]:
    """Read configured mods and installed versions without changing state."""
    try:
        configured = json.loads((mods_dir / "mod-list.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModSyncError(f"Cannot read mod list: {exc}") from exc

    entries = configured.get("mods") if isinstance(configured, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_MODS:
        raise ModSyncError("Mod list has an invalid mods array")

    versions: dict[str, str] = {}
    for archive_path in mods_dir.glob("*.zip"):
        try:
            with zipfile.ZipFile(archive_path) as archive:
                candidates = [
                    item for item in archive.infolist()
                    if Path(item.filename).name == "info.json"
                    and item.file_size <= _MAX_STRING_BYTES
                ]
                if not candidates:
                    continue
                metadata = json.loads(archive.read(min(candidates, key=lambda item: len(Path(item.filename).parts))))
                name = metadata.get("name")
                version = metadata.get("version")
                if isinstance(name, str) and isinstance(version, str):
                    versions[name] = version
        except (OSError, ValueError, zipfile.BadZipFile, KeyError, AttributeError):
            continue

    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "enabled": bool(entry.get("enabled", False)),
                "version": "Built-in" if name in _BUILTIN_MODS else versions.get(name),
                "installed": name in _BUILTIN_MODS or name in versions,
            }
        )
    return result


def synchronize_mods(
    saves_dir: Path,
    mods_dir: Path,
    server_settings_path: Path,
    timeout: int = 30,
) -> dict[str, object]:
    saves = sorted(saves_dir.glob("*.zip"), key=lambda path: path.stat().st_mtime)
    if not saves:
        raise ModSyncError("No Factorio save was found")
    save = saves[-1]
    required = read_save_mods(save)

    try:
        server_settings = json.loads(server_settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModSyncError(f"Cannot read server settings: {exc}") from exc
    username = server_settings.get("username", "")
    token = server_settings.get("token", "")
    portal_mods = [mod for mod in required if mod.name not in _BUILTIN_MODS]
    if portal_mods and (not username or not token):
        raise ModSyncError("Factorio username and token are required to download mods")

    downloaded: list[str] = []
    mods_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".mod-sync-", dir=mods_dir) as temp:
        stage = Path(temp)
        for mod in portal_mods:
            release = _portal_release(mod, timeout)
            downloaded.append(
                _stage_release(mod, release, stage, username, token, timeout)
            )

        mod_list = _desired_mod_list(required, mods_dir)
        (stage / "mod-list.json").write_text(
            json.dumps(mod_list, indent=2) + "\n", encoding="utf-8"
        )

        backup = stage / "backup"
        backup.mkdir()
        old_files = list(mods_dir.glob("*.zip"))
        if (mods_dir / "mod-list.json").exists():
            old_files.append(mods_dir / "mod-list.json")
        installed: list[Path] = []
        try:
            for current in old_files:
                os.replace(current, backup / current.name)
            for staged in list(stage.iterdir()):
                if staged == backup:
                    continue
                target = mods_dir / staged.name
                os.replace(staged, target)
                installed.append(target)
        except OSError as exc:
            for target in installed:
                target.unlink(missing_ok=True)
            for previous in backup.iterdir():
                os.replace(previous, mods_dir / previous.name)
            raise ModSyncError(f"Could not install synchronized mods: {exc}") from exc

    return {
        "save": save.name,
        "mods": [
            {"name": mod.name, "version": str(mod.version)} for mod in required
        ],
        "downloaded": downloaded,
    }


def run_privileged_sync(script_path: str, sudo_path: str, timeout: int) -> dict[str, object]:
    try:
        result = subprocess.run(
            [sudo_path, "--non-interactive", script_path],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ModSyncError(f"Mod synchronization timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "Mod synchronization failed"
        raise ModSyncError(message) from exc
    except ValueError as exc:
        raise ModSyncError("Mod synchronization returned an invalid response") from exc


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-save", type=Path)
    parser.add_argument("--saves", type=Path)
    parser.add_argument("--mods", type=Path)
    parser.add_argument("--server-settings", type=Path)
    args = parser.parse_args()
    try:
        if args.inspect_save:
            print(
                json.dumps(
                    [
                        {"name": mod.name, "version": str(mod.version)}
                        for mod in read_save_mods(args.inspect_save)
                    ]
                )
            )
            return
        if not args.saves or not args.mods or not args.server_settings:
            parser.error("--saves, --mods and --server-settings are required for sync")
        print(json.dumps(synchronize_mods(args.saves, args.mods, args.server_settings)))
    except ModSyncError as exc:
        parser.exit(1, f"mod sync failed: {exc}\n")


if __name__ == "__main__":
    main()