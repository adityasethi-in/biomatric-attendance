"""HTTP client for sending attendance events to a Delight-Model-School-style
DMS backend.

Each organization in BIOMATRIC may optionally point to a DMS instance
(`dms_base_url` + `dms_webhook_secret`). When set, every successful
`/attendance/mark` queues a webhook in `dms_outbox`; a background worker
drains the outbox with HMAC-signed POSTs and exponential backoff.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

LOGGER = logging.getLogger("biomatric.dms_link")

OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS dms_outbox (
  id SERIAL PRIMARY KEY,
  organization_id INT NOT NULL,
  endpoint VARCHAR(255) NOT NULL,
  payload_json TEXT NOT NULL,
  attempt_count INT NOT NULL DEFAULT 0,
  next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_error TEXT,
  delivered_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_dms_outbox_pending ON dms_outbox(delivered_at, next_attempt_at);
"""

def sign_request(secret: str, method: str, path: str, body: bytes) -> tuple[str, str]:
    """Return (timestamp, signature) for an HMAC-signed call to DMS."""
    ts = str(int(time.time()))
    payload = b"\n".join([method.upper().encode(), path.encode(), ts.encode(), body])
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return ts, sig


def _split_url(base_url: str, path: str) -> tuple[str, str]:
    """Return (host_url, full_path) preserving any base-url prefix.

    A base URL of ``https://dms.example.com/api/v1`` plus the relative path
    ``/integrations/biomatric/attendance`` becomes
    ``(https://dms.example.com, /api/v1/integrations/biomatric/attendance)``
    so the HMAC payload signs the *full* path the server actually sees.
    """
    base = base_url.rstrip("/")
    if base.endswith("/api/v1"):
        prefix = "/api/v1"
        host = base[: -len("/api/v1")]
    elif "/api/v1" in base:
        host, _, suffix = base.partition("/api/v1")
        prefix = "/api/v1" + (suffix or "")
    else:
        host = base
        prefix = ""
    full_path = f"{prefix}{path}"
    return host, full_path


async def enqueue_attendance(
    db: AsyncSession,
    organization_id: int,
    person_kind: str,
    person_id: str,
    marked_at: datetime,
    confidence: int | None,
    source_ref: str,
    status: str = "present",
):
    payload = {
        "person_kind": person_kind,
        "person_id": str(person_id),
        "marked_at": marked_at.astimezone(timezone.utc).isoformat(),
        "confidence": confidence,
        "source_ref": source_ref,
        "status": status,
    }
    await db.execute(
        text(
            """
            INSERT INTO dms_outbox (organization_id, endpoint, payload_json)
            VALUES (:org_id, :endpoint, :payload)
            """
        ),
        {
            "org_id": organization_id,
            "endpoint": "/integrations/biomatric/attendance",
            "payload": json.dumps(payload),
        },
    )


async def _post_signed(base_url: str, secret: str, path: str, payload: dict[str, Any]) -> httpx.Response:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    host, full_path = _split_url(base_url, path)
    ts, sig = sign_request(secret, "POST", full_path, body)
    headers = {
        "Content-Type": "application/json",
        "X-Biomatric-Timestamp": ts,
        "X-Biomatric-Signature": sig,
        "User-Agent": "biomatric-webhook/1.0",
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await client.post(f"{host}{full_path}", content=body, headers=headers)


async def _get_signed(base_url: str, secret: str, path: str) -> httpx.Response:
    host, full_path = _split_url(base_url, path)
    ts, sig = sign_request(secret, "GET", full_path, b"")
    headers = {
        "Accept": "application/json",
        "X-Biomatric-Timestamp": ts,
        "X-Biomatric-Signature": sig,
        "User-Agent": "biomatric-webhook/1.0",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        return await client.get(f"{host}{full_path}", headers=headers)


async def fetch_roster(base_url: str, secret: str) -> dict:
    response = await _get_signed(base_url, secret, "/integrations/biomatric/roster")
    response.raise_for_status()
    return response.json()


async def health_check(base_url: str, secret: str) -> dict:
    response = await _get_signed(base_url, secret, "/integrations/biomatric/health")
    response.raise_for_status()
    return response.json()


def _backoff_seconds(attempt: int) -> int:
    return min(60 * 60, 5 * (2 ** min(attempt, 8)))


async def _drain_once(SessionLocal: async_sessionmaker[AsyncSession], batch: int = 25):
    async with SessionLocal() as db:
        rows = (await db.execute(
            text(
                """
                SELECT o.id, o.organization_id, o.endpoint, o.payload_json, o.attempt_count,
                       org.dms_base_url, org.dms_webhook_secret
                FROM dms_outbox o
                JOIN organizations org ON org.id = o.organization_id
                WHERE o.delivered_at IS NULL
                  AND o.next_attempt_at <= now()
                  AND org.dms_base_url IS NOT NULL
                  AND org.dms_webhook_secret IS NOT NULL
                ORDER BY o.id ASC
                LIMIT :batch
                """
            ),
            {"batch": batch},
        )).mappings().all()

    if not rows:
        return 0

    delivered = 0
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            response = await _post_signed(
                row["dms_base_url"], row["dms_webhook_secret"], row["endpoint"], payload
            )
            ok = 200 <= response.status_code < 300
            error = None if ok else f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"

        async with SessionLocal() as db:
            if ok:
                await db.execute(
                    text(
                        """
                        UPDATE dms_outbox
                        SET delivered_at = now(),
                            attempt_count = attempt_count + 1,
                            last_error = NULL
                        WHERE id = :id
                        """
                    ),
                    {"id": row["id"]},
                )
                delivered += 1
            else:
                next_in = _backoff_seconds(row["attempt_count"] + 1)
                await db.execute(
                    text(
                        """
                        UPDATE dms_outbox
                        SET attempt_count = attempt_count + 1,
                            last_error = :err,
                            next_attempt_at = now() + (:delta || ' seconds')::interval
                        WHERE id = :id
                        """
                    ),
                    {"id": row["id"], "err": error, "delta": next_in},
                )
                LOGGER.warning("DMS webhook failed (id=%s, attempt=%s): %s", row["id"], row["attempt_count"] + 1, error)
            await db.commit()
    return delivered


async def outbox_worker(SessionLocal: async_sessionmaker[AsyncSession], stop_event: asyncio.Event):
    """Long-running coroutine. Drains the outbox every few seconds."""
    LOGGER.info("BIOMATRIC -> DMS outbox worker started")
    while not stop_event.is_set():
        try:
            delivered = await _drain_once(SessionLocal)
            if delivered:
                LOGGER.info("Delivered %s DMS webhook(s)", delivered)
        except Exception as exc:  # never crash the worker
            LOGGER.exception("Outbox drain error: %s", exc)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
    LOGGER.info("BIOMATRIC -> DMS outbox worker stopped")
