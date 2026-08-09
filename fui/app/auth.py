"""Password-only session authentication.

The cookie carries a signed, expiring token rather than the password itself, so
a leaked cookie cannot be turned back into the credential and the password is
not retransmitted on every request.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass, field

_TOKEN_VERSION = "v1"
_MAX_TRACKED_CLIENTS = 1024


def _sign(secret: str, payload: str) -> str:
    digest = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token(secret: str, ttl_seconds: int) -> str:
    payload = f"{_TOKEN_VERSION}.{int(time.time()) + ttl_seconds}"
    return f"{payload}.{_sign(secret, payload)}"


def verify_token(secret: str, token: str | None) -> bool:
    if not token:
        return False
    parts = token.split(".")
    if len(parts) != 3:
        return False
    version, expires_raw, signature = parts
    if version != _TOKEN_VERSION:
        return False
    # Signature is checked before the expiry is parsed so an unsigned token
    # never reaches any further logic.
    if not hmac.compare_digest(_sign(secret, f"{version}.{expires_raw}"), signature):
        return False
    try:
        return int(expires_raw) > time.time()
    except ValueError:
        return False


def verify_password(expected: str, provided: str) -> bool:
    return hmac.compare_digest(expected.encode(), provided.encode())


def is_authenticated(password: str, secret: str, token: str | None) -> bool:
    return password == "" or verify_token(secret, token)


@dataclass
class _Attempts:
    failures: int = 0
    blocked_until: float = 0.0
    last_seen: float = field(default_factory=time.monotonic)


class LoginThrottle:
    """Rate-limit password guessing.

    With no username the whole credential space is a single password, so
    unlimited guessing is the main risk of dropping basic auth.
    """

    def __init__(
        self,
        max_failures: int = 5,
        base_lockout: float = 60.0,
        max_lockout: float = 900.0,
    ) -> None:
        self._max_failures = max_failures
        self._base_lockout = base_lockout
        self._max_lockout = max_lockout
        self._clients: dict[str, _Attempts] = {}

    def _prune(self, now: float) -> None:
        if len(self._clients) <= _MAX_TRACKED_CLIENTS:
            return
        stale = [
            key
            for key, state in self._clients.items()
            if state.blocked_until < now and now - state.last_seen > self._max_lockout
        ]
        for key in stale:
            del self._clients[key]

    def retry_after(self, client: str) -> float:
        state = self._clients.get(client)
        if state is None:
            return 0.0
        return max(0.0, state.blocked_until - time.monotonic())

    def record_failure(self, client: str) -> None:
        now = time.monotonic()
        self._prune(now)
        state = self._clients.setdefault(client, _Attempts())
        state.failures += 1
        state.last_seen = now
        if state.failures >= self._max_failures:
            backoff = self._base_lockout * 2 ** (state.failures - self._max_failures)
            state.blocked_until = now + min(backoff, self._max_lockout)

    def reset(self, client: str) -> None:
        self._clients.pop(client, None)
