"""Password hashing and admin-token utilities."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from datetime import datetime
from zoneinfo import ZoneInfo

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


SCANNER_REAUTH_CUTOFF = (8, 25)


def admin_token(
    org_slug: str,
    username: str,
    password_hash: str,
    *,
    purpose: str = "admin",
) -> str:
    """Create an expiring token bound to the current password hash."""
    if purpose not in {"admin", "scanner_morning", "scanner_after_cutoff"}:
        raise ValueError("Unsupported token purpose")
    issued_at = int(time.time())
    payload = f"v2:{purpose}:{org_slug}:{username}:{password_hash}:{issued_at}"
    return f"v2:{purpose}:{issued_at}:{_sign(payload)}"


def _token_max_age(purpose: str, issued_at: int) -> int:
    if purpose == "scanner_morning":
        issued_local = datetime.fromtimestamp(issued_at, ZoneInfo("Asia/Kolkata"))
        cutoff = issued_local.replace(
            hour=SCANNER_REAUTH_CUTOFF[0],
            minute=SCANNER_REAUTH_CUTOFF[1],
            second=0,
            microsecond=0,
        )
        return max(0, int(cutoff.timestamp()) - issued_at)

    try:
        return int(os.getenv("ADMIN_TOKEN_MAX_AGE_SECONDS", "28800"))
    except ValueError:
        return 28800


def verify_admin_token(
    org_slug: str,
    username: str,
    password_hash: str,
    token: str,
    *,
    allowed_purposes: set[str] | None = None,
) -> bool:
    allowed_purposes = allowed_purposes or {"admin"}
    try:
        version, purpose, issued_raw, signature = token.split(":", 3)
        if version != "v2" or purpose not in {"admin", "scanner_morning", "scanner_after_cutoff"} or purpose not in allowed_purposes:
            return False
        issued_at = int(issued_raw)
    except (ValueError, AttributeError):
        return False

    max_age = _token_max_age(purpose, issued_at)
    now = int(time.time())
    if purpose == "scanner_morning" and max_age <= 0:
        return False
    if max_age > 0 and (issued_at > now + 60 or now - issued_at > max_age):
        return False

    payload = f"v2:{purpose}:{org_slug}:{username}:{password_hash}:{issued_at}"
    return hmac.compare_digest(_sign(payload), signature)
