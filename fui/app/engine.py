"""Report, install, and switch the Factorio headless engine.

The web console only reads versions. Installing, upgrading, and rolling back
run as root through a fixed helper that invokes this file as a script:

    engine.py upgrade|rollback --instances FILE --install-dir /opt/factorio ...

Each version lives in its own directory next to the install directory
(/opt/factorio-2.1.23) and the install directory is a symlink to the active
one, so a switch is a single atomic rename and the previous version stays
available for rollback through the "<install-dir>.previous" symlink.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:
    from .mods import ModSyncError, ensure_builtin_entries, read_engine_version, update_mods
except ImportError:  # executed as a script by the root helper
    from mods import ModSyncError, ensure_builtin_entries, read_engine_version, update_mods

LATEST_RELEASES_URL = "https://factorio.com/api/latest-releases"
DOWNLOAD_URL = "https://factorio.com/get-download/{version}/headless/linux64"
CHECKSUMS_URL = "https://factorio.com/download/sha256sums/"
_USER_AGENT = "Factorio-FUI-Engine/1"
_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,5}$")
_LATEST_TTL = 600

_latest_lock = threading.Lock()
_latest_cache: tuple[float, str] | None = None


class EngineError(RuntimeError):
    pass


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def previous_link(install_dir: Path) -> Path:
    return install_dir.with_name(install_dir.name + ".previous")


def version_dir(install_dir: Path, version: str) -> Path:
    return install_dir.with_name(f"{install_dir.name}-{version}")


def _open(url: str, timeout: int):
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - fixed https URLs


def latest_stable(timeout: int = 15) -> str:
    """Return the newest stable headless version, cached for ten minutes."""
    global _latest_cache
    with _latest_lock:
        if _latest_cache and time.monotonic() - _latest_cache[0] < _LATEST_TTL:
            return _latest_cache[1]
        try:
            with _open(LATEST_RELEASES_URL, timeout) as response:
                releases = json.load(response)
            version = releases["stable"]["headless"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise EngineError(f"Could not query the latest Factorio release: {exc}") from exc
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise EngineError(f"factorio.com returned an invalid version: {version!r}")
        _latest_cache = (time.monotonic(), version)
        return version


def engine_status(install_dir: Path) -> dict[str, object]:
    return {
        "installed": read_engine_version(install_dir),
        "previous": read_engine_version(previous_link(install_dir))
        if previous_link(install_dir).exists()
        else None,
    }


def _expected_sha256(version: str, timeout: int) -> str:
    filename = f"factorio-headless_linux_{version}.tar.xz"
    try:
        with _open(CHECKSUMS_URL, timeout) as response:
            listing = response.read().decode("utf-8", errors="replace")
    except OSError as exc:
        raise EngineError(f"Could not download Factorio checksums: {exc}") from exc
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == filename and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            return parts[0]
    raise EngineError(f"factorio.com publishes no checksum for {filename}")


def _download(version: str, cache_dir: Path, timeout: int) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"factorio-headless_linux_{version}.tar.xz"
    expected = _expected_sha256(version, timeout)
    if archive.exists() and hashlib.sha256(archive.read_bytes()).hexdigest() == expected:
        return archive

    partial = archive.with_name(archive.name + ".part")
    digest = hashlib.sha256()
    try:
        with _open(DOWNLOAD_URL.format(version=version), timeout) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                output.write(chunk)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        raise EngineError(f"Could not download Factorio {version}: {exc}") from exc
    if digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise EngineError(f"Checksum mismatch for Factorio {version}")
    os.replace(partial, archive)
    for stale in cache_dir.glob("factorio-headless_linux_*.tar.xz"):
        if stale != archive:
            stale.unlink(missing_ok=True)
    return archive


def install_version(version: str, install_dir: Path, cache_dir: Path, timeout: int = 120) -> Path:
    """Make version available in its own directory without activating it."""
    if not _VERSION.fullmatch(version):
        raise EngineError(f"Invalid Factorio version: {version!r}")
    target = version_dir(install_dir, version)
    if read_engine_version(target) == version and (target / "bin" / "x64" / "factorio").is_file():
        return target

    archive = _download(version, cache_dir, timeout)
    staging = install_dir.with_name(f".{install_dir.name}-{version}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(mode=0o755)
    try:
        subprocess.run(
            ["tar", "-xJf", str(archive), "-C", str(staging), "--strip-components=1", "--no-same-owner"],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        shutil.rmtree(staging, ignore_errors=True)
        raise EngineError(f"Could not unpack Factorio {version}: {exc.stderr.strip()}") from exc
    if read_engine_version(staging) != version or not (staging / "bin" / "x64" / "factorio").is_file():
        shutil.rmtree(staging, ignore_errors=True)
        raise EngineError(f"The Factorio {version} archive does not contain that version")
    shutil.rmtree(target, ignore_errors=True)
    os.replace(staging, target)
    return target


def _replace_symlink(link: Path, target: Path) -> None:
    temporary = link.with_name(f".{link.name}.new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    os.replace(temporary, link)


def switch_to(target: Path, install_dir: Path) -> None:
    """Point install_dir at target and remember the replaced version."""
    old_target: Path | None = None
    if install_dir.is_symlink():
        old_target = install_dir.resolve()
    elif install_dir.is_dir():
        # A plain directory from an older deployment becomes a versioned one.
        old_version = read_engine_version(install_dir)
        if not old_version:
            raise EngineError(f"{install_dir} is not a recognizable Factorio installation")
        old_target = version_dir(install_dir, old_version)
        if old_target.exists():
            raise EngineError(f"Cannot migrate {install_dir}: {old_target} already exists")
        os.replace(install_dir, old_target)
    _replace_symlink(install_dir, target)
    if old_target and old_target != target.resolve():
        _replace_symlink(previous_link(install_dir), old_target)


def prune_versions(install_dir: Path) -> list[str]:
    """Delete version directories that are neither active nor the rollback target."""
    keep = {install_dir.resolve()}
    if previous_link(install_dir).exists():
        keep.add(previous_link(install_dir).resolve())
    removed: list[str] = []
    for candidate in install_dir.parent.glob(f"{install_dir.name}-*"):
        version = candidate.name[len(install_dir.name) + 1:]
        if _VERSION.fullmatch(version) and candidate.is_dir() and not candidate.is_symlink() \
                and candidate.resolve() not in keep:
            shutil.rmtree(candidate)
            removed.append(version)
    return removed


def _systemctl(systemctl: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([systemctl, *arguments], capture_output=True, text=True, check=False)


def _chown_mods(mods_dir: Path, owner: str) -> None:
    user, _, group = owner.partition(":")
    for path in [*mods_dir.glob("*.zip"), mods_dir / "mod-list.json"]:
        if path.exists():
            shutil.chown(path, user, group or None)
            path.chmod(0o640)


def change_engine(
    action: str,
    instances: list[dict[str, object]],
    install_dir: Path,
    cache_dir: Path,
    systemctl: str,
    owner: str,
) -> dict[str, object]:
    current = read_engine_version(install_dir)
    if action == "upgrade":
        target_version = latest_stable()
        if current == target_version:
            return {"changed": False, "from": current, "to": current, "instances": []}
        if current and _version_key(target_version) < _version_key(current):
            raise EngineError(
                f"The latest stable release {target_version} is older than the installed {current}"
            )
    elif action == "rollback":
        target_version = read_engine_version(previous_link(install_dir)) or ""
        if not target_version:
            raise EngineError("No previous Factorio version is available for rollback")
    else:
        raise EngineError(f"Unsupported action: {action!r}")

    # Everything that can fail on the network happens before any server stops.
    target = (
        previous_link(install_dir).resolve()
        if action == "rollback"
        else install_version(target_version, install_dir, cache_dir)
    )

    services = [str(instance["service_name"]) for instance in instances]
    running = [
        service for service in services
        if _systemctl(systemctl, "is-active", "--quiet", service).returncode == 0
    ]
    if running:
        stopped = _systemctl(systemctl, "stop", *running)
        if stopped.returncode != 0:
            _systemctl(systemctl, "start", *running)
            raise EngineError(stopped.stderr.strip() or "Could not stop the Factorio servers")

    try:
        switch_to(target, install_dir)
    except (OSError, EngineError):
        if running:
            _systemctl(systemctl, "start", *running)
        raise

    results: list[dict[str, object]] = []
    for instance in instances:
        service = str(instance["service_name"])
        mods_dir = Path(str(instance["mods_dir"]))
        entry: dict[str, object] = {"id": instance["id"], "was_running": service in running}
        mods_ready = True
        try:
            if instance.get("mod_sync_enabled", True) and (mods_dir / "mod-list.json").exists():
                entry["mods"] = update_mods(
                    mods_dir,
                    Path(str(instance["server_settings_path"])),
                    target_version,
                    only_incompatible=True,
                )
            else:
                ensure_builtin_entries(mods_dir)
            if mods_dir.is_dir():
                _chown_mods(mods_dir, owner)
        except (ModSyncError, OSError) as exc:
            mods_ready = False
            entry["error"] = str(exc)
        if service in running and mods_ready:
            started = _systemctl(systemctl, "start", service)
            entry["started"] = started.returncode == 0
            if started.returncode != 0:
                entry["error"] = started.stderr.strip() or "Could not start the server"
        else:
            entry["started"] = False
        results.append(entry)

    return {
        "changed": True,
        "from": current,
        "to": target_version,
        "removed_versions": prune_versions(install_dir),
        "instances": results,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("upgrade", "rollback"))
    parser.add_argument("--instances", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--systemctl", required=True)
    parser.add_argument("--owner", required=True, help="user:group that owns mod files")
    args = parser.parse_args()
    try:
        instances = json.loads(args.instances.read_text(encoding="utf-8"))
        if not isinstance(instances, list):
            raise EngineError("The instance registry must be a JSON array")
        print(
            json.dumps(
                change_engine(
                    args.action,
                    instances,
                    args.install_dir,
                    args.cache_dir,
                    args.systemctl,
                    args.owner,
                )
            )
        )
    except (EngineError, ModSyncError, OSError, ValueError) as exc:
        print(f"Factorio {args.action} failed: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
