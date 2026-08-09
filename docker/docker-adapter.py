#!/usr/bin/env python3
"""Translate the FUI's fixed systemd/journal commands to Docker CLI calls."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_SERVICE = re.compile(r"^factorio@([a-z0-9][a-z0-9_-]{0,31})\.service$")
DATA_ROOT = Path(os.environ.get("FSC_DATA_ROOT", "/data"))


def container_for(service: str) -> str:
    match = _SERVICE.fullmatch(service)
    if not match:
        raise SystemExit(f"Unsupported service: {service}")
    return os.environ.get("DOCKER_CONTAINER_PREFIX", "fsc-factorio-") + match.group(1)


def docker(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/usr/bin/docker", *args],
        text=True,
        capture_output=True,
        check=check,
    )


def inspect(container: str) -> dict[str, object]:
    result = docker("inspect", container)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return json.loads(result.stdout)[0]


def parse_started(value: str) -> dt.datetime | None:
    if not value or value.startswith("0001-"):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        match = re.match(r"(.+\.\d{6})\d*(Z|[+-].+)$", value)
        return dt.datetime.fromisoformat((match.group(1) + match.group(2)).replace("Z", "+00:00")) if match else None


def systemctl(args: list[str]) -> int:
    if not args:
        return 1
    action = args[0]
    if action == "is-active":
        service = args[-1]
        state = inspect(container_for(service))["State"]
        running = bool(state.get("Running"))
        if "--quiet" not in args:
            print("active" if running else "inactive")
        return 0 if running else 3

    if action in {"start", "stop", "restart"}:
        container = container_for(args[1])
        command = [action]
        if action in {"stop", "restart"}:
            command.extend(["--time", "120"])
        result = docker(*command, container)
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.returncode

    if action != "show":
        print(f"Unsupported systemctl action: {action}", file=sys.stderr)
        return 1

    service = args[1]
    data = inspect(container_for(service))
    state = data["State"]
    started = parse_started(str(state.get("StartedAt", "")))
    running = bool(state.get("Running"))
    elapsed = max(0.0, (dt.datetime.now(dt.timezone.utc) - started).total_seconds()) if started and running else 0.0
    invocation = hashlib.md5(str(data.get("Id", "")).encode(), usedforsecurity=False).hexdigest()
    values = {
        "ActiveState": "active" if running else "inactive",
        "SubState": "running" if running else "dead",
        "UnitFileState": "enabled",
        "ActiveEnterTimestamp": started.astimezone().strftime("%a %Y-%m-%d %H:%M:%S %Z") if started and running else "",
        "ActiveEnterTimestampMonotonic": str(int(max(0.0, time.monotonic() - elapsed) * 1_000_000)) if running else "0",
        "MainPID": str(state.get("Pid", 0)),
        "NRestarts": str(data.get("RestartCount", 0)),
        "InvocationID": invocation,
    }
    requested: list[str] = []
    value_only = "--value" in args
    for argument in args[2:]:
        if argument.startswith("--property="):
            requested.extend(argument.partition("=")[2].split(","))
    requested = requested or list(values)
    for name in requested:
        print(values.get(name, "") if value_only else f"{name}={values.get(name, '')}")
    return 0


def timestamped_logs(
    instance_id: str,
    lines: int,
    current_only: bool,
    pattern: re.Pattern[str] | None = None,
) -> list[tuple[str, str]]:
    state = DATA_ROOT / "instances" / instance_id / "state"
    paths = [state / "factorio-current.log"]
    if not current_only:
        paths.insert(0, state / "factorio-history.log")

    records: list[tuple[str, str]] = []
    clock = re.compile(r"\b(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\b")
    elapsed = re.compile(r"^\s*(\d+(?:\.\d+)?)\b")
    for path in paths:
        try:
            fallback = dt.datetime.fromtimestamp(path.stat().st_mtime, dt.timezone.utc)
            messages = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        run_origin: dt.datetime | None = None
        for message in messages:
            found = clock.search(message)
            elapsed_match = elapsed.match(message)
            parsed = fallback
            if found:
                parsed = dt.datetime.strptime(found.group(1), "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=dt.timezone.utc
                )
                if elapsed_match:
                    run_origin = parsed - dt.timedelta(seconds=float(elapsed_match.group(1)))
            elif elapsed_match and run_origin:
                parsed = run_origin + dt.timedelta(seconds=float(elapsed_match.group(1)))
            if pattern and not pattern.search(message):
                continue
            records.append((parsed.isoformat().replace("+00:00", "Z"), message))
    return records[-lines:]


def journalctl(args: list[str]) -> int:
    service = args[args.index("--unit") + 1]
    lines = int(args[args.index("--lines") + 1])
    match = _SERVICE.fullmatch(service)
    if not match:
        print(f"Unsupported service: {service}", file=sys.stderr)
        return 1
    instance_id = match.group(1)
    current_only = any(arg.startswith("_SYSTEMD_INVOCATION_ID=") for arg in args)
    grep = args[args.index("--grep") + 1] if "--grep" in args else None
    pattern = re.compile(grep) if grep else None
    records = timestamped_logs(instance_id, lines, current_only, pattern)
    json_output = "--output=json" in args
    container = container_for(service)
    for stamp, message in records:
        if pattern and not pattern.search(message):
            continue
        parsed = parse_started(stamp)
        micros = int(parsed.timestamp() * 1_000_000) if parsed else 0
        if json_output:
            print(json.dumps({"MESSAGE": message, "__REALTIME_TIMESTAMP": str(micros)}))
        else:
            print(f"{stamp} {container} {message}")
    return 0


def sudo(args: list[str]) -> int:
    command = [arg for arg in args if arg != "--non-interactive"]
    if not command or not Path(command[0]).is_absolute():
        print("sudo adapter accepts only an absolute command", file=sys.stderr)
        return 1
    os.execv(command[0], command)
    return 1


def main() -> int:
    tool = Path(sys.argv[0]).name
    if tool == "systemctl":
        return systemctl(sys.argv[1:])
    if tool == "journalctl":
        return journalctl(sys.argv[1:])
    if tool == "sudo":
        return sudo(sys.argv[1:])
    print(f"Unknown adapter invocation: {tool}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
