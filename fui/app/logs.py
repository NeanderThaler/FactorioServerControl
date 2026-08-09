"""Human-readable events extracted from Factorio's server log."""

from __future__ import annotations

import datetime
import re

from .config import Settings
from .systemd import read_journal_json

_JOURNAL_GREP = r"\[JOIN\]|\[LEAVE\]|\[CHAT\]|; Factorio |Goodbye"

_CLOCK = r"(?P<clock>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"

_RUN_START = re.compile(
    rf"^\s*[\d.]+ {_CLOCK}; Factorio (?P<version>[^ ]+) \(.*\)$"
)
_RUN_END = re.compile(r"^\s*[\d.]+ Goodbye\s*$")
_JOIN = re.compile(rf"^{_CLOCK} \[JOIN\] (?P<name>.+) joined the game$")
_LEAVE = re.compile(rf"^{_CLOCK} \[LEAVE\] (?P<name>.+) left the game$")
_CHAT = re.compile(rf"^{_CLOCK} \[CHAT\] (?P<name>.+?): (?P<text>.*)$")
_DEATH = re.compile(
    r"^(?P<name>.+?) (?P<event>was killed(?: by .+)?|died(?: by .+)?)\.?$",
    re.IGNORECASE,
)


def _journal_clock(realtime: object) -> str:
    try:
        stamp = int(realtime) / 1_000_000
    except (TypeError, ValueError):
        return "Unknown time"
    return datetime.datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S")


def clean_log_line(message: str, realtime: object = None) -> str | None:
    """Return one concise event line, or None for routine engine output."""
    started = _RUN_START.match(message)
    if started:
        return f'{started.group("clock")}  Server started (Factorio {started.group("version")})'

    if _RUN_END.match(message):
        return f"{_journal_clock(realtime)}  Server stopped"

    joined = _JOIN.match(message)
    if joined:
        return f'{joined.group("clock")}  {joined.group("name")} joined'

    left = _LEAVE.match(message)
    if left:
        return f'{left.group("clock")}  {left.group("name")} left'

    chat = _CHAT.match(message)
    if not chat:
        return None

    name = chat.group("name")
    text = chat.group("text")
    if name == "<server>":
        death = _DEATH.match(text)
        if not death:
            return None
        return (
            f'{chat.group("clock")}  {death.group("name")} died: '
            f'{death.group("event").rstrip(".")}'
        )
    return f'{chat.group("clock")}  {name}: {text}'


def read_clean_logs(
    settings: Settings, lines: int, current_run_only: bool = True
) -> list[str]:
    entries, _ = read_journal_json(
        settings,
        _JOURNAL_GREP,
        lines,
        current_run_only=current_run_only,
    )
    result = []
    for entry in entries:
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            continue
        line = clean_log_line(message, entry.get("__REALTIME_TIMESTAMP"))
        if line is not None:
            result.append(line)
    return result