"""Thin wrapper around systemctl/journalctl for a single, fixed unit.

Every subprocess call uses an explicit argv list (never a shell string) and the
action is checked against an allow-list, so no request data can reach a shell.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from .config import Settings

ALLOWED_ACTIONS = frozenset({"start", "stop", "restart"})

_STATUS_PROPERTIES = (
    "ActiveState",
    "SubState",
    "UnitFileState",
    "ActiveEnterTimestamp",
    "ActiveEnterTimestampMonotonic",
    "MainPID",
    "NRestarts",
)

_MAX_LOG_LINES = 500

_MAX_JOURNAL_EVENTS = 20000

_INVOCATION_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class ServiceError(RuntimeError):
    """Raised when systemd could not be queried or did not accept a command."""


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceError(f"Command timed out after {timeout}s") from exc
    except OSError as exc:
        raise ServiceError(f"Could not execute {argv[0]}: {exc}") from exc


def read_status(settings: Settings) -> dict[str, Any]:
    proc = _run(
        [
            settings.systemctl_path,
            "show",
            settings.service_name,
            "--no-pager",
            f"--property={','.join(_STATUS_PROPERTIES)}",
        ],
        settings.command_timeout,
    )
    if proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or "systemctl show failed")

    properties: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value

    active_state = properties.get("ActiveState", "unknown")
    try:
        active_since = int(properties.get("ActiveEnterTimestampMonotonic", "0")) / 1_000_000
    except ValueError:
        active_since = 0
    return {
        "service": settings.service_name,
        "active_state": active_state,
        "sub_state": properties.get("SubState", "unknown"),
        "enabled_state": properties.get("UnitFileState", "unknown"),
        "running": active_state == "active",
        "since": properties.get("ActiveEnterTimestamp") or None,
        "uptime_seconds": max(0.0, time.monotonic() - active_since)
        if active_state == "active" and active_since
        else None,
        "main_pid": int(properties.get("MainPID", "0") or 0),
        "restarts": int(properties.get("NRestarts", "0") or 0),
    }


def _invocation_id(settings: Settings) -> str:
    """Return the unit's most recent invocation ID, or "" if it cannot be read.

    systemd keeps the ID after the unit stops, so a stopped server still
    resolves to its final run.
    """
    try:
        proc = _run(
            [
                settings.systemctl_path,
                "show",
                settings.service_name,
                "--property=InvocationID",
                "--value",
            ],
            settings.command_timeout,
        )
    except ServiceError:
        return ""
    if proc.returncode != 0:
        return ""
    value = proc.stdout.strip()
    # Never let an unexpected value become a journalctl argument.
    return value if _INVOCATION_PATTERN.match(value) else ""


def read_logs(settings: Settings, lines: int, current_run_only: bool = True) -> list[str]:
    bounded = max(1, min(lines, _MAX_LOG_LINES))
    argv = [
        settings.journalctl_path,
        "--unit",
        settings.service_name,
        "--lines",
        str(bounded),
        "--no-pager",
        "--output=short-iso",
    ]

    # Preferred over --since ActiveEnterTimestamp: exact, and immune to clock
    # skew and to two restarts landing in the same second. Without a readable
    # ID the query stays unfiltered rather than returning nothing.
    invocation = _invocation_id(settings) if current_run_only else ""
    if invocation:
        argv.append(f"_SYSTEMD_INVOCATION_ID={invocation}")

    proc = _run(argv, settings.command_timeout)
    if proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or "journalctl failed")
    return proc.stdout.splitlines()


def read_journal_json(
    settings: Settings,
    grep: str,
    max_entries: int,
    current_run_only: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Return (entries, limit) for journal records whose MESSAGE matches grep.

    Filtering in journalctl rather than in Python keeps the whole history
    available without streaming all of it back. Entries are sorted oldest
    first: journalctl walks backwards when --lines is combined with --grep, so
    its output order cannot be relied on.
    """
    limit = max(1, min(max_entries, _MAX_JOURNAL_EVENTS))
    argv = [
        settings.journalctl_path,
        "--unit",
        settings.service_name,
        "--lines",
        str(limit),
        "--no-pager",
        "--output=json",
        "--output-fields=MESSAGE",
        "--grep",
        grep,
    ]
    invocation = _invocation_id(settings) if current_run_only else ""
    if invocation:
        argv.append(f"_SYSTEMD_INVOCATION_ID={invocation}")

    proc = _run(argv, settings.command_timeout)
    # A no-match run exits non-zero with nothing on stderr; that is not an error.
    if proc.returncode != 0 and proc.stderr.strip():
        raise ServiceError(proc.stderr.strip())

    entries: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            entries.append(record)

    def when(record: dict[str, Any]) -> int:
        try:
            return int(record.get("__REALTIME_TIMESTAMP", 0))
        except (TypeError, ValueError):
            return 0

    entries.sort(key=when)
    return entries, limit


def control(settings: Settings, action: str) -> None:
    if action not in ALLOWED_ACTIONS:
        raise ServiceError(f"Unsupported action: {action!r}")

    proc = _run(
        [
            settings.sudo_path,
            "--non-interactive",
            settings.systemctl_path,
            action,
            settings.service_name,
        ],
        settings.command_timeout,
    )
    if proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or f"systemctl {action} failed")


def reset_map(settings: Settings) -> None:
    """Archive the running map and restart, so the scenario is generated anew.

    All of the work happens in a fixed, root-owned script invoked with no
    arguments; nothing from the request reaches it.
    """
    proc = _run(
        [
            settings.sudo_path,
            "--non-interactive",
            settings.reset_script_path,
        ],
        settings.reset_timeout,
    )
    if proc.returncode != 0:
        raise ServiceError(proc.stderr.strip() or "map reset failed")
