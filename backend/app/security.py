"""Password hashing and admin-token utilities."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

from passlib.context import CryptContext


_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(password: str) -> str:
    return _pwd_context.hash(password)


def verify_password(password: str, stored_hash: str) -> tuple[bool, bool]:
    """Return (matches, needs_rehash)."""
    if not stored_hash:
        return (False, False)
    try:
        return (_pwd_context.verify(password, stored_hash), _pwd_context.needs_update(stored_hash))
    except ValueError:
        return (False, False)


def admin_token_secret() -> str:
    """Return the HMAC secret used for admin/scanner tokens."""
    secret = os.getenv("ADMIN_TOKEN_SECRET", "").strip()
    bad_prefixes = ("change-this", "replace-with", "default", "secret")
    if secret and not secret.lower().startswith(bad_prefixes):
        return secret
    if os.getenv("BIOMATRIC_DEV_MODE", "").lower() in {"1", "true", "yes"}:
        os.environ["ADMIN_TOKEN_SECRET"] = secrets.token_hex(32)
        return os.environ["ADMIN_TOKEN_SECRET"]
    raise RuntimeError(
        "ADMIN_TOKEN_SECRET is missing or still a placeholder. Generate one "
        "with `openssl rand -hex 32` and set it in your server environment."
    )


def _sign(payload: str) -> str:
    return hmac.new(
        admin_token_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def admin_token(org_slug: str, username: str, password_hash: str) -> str:
    issued_at = int(time.time())
    payload = f"{org_slug}:{username}:{password_hash}:{issued_at}"
    return f"{issued_at}:{_sign(payload)}"


def verify_admin_token(org_slug: str, username: str, password_hash: str, token: str) -> bool:
    try:
        issued_raw, signature = token.split(":", 1)
        issued_at = int(issued_raw)
    except (ValueError, AttributeError):
        return False

    try:
        max_age = int(os.getenv("ADMIN_TOKEN_MAX_AGE_SECONDS", "28800"))
    except ValueError:
        max_age = 28800
    now = int(time.time())
    if max_age > 0 and (issued_at > now + 60 or now - issued_at > max_age):
        return False

    payload = f"{org_slug}:{username}:{password_hash}:{issued_at}"
    return hmac.compare_digest(_sign(payload), signature)
