"""Synchronize exact mod versions recorded in a Factorio save, or update mods
to the newest releases for the installed Factorio version.

The save-header decoding is adapted from OpenFactorioServerManager's
src/factorio/save.go and src/factorio/version.go; see ../THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.parse
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path
from typing import BinaryIO, NamedTuple, cast


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


_BUILTIN_MODS = {"base", "elevated-rails", "quality", "recycler", "space-age"}
_MAX_STRING_BYTES = 1_048_576
_MAX_MODS = 10_000
_LEVEL_CHUNK = re.compile(r"level\.dat(\d+)")
_MOD_BACKUPS_KEPT = 5
_USER_AGENT = "Factorio-FUI-ModSync/1"
_GAME_VERSION = re.compile(r"^(\d{1,4})\.(\d{1,4})\.(\d{1,5})$")
_MOD_VERSION = re.compile(r"^\d{1,5}\.\d{1,5}\.\d{1,5}$")
# Dependency prefixes: "!" incompatible, "?" and "(?)" optional, "~" required
# without load-order effect, "+" required. Everything else is required.
_DEPENDENCY = re.compile(
    r"^\s*(?P<prefix>!|\?|\(\?\)|~|\+)?\s*(?P<name>[^<>=]+?)\s*"
    r"(?:(?:<=|>=|<|>|=)\s*\S+)?\s*$"
)


def discover_builtin_mods(install_dir: Path) -> set[str]:
    """Names of the mods bundled under install_dir/data, excluding core."""
    try:
        return {path.parent.name for path in (install_dir / "data").glob("*/info.json")} - {"core"}
    except OSError:
        return set()


def _apply_builtin_dependencies(builtins: dict[str, bool]) -> None:
    # Factorio 2.1 split the recycler out of quality, and an unlisted built-in
    # mod is enabled by default. It must therefore be listed explicitly, and
    # it is required exactly when quality or Space Age is enabled.
    builtins["recycler"] = bool(builtins.get("quality") or builtins.get("space-age"))


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


class _ChunkedLevel:
    """Read level.dat0..N, each an independent zlib stream, as one stream.

    Chunks are inflated lazily, so parsing the header touches only chunk 0.
    """

    def __init__(self, archive: zipfile.ZipFile, chunks: list[zipfile.ZipInfo]) -> None:
        self._archive = archive
        self._chunks = iter(chunks)
        self._inflater: zlib._Decompress | None = None
        self._pending = b""
        self._buffer = b""

    def read(self, size: int) -> bytes:
        while len(self._buffer) < size:
            if self._inflater is None or self._inflater.eof:
                chunk = next(self._chunks, None)
                if chunk is None:
                    break
                self._inflater = zlib.decompressobj()
                self._pending = self._archive.read(chunk)
            try:
                data = self._inflater.decompress(self._pending, size - len(self._buffer))
            except zlib.error as exc:
                raise ModSyncError("Save contains a corrupt level.dat chunk") from exc
            self._pending = self._inflater.unconsumed_tail
            if not data and not self._inflater.eof:
                raise ModSyncError("Save contains a truncated level.dat chunk")
            self._buffer += data
        value, self._buffer = self._buffer[:size], self._buffer[size:]
        return value


def read_save_mods(save_path: Path) -> list[SaveMod]:
    try:
        archive = zipfile.ZipFile(save_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModSyncError(f"Cannot open save {save_path.name}: {exc}") from exc

    # level-init.dat holds the version and mods the map was created with, so
    # it is only a fallback when no current level data exists. Large maps
    # replace level.dat with level.dat0..N chunks.
    with archive:
        entries = {Path(entry.filename).name: entry for entry in archive.infolist()}
        if "level.dat" in entries:
            with archive.open(entries["level.dat"]) as stream:
                return _parse_save_header(stream)

        chunks = sorted(
            (
                (int(match.group(1)), entry)
                for name, entry in entries.items()
                if (match := _LEVEL_CHUNK.fullmatch(name))
            ),
            key=lambda item: item[0],
        )
        if chunks:
            if [index for index, _ in chunks] != list(range(len(chunks))):
                raise ModSyncError("Save is missing level.dat chunks")
            return _parse_save_header(
                cast(BinaryIO, _ChunkedLevel(archive, [entry for _, entry in chunks]))
            )

        if "level-init.dat" in entries:
            with archive.open(entries["level-init.dat"]) as stream:
                return _parse_save_header(stream)
        raise ModSyncError("Save contains no level.dat, level.dat0 or level-init.dat")


def _parse_save_header(stream: BinaryIO) -> list[SaveMod]:
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


def _portal_metadata(name: str, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://mods.factorio.com/api/mods/{urllib.parse.quote(name, safe='')}/full",
        headers={"User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            metadata = json.load(response)
    except (OSError, ValueError) as exc:
        raise ModSyncError(f"Could not retrieve metadata for {name}: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ModSyncError(f"Portal returned invalid metadata for {name}")
    return metadata


def _portal_release(mod: SaveMod, timeout: int) -> dict[str, str]:
    metadata = _portal_metadata(mod.name, timeout)
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
        headers={"User-Agent": _USER_AGENT},
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


def _desired_mod_list(required: list[SaveMod], _mods_dir: Path) -> dict[str, object]:
    enabled = {mod.name for mod in required}
    builtins = {
        name: name == "base" or name in enabled
        for name in _BUILTIN_MODS
    }
    _apply_builtin_dependencies(builtins)

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


class InstalledMod(NamedTuple):
    version: str
    factorio_version: str
    dependencies: tuple[str, ...]
    paths: tuple[Path, ...]


def _version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def _installed_archives(mods_dir: Path) -> dict[str, InstalledMod]:
    """Map each mod name to its newest archive; all archive paths are kept."""
    found: dict[str, list[tuple[str, str, tuple[str, ...], Path]]] = {}
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
                game = metadata.get("factorio_version", "")
                dependencies = metadata.get("dependencies", [])
                if not isinstance(dependencies, list):
                    dependencies = []
                if isinstance(name, str) and isinstance(version, str):
                    found.setdefault(name, []).append(
                        (
                            version,
                            game if isinstance(game, str) else "",
                            tuple(str(item) for item in dependencies),
                            archive_path,
                        )
                    )
        except (OSError, ValueError, zipfile.BadZipFile, KeyError, AttributeError):
            continue

    result: dict[str, InstalledMod] = {}
    for name, archives in found.items():
        newest = max(archives, key=lambda item: _version_key(item[0]))
        result[name] = InstalledMod(
            newest[0],
            newest[1],
            newest[2],
            tuple(sorted(item[3] for item in archives)),
        )
    return result


def _read_mod_list(mods_dir: Path) -> list[dict[str, object]]:
    try:
        configured = json.loads((mods_dir / "mod-list.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModSyncError(f"Cannot read mod list: {exc}") from exc

    entries = configured.get("mods") if isinstance(configured, dict) else None
    if not isinstance(entries, list) or len(entries) > _MAX_MODS:
        raise ModSyncError("Mod list has an invalid mods array")
    return entries


def read_mod_inventory(mods_dir: Path) -> list[dict[str, object]]:
    """Read configured mods and installed versions without changing state."""
    entries = _read_mod_list(mods_dir)
    versions = {name: mod.version for name, mod in _installed_archives(mods_dir).items()}
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


def read_engine_version(install_dir: Path) -> str | None:
    """Return the Factorio version installed at install_dir, or None."""
    try:
        metadata = json.loads(
            (install_dir / "data" / "base" / "info.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    version = metadata.get("version") if isinstance(metadata, dict) else None
    return version if isinstance(version, str) and _GAME_VERSION.fullmatch(version) else None


def game_series(factorio_version: str) -> str:
    """Map "2.1.23" to "2.1", the value mods declare as factorio_version."""
    match = _GAME_VERSION.fullmatch(factorio_version)
    if not match:
        raise ModSyncError(f"Invalid Factorio version: {factorio_version!r}")
    return f"{int(match.group(1))}.{int(match.group(2))}"


def _required_dependencies(dependencies: tuple[str, ...] | list[str]) -> list[str]:
    names: list[str] = []
    for dependency in dependencies:
        match = _DEPENDENCY.match(str(dependency))
        if not match or match.group("prefix") in {"!", "?", "(?)"}:
            continue
        name = match.group("name").strip()
        if name and name not in _BUILTIN_MODS:
            names.append(name)
    return names


def _newest_release(metadata: dict[str, object], series: str) -> dict[str, object] | None:
    releases = metadata.get("releases", [])
    candidates = [
        release for release in (releases if isinstance(releases, list) else [])
        if isinstance(release, dict)
        and isinstance(release.get("info_json"), dict)
        and release["info_json"].get("factorio_version") == series
        and _MOD_VERSION.fullmatch(str(release.get("version", "")))
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda release: _version_key(str(release["version"])))


def _complete_builtin_entries(entries: list[dict[str, object]]) -> list[dict[str, object]]:
    """List every built-in mod explicitly so none is enabled implicitly."""
    builtins = {
        str(entry["name"]): bool(entry.get("enabled", False))
        for entry in entries
        if entry.get("name") in _BUILTIN_MODS
    }
    for name in _BUILTIN_MODS:
        builtins.setdefault(name, name == "base")
    _apply_builtin_dependencies(builtins)
    others = [entry for entry in entries if entry.get("name") not in _BUILTIN_MODS]
    return [
        {"name": name, "enabled": builtins[name]} for name in sorted(builtins)
    ] + others


def ensure_builtin_entries(mods_dir: Path) -> bool:
    """Rewrite mod-list.json when a built-in mod is unlisted; True if changed."""
    path = mods_dir / "mod-list.json"
    if not path.exists():
        return False
    entries = [entry for entry in _read_mod_list(mods_dir) if isinstance(entry, dict)]
    completed = _complete_builtin_entries(entries)
    current = {str(entry.get("name")): bool(entry.get("enabled", False)) for entry in entries}
    if all(current.get(str(entry["name"])) == entry["enabled"] for entry in completed):
        return False
    path.write_text(json.dumps({"mods": completed}, indent=2) + "\n", encoding="utf-8")
    return True


def _prune_backups(backup_root: Path) -> None:
    backups = sorted(path for path in backup_root.iterdir() if path.is_dir())
    for stale in backups[:-_MOD_BACKUPS_KEPT]:
        shutil.rmtree(stale, ignore_errors=True)


def update_mods(
    mods_dir: Path,
    server_settings_path: Path,
    factorio_version: str,
    only_incompatible: bool = False,
    timeout: int = 30,
) -> dict[str, object]:
    """Install the newest portal release of every enabled mod for factorio_version.

    With only_incompatible, mods already built for the same Factorio series
    stay untouched; only those that would not load are replaced. Missing
    required dependencies are added and enabled. Replaced archives are moved
    to a sibling mods-backup directory, never inside the mods directory, where
    Factorio would try to load them.
    """
    series = game_series(factorio_version)
    entries = [entry for entry in _read_mod_list(mods_dir) if isinstance(entry, dict)]
    installed = _installed_archives(mods_dir)
    queue = [
        str(entry["name"]) for entry in entries
        if entry.get("enabled") and isinstance(entry.get("name"), str)
        and entry["name"] not in _BUILTIN_MODS
    ]

    plan: dict[str, dict[str, object]] = {}
    unchanged: list[dict[str, str]] = []
    unavailable: list[str] = []
    required: list[str] = []
    seen: set[str] = set()
    while queue:
        name = queue.pop(0)
        if name in seen or name in _BUILTIN_MODS:
            continue
        seen.add(name)
        required.append(name)
        current = installed.get(name)
        if only_incompatible and current and current.factorio_version == series:
            unchanged.append({"name": name, "version": current.version})
            queue.extend(_required_dependencies(current.dependencies))
            continue
        release = _newest_release(_portal_metadata(name, timeout), series)
        if release is None:
            unavailable.append(name)
            continue
        info = release["info_json"]
        dependencies = info.get("dependencies", []) if isinstance(info, dict) else []
        queue.extend(_required_dependencies(dependencies if isinstance(dependencies, list) else []))
        if current and current.version == release["version"]:
            unchanged.append({"name": name, "version": current.version})
        else:
            plan[name] = release

    if unavailable:
        raise ModSyncError(
            f"No Factorio {series} release on the mod portal for: "
            + ", ".join(sorted(unavailable))
        )

    configured = {str(entry.get("name")) for entry in entries}
    new_entries = []
    for entry in entries:
        if entry.get("name") in seen and not entry.get("enabled"):
            entry = {**entry, "enabled": True}
        new_entries.append(entry)
    added = [name for name in required if name not in configured]
    new_entries.extend({"name": name, "enabled": True} for name in added)
    new_entries = _complete_builtin_entries(new_entries)

    updated = [
        {
            "name": name,
            "from": installed[name].version if name in installed else None,
            "to": str(release["version"]),
        }
        for name, release in plan.items()
    ]
    result: dict[str, object] = {
        "factorio_version": factorio_version,
        "updated": updated,
        "added": added,
        "unchanged": unchanged,
        "backup": None,
    }
    old_list = json.dumps({"mods": entries}, indent=2) + "\n"
    new_list = json.dumps({"mods": new_entries}, indent=2) + "\n"
    if not plan and new_list == old_list:
        return result

    username = token = ""
    if plan:
        try:
            server_settings = json.loads(server_settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ModSyncError(f"Cannot read server settings: {exc}") from exc
        username = server_settings.get("username", "")
        token = server_settings.get("token", "")
        if not username or not token:
            raise ModSyncError("Factorio username and token are required to download mods")

    backup_root = mods_dir.parent / "mods-backup"
    backup = backup_root / dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory(prefix=".mod-update-", dir=mods_dir) as temp:
        stage = Path(temp)
        for name, release in plan.items():
            version = ModVersion(*(int(part) for part in str(release["version"]).split(".")))
            _stage_release(SaveMod(name, version), release, stage, username, token, timeout)

        backup.mkdir(parents=True, exist_ok=True)
        list_path = mods_dir / "mod-list.json"
        shutil.copy2(list_path, backup / "mod-list.json")
        moved: list[Path] = []
        installed_now: list[Path] = []
        try:
            for name in plan:
                for archive in installed[name].paths if name in installed else ():
                    os.replace(archive, backup / archive.name)
                    moved.append(archive)
            for staged in list(stage.iterdir()):
                target = mods_dir / staged.name
                os.replace(staged, target)
                installed_now.append(target)
            list_path.write_text(new_list, encoding="utf-8")
        except OSError as exc:
            for target in installed_now:
                target.unlink(missing_ok=True)
            for archive in moved:
                os.replace(backup / archive.name, archive)
            shutil.copy2(backup / "mod-list.json", list_path)
            raise ModSyncError(f"Could not install updated mods: {exc}") from exc

    _prune_backups(backup_root)
    result["backup"] = str(backup)
    return result


def run_privileged_sync(script_path: str, sudo_path: str, timeout: int) -> dict[str, object]:
    return run_privileged_helper(script_path, sudo_path, timeout, "Mod synchronization")


def run_privileged_helper(
    script_path: str,
    sudo_path: str,
    timeout: int,
    label: str,
    *arguments: str,
) -> dict[str, object]:
    """Run a fixed root-owned helper through sudo and parse its JSON result."""
    try:
        result = subprocess.run(
            [sudo_path, "--non-interactive", script_path, *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ModSyncError(f"{label} timed out after {timeout}s") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or f"{label} failed"
        raise ModSyncError(message) from exc
    except ValueError as exc:
        raise ModSyncError(f"{label} returned an invalid response") from exc


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--inspect-save", type=Path)
    parser.add_argument("--saves", type=Path)
    parser.add_argument("--mods", type=Path)
    parser.add_argument("--server-settings", type=Path)
    parser.add_argument("--update", choices=("all", "incompatible"))
    parser.add_argument("--factorio-install-dir", type=Path, default=Path("/opt/factorio"))
    args = parser.parse_args()
    # A bundled mod unknown to this release must never be fetched from the portal.
    _BUILTIN_MODS.update(discover_builtin_mods(args.factorio_install_dir))
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
        if args.update:
            if not args.mods or not args.server_settings:
                parser.error("--mods and --server-settings are required for --update")
            version = read_engine_version(args.factorio_install_dir)
            if not version:
                raise ModSyncError(f"No Factorio installation found at {args.factorio_install_dir}")
            print(
                json.dumps(
                    update_mods(
                        args.mods,
                        args.server_settings,
                        version,
                        only_incompatible=args.update == "incompatible",
                    )
                )
            )
            return
        if not args.saves or not args.mods or not args.server_settings:
            parser.error("--saves, --mods and --server-settings are required for sync")
        print(json.dumps(synchronize_mods(args.saves, args.mods, args.server_settings)))
    except ModSyncError as exc:
        parser.exit(1, f"mod {'update' if args.update else 'sync'} failed: {exc}\n")


if __name__ == "__main__":
    main()