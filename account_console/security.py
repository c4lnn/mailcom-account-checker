from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time

from cryptography.fernet import Fernet, InvalidToken


class SecretBox:
    def __init__(self, key: str | None = None) -> None:
        value = key or os.environ.get("ACCOUNT_CHECKER_ENCRYPTION_KEY", "")
        if not value:
            raise RuntimeError("ACCOUNT_CHECKER_ENCRYPTION_KEY is required")
        try:
            self._fernet = Fernet(value.encode("ascii"))
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError("ACCOUNT_CHECKER_ENCRYPTION_KEY is invalid") from exc

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes | str) -> str:
        try:
            raw = value.encode("ascii") if isinstance(value, str) else value
            return self._fernet.decrypt(raw).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise ValueError("encrypted_value_invalid") from exc


class SessionSigner:
    def __init__(self, secret: str | None = None, *, ttl_seconds: int = 12 * 60 * 60):
        self.secret = (secret or os.environ.get("CONSOLE_SESSION_SECRET", "")).encode("utf-8")
        if len(self.secret) < 32:
            raise RuntimeError("CONSOLE_SESSION_SECRET must be at least 32 characters")
        self.ttl_seconds = ttl_seconds

    def issue(self, now: int | None = None) -> str:
        issued_at = int(time.time() if now is None else now)
        payload = f"{issued_at}:{secrets.token_urlsafe(24)}".encode("ascii")
        signature = hmac.new(self.secret, payload, hashlib.sha256).hexdigest().encode("ascii")
        return base64.urlsafe_b64encode(payload + b"." + signature).decode("ascii")

    def verify(self, token: str, now: int | None = None) -> bool:
        try:
            decoded = base64.urlsafe_b64decode(token.encode("ascii"))
            payload, signature = decoded.rsplit(b".", 1)
            expected = hmac.new(self.secret, payload, hashlib.sha256).hexdigest().encode("ascii")
            issued_at = int(payload.split(b":", 1)[0])
            current = int(time.time() if now is None else now)
            return (
                hmac.compare_digest(signature, expected)
                and issued_at <= current
                and current - issued_at <= self.ttl_seconds
            )
        except (ValueError, UnicodeError, TypeError):
            return False


def password_matches(candidate: str, expected: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
