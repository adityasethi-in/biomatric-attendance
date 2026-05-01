"""Password hashing and admin-token utilities."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

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


def admin_token(org_slug: str, username: str, password_hash: str) -> str:
    payload = f"{org_slug}:{username}:{password_hash}"
    return hmac.new(
        admin_token_secret().encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
