"""Single-user password auth: scrypt hashes, login throttling, CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=2**26)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${b64(salt)}${b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(
            password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
            maxmem=2**26, dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class LoginThrottle:
    """At most ``max_failures`` failed logins per client within ``window`` seconds."""

    def __init__(self, max_failures: int = 5, window: float = 15 * 60):
        self.max_failures = max_failures
        self.window = window
        self._failures: dict[str, list[float]] = {}

    def _recent(self, client: str) -> list[float]:
        now = time.monotonic()
        recent = [t for t in self._failures.get(client, []) if now - t < self.window]
        self._failures[client] = recent
        return recent

    def blocked(self, client: str) -> bool:
        return len(self._recent(client)) >= self.max_failures

    def fail(self, client: str) -> None:
        self._recent(client).append(time.monotonic())

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)
