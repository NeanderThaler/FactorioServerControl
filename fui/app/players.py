"""Player presence and playtime, reconstructed from the server's log lines.

The headless build prints a wall-clock tagged line whenever someone joins or
leaves. Short of attaching to the game's console that is the only presence
signal available, so sessions are rebuilt by pairing those lines up.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from .config import Settings
from .systemd import read_journal_json, read_status

# Coarse pre-filter handed to journalctl; every line is re-checked below.
_JOURNAL_GREP = (
    r"\[JOIN\]|\[LEAVE\]|; Factorio |Goodbye|processed PlayerJoinGame|removing peer\("
)

_DEFAULT_EVENTS = 20000

_CLOCK = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"

# Anchoring on the timestamp and the tag slot is what makes this safe: chat is
# printed as "<clock> [CHAT] <name>: <text>", so a player typing "[JOIN] x
# joined the game" lands after the tag and can never match here.
_JOIN = re.compile(rf"^{_CLOCK} \[JOIN\] (?P<name>.+) joined the game$")
_LEAVE = re.compile(rf"^{_CLOCK} \[LEAVE\] (?P<name>.+) left the game$")

# Engine lines are prefixed with seconds-since-start, which chat cannot forge.
_RUN_START = re.compile(rf"^\s*[\d.]+ {_CLOCK}; Factorio \d")
_RUN_END = re.compile(r"^\s*[\d.]+ Goodbye\s*$")
# A timed-out or dropped client never gets a [LEAVE] line, only a peer removal,
# so the peer id announced just before its [JOIN] is kept to close it later.
_PEER_JOIN = re.compile(
    r"^\s*[\d.]+ Info \w+\.cpp:\d+: UpdateTick \(\d+\) processed PlayerJoinGame peerID\((?P<peer>\d+)\)"
)
_PEER_REMOVED = re.compile(r"^\s*[\d.]+ Info \w+\.cpp:\d+: .*removing peer\((?P<peer>\d+)\)")


def _blank() -> dict[str, Any]:
    return {"sessions": 0, "total_seconds": 0.0, "last_seen": None, "online": False}


def read_players(
    settings: Settings,
    max_events: int = _DEFAULT_EVENTS,
    running: bool | None = None,
) -> dict[str, Any]:
    entries, limit = read_journal_json(settings, _JOURNAL_GREP, max_events)
    if running is None:
        running = read_status(settings)["running"]
    now = time.time()

    totals: dict[str, dict[str, Any]] = {}
    open_since: dict[str, float] = {}
    first_seen: float | None = None
    last_event: float | None = None
    last_join: dict[str, Any] | None = None
    pending_peers: deque[str] = deque()
    peer_names: dict[str, str] = {}

    def close(name: str, end: float) -> None:
        start = open_since.pop(name, None)
        if start is None:
            return
        record = totals.setdefault(name, _blank())
        record["sessions"] += 1
        record["total_seconds"] += max(0.0, end - start)
        record["last_seen"] = end

    for entry in entries:
        message = entry.get("MESSAGE")
        if not isinstance(message, str):
            continue
        try:
            stamp = int(entry["__REALTIME_TIMESTAMP"]) / 1_000_000
        except (KeyError, TypeError, ValueError):
            continue
        if first_seen is None:
            first_seen = stamp
        last_event = stamp

        joined = _JOIN.match(message)
        if joined:
            name = joined.group("name")
            last_join = {"name": name, "at": stamp}
            totals.setdefault(name, _blank())
            if pending_peers:
                peer_names[pending_peers.popleft()] = name
            # A duplicate join without a leave keeps the earlier start.
            open_since.setdefault(name, stamp)
            continue

        left = _LEAVE.match(message)
        if left:
            close(left.group("name"), stamp)
            continue

        peer_join = _PEER_JOIN.match(message)
        if peer_join:
            pending_peers.append(peer_join.group("peer"))
            continue

        removed = _PEER_REMOVED.match(message)
        if removed:
            peer = removed.group("peer")
            if peer in pending_peers:
                pending_peers.remove(peer)
            name = peer_names.pop(peer, None)
            if name is not None:
                close(name, stamp)
            continue

        if _RUN_START.match(message) or _RUN_END.match(message):
            # Peer ids restart with every run.
            pending_peers.clear()
            peer_names.clear()
            # A stop or a crash disconnects everyone without emitting [LEAVE].
            for name in list(open_since):
                close(name, stamp)

    if not running:
        # Whatever is still open ended when the server last said anything.
        for name in list(open_since):
            close(name, last_event or now)

    for name, start in open_since.items():
        record = totals.setdefault(name, _blank())
        record["sessions"] += 1
        record["total_seconds"] += max(0.0, now - start)
        record["last_seen"] = now
        record["online"] = True
        record["current_seconds"] = max(0.0, now - start)
        record["online_since"] = start

    players = [
        {
            "name": name,
            "online": record["online"],
            "sessions": record["sessions"],
            "total_seconds": round(record["total_seconds"], 1),
            "current_seconds": round(record.get("current_seconds", 0.0), 1) or None,
            "online_since": record.get("online_since"),
            "last_seen": record["last_seen"],
        }
        for name, record in totals.items()
    ]
    players.sort(key=lambda p: (not p["online"], -p["total_seconds"], p["name"].lower()))

    return {
        "players": players,
        "online_count": sum(1 for player in players if player["online"]),
        "running": running,
        "window_start": first_seen,
        "last_join": last_join,
        # Older events fell outside the fetch window, so totals are a lower bound.
        "truncated": len(entries) >= limit,
    }
