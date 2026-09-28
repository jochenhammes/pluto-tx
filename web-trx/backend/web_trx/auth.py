"""Shared-password session auth -- deliberately not a user account system:
the setting is always exactly one licensed operator (docs/PROJECT_PLAN.md
section 1), so a single server-side password plus an opaque session token
per logged-in browser tab is the whole model. No password is ever
hardcoded: WEB_TRX_PASSWORD must be set, or one is generated and printed
to stderr once at startup (the same "generate and print" pattern Jupyter
uses) -- either way nothing guessable ships in source.

Sessions can optionally survive a server restart (WEB_TRX_SESSIONS_PATH,
set by scripts/start.sh): the file stores only SHA-256 hashes of the
tokens plus their expiry, never the tokens themselves.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time

COOKIE_NAME = "web_trx_token"
DEFAULT_TOKEN_TTL_S = 12 * 3600


class AuthManager:
    def __init__(self, password: str | None = None, token_ttl_s: float = DEFAULT_TOKEN_TTL_S,
                 sessions_path: str | None = None):
        self.password = password or os.environ.get("WEB_TRX_PASSWORD") or self._generate_and_print()
        self.token_ttl_s = token_ttl_s
        # sha256(token) -> expiry (wall clock, so it stays meaningful across restarts)
        self._tokens: dict[str, float] = {}
        self._sessions_path = sessions_path
        self._load()

    @staticmethod
    def _generate_and_print() -> str:
        password = secrets.token_urlsafe(12)
        print(
            f"WEB_TRX_PASSWORD not set -- generated one-time password for this run: {password}",
            file=sys.stderr,
        )
        return password

    def check_password(self, candidate: str) -> bool:
        # Constant-time compare -- a login endpoint is exactly the kind of
        # place a timing side-channel on string comparison matters.
        return hmac.compare_digest(candidate, self.password)

    def issue_token(self) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[_digest(token)] = time.time() + self.token_ttl_s
        self._save()
        return token

    def validate(self, token: str | None) -> bool:
        if not token:
            return False
        key = _digest(token)
        expiry = self._tokens.get(key)
        if expiry is None:
            return False
        if time.time() > expiry:
            del self._tokens[key]
            self._save()
            return False
        return True

    def revoke(self, token: str | None) -> None:
        if token and self._tokens.pop(_digest(token), None) is not None:
            self._save()

    def _load(self) -> None:
        if not self._sessions_path or not os.path.exists(self._sessions_path):
            return
        try:
            with open(self._sessions_path) as f:
                saved = json.load(f)
            now = time.time()
            self._tokens = {k: float(v) for k, v in saved.items() if float(v) > now}
        except (OSError, ValueError, TypeError, AttributeError):
            self._tokens = {}  # unreadable -> everyone logs in again, nothing worse

    def _save(self) -> None:
        if not self._sessions_path:
            return
        tmp = self._sessions_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._tokens, f)
        os.replace(tmp, self._sessions_path)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
