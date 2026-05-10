import asyncio
import gc
import hashlib
import logging
import os
import re
import secrets
import smtplib
import ssl
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time as dt_time, timedelta, timezone
from decimal import Decimal
from email.message import EmailMessage
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo

import math

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import (
    SessionLocal,
    DEFAULT_SCHEMA,
    ensure_schema_exists,
    get_sessionmaker_for_schema,
    quote_identifier,
    safe_schema_name,
)
from .dms_link import (
    OUTBOX_DDL,
    enqueue_attendance,
    fetch_roster,
    health_check as dms_health_check,
    outbox_worker,
)
from .security import admin_token, admin_token_secret, hash_password, verify_admin_token, verify_password


LOGGER = logging.getLogger("biomatric")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


THRESH = float(os.getenv("FACE_MATCH_THRESHOLD", "0.61"))
DUPLICATE_THRESH = float(os.getenv("FACE_DUPLICATE_THRESHOLD", os.getenv("FACE_MATCH_THRESHOLD", "0.60")))
FACE_SCAN_CANDIDATES = int(os.getenv("FACE_SCAN_CANDIDATES", "20"))
FACE_MULTI_MATCH_MIN_HITS = int(os.getenv("FACE_MULTI_MATCH_MIN_HITS", "2"))
FACE_MATCH_MARGIN = float(os.getenv("FACE_MATCH_MARGIN", "0.035"))
CLIENT_FACE_MATCH_THRESHOLD = float(os.getenv("CLIENT_FACE_MATCH_THRESHOLD", "0.60"))
CLIENT_FACE_DUPLICATE_THRESHOLD = float(
    os.getenv("CLIENT_FACE_DUPLICATE_THRESHOLD", os.getenv("CLIENT_FACE_MATCH_THRESHOLD", "0.60"))
)
CLIENT_FACE_EMBEDDING_DIM = int(os.getenv("CLIENT_FACE_EMBEDDING_DIM", "128"))
CLIENT_FACE_MODEL_NAME = os.getenv("CLIENT_FACE_MODEL_NAME", "face-api-128").strip() or "face-api-128"
CLIENT_FACE_MODEL_VERSION = os.getenv("CLIENT_FACE_MODEL_VERSION", "vladmandic-face-api-1.7.15").strip()
CLIENT_FACE_SCAN_CANDIDATES = int(os.getenv("CLIENT_FACE_SCAN_CANDIDATES", "20"))
CLIENT_FACE_MULTI_MATCH_MIN_HITS = int(os.getenv("CLIENT_FACE_MULTI_MATCH_MIN_HITS", "2"))
CLIENT_FACE_MATCH_MARGIN = float(os.getenv("CLIENT_FACE_MATCH_MARGIN", "0.035"))
FACE_ENGINE_MODE = os.getenv("FACE_ENGINE_MODE", "server").lower()
FACE_ENGINE_ACTIVE_WINDOWS = os.getenv("FACE_ENGINE_ACTIVE_WINDOWS", "").strip()
FACE_ENGINE_IDLE_UNLOAD_SECONDS = int(os.getenv("FACE_ENGINE_IDLE_UNLOAD_SECONDS", "300"))
SCANNER_OVERRIDE_MINUTES = int(os.getenv("SCANNER_OVERRIDE_MINUTES", "30"))
LIVENESS_MODE = os.getenv("LIVENESS_MODE", "basic").lower()
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
VALID_PERSON_TYPES = {"student", "staff", "teacher"}
ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/jpg"}
DEFAULT_ORG_NAME = os.getenv("DEFAULT_FREE_ORG_NAME", "Delight Model School")
DEFAULT_ORG_SLUG = os.getenv("DEFAULT_FREE_ORG_SLUG", "delight-model-school")
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin@delightmodelschool.in").strip().lower()
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", "2500000"))
PRICE_PER_USER_PER_DAY = Decimal(os.getenv("PRICE_PER_USER_PER_DAY", "3"))
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))
ALLOWED_ORIGINS = _csv_env("ALLOWED_ORIGINS") or ["http://localhost:7200"]
TRUSTED_HOSTS = _csv_env("TRUSTED_HOSTS")
DEV_MODE = os.getenv("BIOMATRIC_DEV_MODE", "").lower() in {"1", "true", "yes"}

DMS_DEFAULT_BASE_URL = os.getenv("DMS_BASE_URL", "").strip() or None
DMS_DEFAULT_SECRET = os.getenv("DMS_WEBHOOK_SECRET", "").strip() or None
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", os.getenv("PUBLIC_APP_URL", "")).strip().rstrip("/")
PASSWORD_RESET_TOKEN_TTL_MINUTES = int(os.getenv("PASSWORD_RESET_TOKEN_TTL_MINUTES", "30"))
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587") or "587")
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", DEFAULT_ORG_NAME).strip()
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() in {"1", "true", "yes"}
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "false").lower() in {"1", "true", "yes"}


TENANT_SCHEMA_SQL = """
-- The `vector` extension is enabled at the database level by the DMS stack,
-- so BIOMATRIC does not (and cannot, without superuser rights) create it.
CREATE TABLE IF NOT EXISTS students (
  id SERIAL PRIMARY KEY,
  student_code VARCHAR(64) UNIQUE NOT NULL,
  full_name VARCHAR(128) NOT NULL,
  person_type VARCHAR(16) NOT NULL DEFAULT 'student',
  dms_person_kind VARCHAR(16),
  dms_person_id UUID,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS face_embeddings (
  id SERIAL PRIMARY KEY,
  student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  embedding vector(512) NOT NULL,
  quality_score INT DEFAULT 100,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client_face_embeddings (
  id SERIAL PRIMARY KEY,
  student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  model_name VARCHAR(80) NOT NULL DEFAULT 'face-api-128',
  model_version VARCHAR(80) NOT NULL DEFAULT 'vladmandic-face-api-1.7.15',
  embedding vector(128) NOT NULL,
  quality_score INT DEFAULT 100,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS attendance_logs (
  id SERIAL PRIMARY KEY,
  student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
  status VARCHAR(16) DEFAULT 'present',
  confidence INT DEFAULT 0,
  marked_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_students_code ON students(student_code);
CREATE INDEX IF NOT EXISTS idx_students_dms_person ON students(dms_person_kind, dms_person_id);
CREATE INDEX IF NOT EXISTS idx_attendance_marked_at ON attendance_logs(marked_at);
CREATE INDEX IF NOT EXISTS idx_face_embeddings_ivfflat
ON face_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX IF NOT EXISTS idx_client_face_embeddings_model
ON client_face_embeddings(model_name, model_version);
CREATE INDEX IF NOT EXISTS idx_client_face_embeddings_ivfflat
ON client_face_embeddings USING ivfflat (embedding vector_l2_ops) WITH (lists = 100);
"""

CENTRAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS organizations (
  id SERIAL PRIMARY KEY,
  name VARCHAR(160) NOT NULL,
  slug VARCHAR(96) UNIQUE NOT NULL,
  org_type VARCHAR(80),
  contact_name VARCHAR(128),
  phone VARCHAR(32),
  email VARCHAR(160),
  database_name VARCHAR(63) UNIQUE NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'active',
  is_free BOOLEAN NOT NULL DEFAULT false,
  seats INT NOT NULL DEFAULT 0,
  price_per_user_per_day NUMERIC(10,2) NOT NULL DEFAULT 3.00,
  billing_days INT NOT NULL DEFAULT 30,
  advance_amount NUMERIC(12,2) NOT NULL DEFAULT 0,
  payment_reference VARCHAR(128),
  dms_base_url VARCHAR(255),
  dms_webhook_secret VARCHAR(255),
  scanner_override_until TIMESTAMPTZ,
  scanner_override_by_admin_id INT,
  scanner_override_created_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organization_admins (
  id SERIAL PRIMARY KEY,
  organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  username VARCHAR(80) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  full_name VARCHAR(128),
  is_active BOOLEAN NOT NULL DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (organization_id, username)
);

CREATE TABLE IF NOT EXISTS password_reset_tokens (
  id SERIAL PRIMARY KEY,
  organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  admin_id INT NOT NULL REFERENCES organization_admins(id) ON DELETE CASCADE,
  token_hash VARCHAR(64) UNIQUE NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  used_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS payments (
  id SERIAL PRIMARY KEY,
  organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  amount NUMERIC(12,2) NOT NULL,
  status VARCHAR(24) NOT NULL DEFAULT 'paid',
  reference VARCHAR(128),
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_lookup
ON password_reset_tokens(token_hash, used_at, expires_at);
"""


def rate_limit_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=rate_limit_key, default_limits=["120/minute"])
_face_engine = None
_face_engine_last_used_at = 0.0
_face_engine_override_until: datetime | None = None


def _app_zone():
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception:
        return ZoneInfo("Asia/Kolkata")


def _parse_hhmm(value: str) -> dt_time | None:
    try:
        hour, minute = value.strip().split(":", 1)
        return dt_time(hour=int(hour), minute=int(minute))
    except Exception:
        return None


def _face_engine_windows() -> list[tuple[dt_time, dt_time]]:
    if not FACE_ENGINE_ACTIVE_WINDOWS:
        return []
    windows = []
    for raw_window in re.split(r"[,;]", FACE_ENGINE_ACTIVE_WINDOWS):
        if "-" not in raw_window:
            continue
        start_raw, end_raw = raw_window.split("-", 1)
        start = _parse_hhmm(start_raw)
        end = _parse_hhmm(end_raw)
        if start and end:
            windows.append((start, end))
    return windows


def _time_in_window(current: dt_time, start: dt_time, end: dt_time) -> bool:
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end


def face_engine_in_schedule() -> bool:
    windows = _face_engine_windows()
    if not windows:
        return True
    current = datetime.now(_app_zone()).time()
    return any(_time_in_window(current, start, end) for start, end in windows)


def _format_human_time(value: dt_time) -> str:
    return datetime.combine(datetime.today(), value).strftime("%I:%M %p").lstrip("0")


def face_engine_schedule_label() -> str:
    windows = _face_engine_windows()
    if not windows:
        return "all day"
    return ", ".join(f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for start, end in windows)


def face_engine_schedule_human() -> str:
    windows = _face_engine_windows()
    if not windows:
        return "all day"
    return ", ".join(f"{_format_human_time(start)} to {_format_human_time(end)}" for start, end in windows)


def _coerce_utc_datetime(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def scanner_override_active(access_context: dict | None = None) -> bool:
    override_until = _coerce_utc_datetime((access_context or {}).get("scanner_override_until"))
    return bool(override_until and override_until > datetime.now(timezone.utc))


def face_engine_allowed_now(access_context: dict | None = None) -> bool:
    return face_engine_in_schedule() or scanner_override_active(access_context)


def face_engine_closed_message() -> str:
    return (
        f"Scanner is available {face_engine_schedule_human()}. "
        f"Admin can unlock for {SCANNER_OVERRIDE_MINUTES} minutes."
    )


def remember_active_override(access_context: dict | None = None):
    global _face_engine_override_until
    override_until = _coerce_utc_datetime((access_context or {}).get("scanner_override_until"))
    if override_until and override_until > datetime.now(timezone.utc):
        if not _face_engine_override_until or override_until > _face_engine_override_until:
            _face_engine_override_until = override_until


def face_engine_globally_allowed() -> bool:
    global _face_engine_override_until
    if face_engine_in_schedule():
        return True
    if _face_engine_override_until and _face_engine_override_until > datetime.now(timezone.utc):
        return True
    _face_engine_override_until = None
    return False


def password_reset_email_ready() -> bool:
    return bool(SMTP_HOST and SMTP_FROM_EMAIL and SMTP_USERNAME and SMTP_PASSWORD)


def password_reset_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def password_reset_link(org_slug: str, email: str, token: str) -> str:
    base_url = APP_PUBLIC_URL or "http://localhost:7200"
    return (
        f"{base_url}/admin?reset_token={quote(token)}"
        f"&org={quote(org_slug)}&email={quote(email)}"
    )


def send_password_reset_email(to_email: str, org_name: str, reset_link: str):
    message = EmailMessage()
    message["Subject"] = f"{org_name} attendance password reset"
    message["From"] = f"{SMTP_FROM_NAME} <{SMTP_FROM_EMAIL}>"
    message["To"] = to_email
    message.set_content(
        "A password reset was requested for your attendance admin account.\n\n"
        f"Reset link:\n{reset_link}\n\n"
        f"This link expires in {PASSWORD_RESET_TOKEN_TTL_MINUTES} minutes.\n"
        "If you did not request this, ignore this email."
    )

    if SMTP_USE_SSL:
        smtp = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15, context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
    with smtp:
        if SMTP_USE_TLS and not SMTP_USE_SSL:
            smtp.starttls(context=ssl.create_default_context())
        smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
        smtp.send_message(message)


async def read_image_upload(upload: UploadFile) -> bytes:
    content_type = (upload.content_type or "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in ALLOWED_IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail="Only JPG, PNG, or WebP images are allowed")

    data = await upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="Image is too large. Use a smaller camera frame.")
    return data


def unload_face_engine(reason: str = "idle"):
    global _face_engine, _face_engine_last_used_at
    if _face_engine is None:
        return
    close = getattr(_face_engine, "close", None)
    if callable(close):
        close()
    _face_engine = None
    _face_engine_last_used_at = 0.0
    gc.collect()
    LOGGER.info("Face scanner released (%s)", reason)


def get_face_engine(access_context: dict | None = None):
    """Start the scanner engine only for the image upload flow."""
    global _face_engine, _face_engine_last_used_at
    if FACE_ENGINE_MODE in {"client", "off", "disabled", "false", "0"}:
        raise HTTPException(
            status_code=503,
            detail="Face scanner is not ready. Please refresh and try again.",
        )
    if not face_engine_allowed_now(access_context):
        unload_face_engine("outside-window")
        raise HTTPException(
            status_code=503,
            detail=face_engine_closed_message(),
        )
    remember_active_override(access_context)

    if _face_engine is None:
        from .face_engine import FaceEngine

        _face_engine = FaceEngine()
        LOGGER.info("Face scanner loaded")
    _face_engine_last_used_at = time.monotonic()
    return _face_engine


async def face_engine_reaper():
    while True:
        await asyncio.sleep(30)
        if _face_engine is None:
            continue
        idle_for = time.monotonic() - _face_engine_last_used_at
        if not face_engine_globally_allowed():
            unload_face_engine("outside-window")
        elif FACE_ENGINE_IDLE_UNLOAD_SECONDS > 0 and idle_for >= FACE_ENGINE_IDLE_UNLOAD_SECONDS:
            unload_face_engine("idle")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate the admin-token secret eagerly so misconfigured deployments
    # crash on boot instead of silently using a known string.
    admin_token_secret()

    async with SessionLocal() as db:
        await run_sql_script(db, TENANT_SCHEMA_SQL)
        await run_sql_script(db, CENTRAL_SCHEMA_SQL)
        await run_sql_script(db, OUTBOX_DDL)

        # Idempotent migrations for instances upgrading from <1.1. CREATE TABLE
        # IF NOT EXISTS will not add new columns to a pre-existing table, so
        # we explicitly add anything the integration introduces.
        for stmt in (
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS dms_base_url VARCHAR(255)",
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS dms_webhook_secret VARCHAR(255)",
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scanner_override_until TIMESTAMPTZ",
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scanner_override_by_admin_id INT",
            "ALTER TABLE organizations ADD COLUMN IF NOT EXISTS scanner_override_created_at TIMESTAMPTZ",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_kind VARCHAR(16)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_id UUID",
            "CREATE INDEX IF NOT EXISTS idx_students_dms_person ON students(dms_person_kind, dms_person_id)",
            "ALTER TABLE organization_admins ALTER COLUMN password_hash TYPE VARCHAR(255)",
            "ALTER TABLE organization_admins ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true",
            "CREATE INDEX IF NOT EXISTS idx_organization_admins_is_active ON organization_admins(is_active)",
        ):
            await db.execute(text(stmt))

        org_defaults = {
            "name": DEFAULT_ORG_NAME,
            "slug": DEFAULT_ORG_SLUG,
            "price": PRICE_PER_USER_PER_DAY,
            "default_schema": DEFAULT_SCHEMA,
            "dms_base": DMS_DEFAULT_BASE_URL,
            "dms_secret": DMS_DEFAULT_SECRET,
        }
        await db.execute(
            text(
                """
                INSERT INTO organizations (
                  name, slug, org_type, database_name, status, is_free, seats,
                  price_per_user_per_day, billing_days, advance_amount, payment_reference,
                  dms_base_url, dms_webhook_secret
                )
                VALUES (
                  :name, :slug, 'school', :default_schema, 'active', true, 0,
                  :price, 0, 0, 'FREE_INTERNAL', :dms_base, :dms_secret
                )
                ON CONFLICT (slug) DO UPDATE
                SET name = EXCLUDED.name,
                    database_name = EXCLUDED.database_name,
                    status = 'active',
                    is_free = true,
                    dms_base_url = COALESCE(EXCLUDED.dms_base_url, organizations.dms_base_url),
                    dms_webhook_secret = COALESCE(EXCLUDED.dms_webhook_secret, organizations.dms_webhook_secret)
                """
            ),
            org_defaults,
        )
        org = await get_organization_by_slug(db, DEFAULT_ORG_SLUG)
        if DEFAULT_ADMIN_PASSWORD:
            if DEFAULT_ADMIN_USERNAME != "admin":
                target = await db.execute(
                    text(
                        """
                        SELECT id FROM organization_admins
                        WHERE organization_id = :organization_id AND username = :username
                        """
                    ),
                    {"organization_id": org["id"], "username": DEFAULT_ADMIN_USERNAME},
                )
                if target.first():
                    await db.execute(
                        text(
                            """
                            DELETE FROM organization_admins
                            WHERE organization_id = :organization_id AND username = 'admin'
                            """
                        ),
                        {"organization_id": org["id"]},
                    )
                else:
                    await db.execute(
                        text(
                            """
                            UPDATE organization_admins
                            SET username = :username
                            WHERE organization_id = :organization_id AND username = 'admin'
                            """
                        ),
                        {"organization_id": org["id"], "username": DEFAULT_ADMIN_USERNAME},
                    )
            await db.execute(
                text(
                    """
                    INSERT INTO organization_admins (organization_id, username, password_hash, full_name, is_active)
                    VALUES (:organization_id, :username, :password_hash, 'Default Admin', true)
                    ON CONFLICT (organization_id, username) DO NOTHING
                    """
                ),
                {
                    "organization_id": org["id"],
                    "username": DEFAULT_ADMIN_USERNAME,
                    "password_hash": hash_password(DEFAULT_ADMIN_PASSWORD),
                },
            )
        await db.commit()

    async with SessionLocal() as db:
        schemas = (
            await db.execute(text("SELECT database_name FROM organizations WHERE status = 'active'"))
        ).scalars().all()
    for schema in sorted({DEFAULT_SCHEMA if item == "fras" else (item or DEFAULT_SCHEMA) for item in schemas}):
        await ensure_tenant_schema(schema)

    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(outbox_worker(SessionLocal, stop_event))
    face_reaper_task = asyncio.create_task(face_engine_reaper())
    try:
        yield
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()
        face_reaper_task.cancel()
        with suppress(asyncio.CancelledError):
            await face_reaper_task
        unload_face_engine("shutdown")


from slowapi import _rate_limit_exceeded_handler

app = FastAPI(title="Face Recognition Attendance System", version="1.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

if TRUSTED_HOSTS:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=TRUSTED_HOSTS)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("X-Frame-Options", "DENY")
    return response


async def get_db():
    async with SessionLocal() as session:
        yield session


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "organization"


async def run_sql_script(db: AsyncSession, script: str):
    for statement in [part.strip() for part in script.split(";") if part.strip()]:
        await db.execute(text(statement))


async def get_organization_by_slug(db: AsyncSession, slug: str):
    result = await db.execute(
        text(
            """
            SELECT id, name, slug, database_name, status, is_free, seats,
                   price_per_user_per_day, billing_days, advance_amount,
                   dms_base_url, dms_webhook_secret,
                   scanner_override_until, scanner_override_by_admin_id,
                   scanner_override_created_at
            FROM organizations
            WHERE slug = :slug
            """
        ),
        {"slug": slug},
    )
    return result.mappings().first()


async def ensure_tenant_schema(schema: str):
    """Idempotently create a paid org's schema and apply the tenant DDL.

    For the default Delight Model School org the schema is the same as the
    central one (`biomatric`), so this is effectively just `IF NOT EXISTS`
    bookkeeping. Paid orgs get their own `biomatric_tenant_<slug>` schema.
    """
    if schema != DEFAULT_SCHEMA:
        await ensure_schema_exists(schema)

    sessionmaker = get_sessionmaker_for_schema(schema)
    async with sessionmaker() as tenant_db:
        await run_sql_script(tenant_db, TENANT_SCHEMA_SQL)
        for stmt in (
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS person_type VARCHAR(16) NOT NULL DEFAULT 'student'",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_kind VARCHAR(16)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_id UUID",
            "CREATE INDEX IF NOT EXISTS idx_students_dms_person ON students(dms_person_kind, dms_person_id)",
        ):
            await tenant_db.execute(text(stmt))
        await tenant_db.commit()


# Backwards-compat alias: callers still invoking the old name keep working.
ensure_tenant_database = ensure_tenant_schema


async def get_tenant_db(x_org_slug: str | None = Header(default=None)):
    slug = x_org_slug or DEFAULT_ORG_SLUG
    async with SessionLocal() as central_db:
        org = await get_organization_by_slug(central_db, slug)
        if not org:
            raise HTTPException(status_code=404, detail="Organization not found")
        if org["status"] != "active":
            raise HTTPException(
                status_code=402, detail="Organization is not active. Complete advance payment first."
            )
        # The `database_name` column historically held a Postgres database
        # name. After the move to schema-per-tenant it carries the schema
        # name. Existing rows pointing at the legacy 'fras' DB are mapped
        # back to the default schema so old data keeps working.
        schema = org["database_name"] or DEFAULT_SCHEMA
        if schema == "fras":
            schema = DEFAULT_SCHEMA

    sessionmaker = get_sessionmaker_for_schema(schema)
    async with sessionmaker() as tenant_db:
        yield tenant_db


async def _resolve_admin_row(db: AsyncSession, slug: str, username: str):
    result = await db.execute(
        text(
            """
            SELECT oa.id, oa.username, oa.password_hash, oa.full_name,
                   COALESCE(oa.is_active, true) AS is_active,
                   o.id AS organization_id, o.name AS organization_name,
                   o.slug, o.status, o.is_free, o.seats, o.advance_amount,
                   o.dms_base_url, o.dms_webhook_secret,
                   (o.dms_base_url IS NOT NULL AND o.dms_webhook_secret IS NOT NULL) AS dms_linked,
                   o.scanner_override_until, o.scanner_override_by_admin_id,
                   o.scanner_override_created_at
            FROM organization_admins oa
            JOIN organizations o ON o.id = oa.organization_id
            WHERE o.slug = :slug AND oa.username = :username
            """
        ),
        {"slug": slug, "username": username},
    )
    return result.mappings().first()


async def require_admin(
    x_org_slug: str | None = Header(default=None),
    x_admin_username: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
):
    slug = x_org_slug or DEFAULT_ORG_SLUG
    if not x_admin_username or not x_admin_token:
        raise HTTPException(status_code=401, detail="Admin login required")

    async with SessionLocal() as db:
        row = await _resolve_admin_row(db, slug, x_admin_username)
        if not row or row["status"] != "active" or not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid admin login")
        if not verify_admin_token(row["slug"], row["username"], row["password_hash"], x_admin_token):
            raise HTTPException(status_code=401, detail="Invalid admin login")
        return dict(row)


async def require_operator(
    x_org_slug: str | None = Header(default=None),
    x_user_username: str | None = Header(default=None),
    x_user_token: str | None = Header(default=None),
    x_admin_username: str | None = Header(default=None),
    x_admin_token: str | None = Header(default=None),
):
    slug = x_org_slug or DEFAULT_ORG_SLUG
    username = x_user_username or x_admin_username
    token = x_user_token or x_admin_token
    if not username or not token:
        raise HTTPException(status_code=401, detail="Attendance login required")

    async with SessionLocal() as db:
        row = await _resolve_admin_row(db, slug, username)
        if not row or row["status"] != "active" or not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid attendance login")
        if not verify_admin_token(row["slug"], row["username"], row["password_hash"], token):
            raise HTTPException(status_code=401, detail="Invalid attendance login")
        return dict(row)


@app.get("/health")
async def health():
    return {"ok": True, "version": app.version}


@app.get("/")
async def root():
    return {"service": "Face Recognition Attendance API", "health": "/health"}


@app.post("/scanner/warmup")
@limiter.limit("20/minute")
async def warmup_scanner(request: Request, operator: dict = Depends(require_operator)):
    get_face_engine(operator)
    return {
        "ok": True,
        "scanner": "ready",
        "schedule": face_engine_schedule_label(),
        "schedule_human": face_engine_schedule_human(),
        "override_until": str(operator.get("scanner_override_until") or ""),
    }


@app.post("/scanner/override")
@limiter.limit("5/minute")
async def scanner_override(
    request: Request,
    organization_slug: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    normalized_username = username.strip().lower()
    async with SessionLocal() as db:
        row = await _resolve_admin_row(db, organization_slug, normalized_username)
        if not row or row["status"] != "active" or not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid admin login")
        ok, needs_rehash = verify_password(password, row["password_hash"])
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid admin login")
        password_hash = row["password_hash"]
        if needs_rehash:
            password_hash = hash_password(password)
            await db.execute(
                text("UPDATE organization_admins SET password_hash = :h WHERE id = :id"),
                {"h": password_hash, "id": row["id"]},
            )

        override_until = datetime.now(timezone.utc) + timedelta(minutes=SCANNER_OVERRIDE_MINUTES)
        await db.execute(
            text(
                """
                UPDATE organizations
                SET scanner_override_until = :override_until,
                    scanner_override_by_admin_id = :admin_id,
                    scanner_override_created_at = now()
                WHERE id = :organization_id
                """
            ),
            {
                "override_until": override_until,
                "admin_id": row["id"],
                "organization_id": row["organization_id"],
            },
        )
        await db.commit()

    remember_active_override({"scanner_override_until": override_until})
    return {
        "ok": True,
        "scanner": "unlocked",
        "override_minutes": SCANNER_OVERRIDE_MINUTES,
        "override_until": override_until.isoformat(),
        "schedule": face_engine_schedule_label(),
        "schedule_human": face_engine_schedule_human(),
        "token": admin_token(row["slug"], row["username"], password_hash),
        "organization": {
            "id": row["organization_id"],
            "name": row["organization_name"],
            "slug": row["slug"],
            "is_free": row["is_free"],
            "seats": row["seats"],
            "advance_amount": float(row["advance_amount"]),
            "dms_linked": bool(row["dms_linked"]),
        },
        "admin": {"username": row["username"], "full_name": row["full_name"]},
    }


@app.get("/organizations")
async def list_organizations():
    async with SessionLocal() as db:
        res = await db.execute(
            text(
                """
                SELECT id, name, slug, org_type, status, is_free, seats,
                       price_per_user_per_day, billing_days, advance_amount,
                       (dms_base_url IS NOT NULL AND dms_webhook_secret IS NOT NULL) AS dms_linked
                FROM organizations
                WHERE status = 'active'
                ORDER BY is_free DESC, name ASC
                """
            )
        )
        return {"items": [dict(row) for row in res.mappings().all()]}


@app.get("/billing/price")
async def billing_price():
    return {
        "currency": "INR",
        "price_per_user_per_day": float(PRICE_PER_USER_PER_DAY),
        "default_billing_days": DEFAULT_BILLING_DAYS,
    }


@app.post("/organizations/register")
@limiter.limit("5/minute")
async def register_organization(
    request: Request,
    organization_name: str = Form(...),
    org_type: str = Form("school"),
    contact_name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(""),
    seats: int = Form(...),
    billing_days: int = Form(DEFAULT_BILLING_DAYS),
    payment_reference: str = Form(...),
    admin_full_name: str = Form(...),
    admin_username: str = Form(...),
    admin_password: str = Form(...),
):
    if seats < 1:
        raise HTTPException(status_code=400, detail="Number of users must be at least 1")
    if billing_days < 1:
        raise HTTPException(status_code=400, detail="Billing days must be at least 1")
    if len(admin_password.strip()) < 8 and not DEV_MODE:
        raise HTTPException(status_code=400, detail="Admin password must be at least 8 characters")
    if not payment_reference.strip():
        raise HTTPException(status_code=400, detail="Advance payment reference is required")

    base_slug = slugify(organization_name)
    async with SessionLocal() as db:
        slug = base_slug
        suffix = 2
        while await get_organization_by_slug(db, slug):
            slug = f"{base_slug}-{suffix}"
            suffix += 1

        db_name = safe_schema_name(slug.replace("-", "_"))
        await ensure_tenant_schema(db_name)

        advance_amount = PRICE_PER_USER_PER_DAY * Decimal(seats) * Decimal(billing_days)
        org_row = await db.execute(
            text(
                """
                INSERT INTO organizations (
                  name, slug, org_type, contact_name, phone, email, database_name,
                  status, is_free, seats, price_per_user_per_day, billing_days,
                  advance_amount, payment_reference
                )
                VALUES (
                  :name, :slug, :org_type, :contact_name, :phone, :email, :database_name,
                  'active', false, :seats, :price, :billing_days, :advance_amount,
                  :payment_reference
                )
                RETURNING id, name, slug, database_name, seats, advance_amount
                """
            ),
            {
                "name": organization_name.strip(),
                "slug": slug,
                "org_type": org_type.strip() or "school",
                "contact_name": contact_name.strip(),
                "phone": phone.strip(),
                "email": email.strip(),
                "database_name": db_name,
                "seats": seats,
                "price": PRICE_PER_USER_PER_DAY,
                "billing_days": billing_days,
                "advance_amount": advance_amount,
                "payment_reference": payment_reference.strip(),
            },
        )
        org = org_row.mappings().first()
        await db.execute(
            text(
                """
                INSERT INTO organization_admins (organization_id, username, password_hash, full_name, is_active)
                VALUES (:organization_id, :username, :password_hash, :full_name, true)
                """
            ),
            {
                "organization_id": org["id"],
                "username": admin_username.strip(),
                "password_hash": hash_password(admin_password),
                "full_name": admin_full_name.strip(),
            },
        )
        await db.execute(
            text(
                """
                INSERT INTO payments (organization_id, amount, status, reference, notes)
                VALUES (:organization_id, :amount, 'paid', :reference, :notes)
                """
            ),
            {
                "organization_id": org["id"],
                "amount": advance_amount,
                "reference": payment_reference.strip(),
                "notes": f"Advance payment for {seats} users x {billing_days} days at INR {PRICE_PER_USER_PER_DAY}/day",
            },
        )
        await db.commit()

    return {
        "registered": True,
        "organization": {
            "name": org["name"],
            "slug": org["slug"],
            "database_name": org["database_name"],
            "seats": org["seats"],
            "advance_amount": float(org["advance_amount"]),
        },
        "message": "Organization activated and separate tenant database created.",
    }


@app.post("/auth/login")
@limiter.limit("10/minute")
async def admin_login(
    request: Request,
    organization_slug: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    async with SessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT oa.id, oa.username, oa.password_hash, oa.full_name,
                       COALESCE(oa.is_active, true) AS is_active,
                       o.id AS organization_id, o.name AS organization_name, o.slug,
                       o.status, o.is_free, o.seats, o.advance_amount,
                       (o.dms_base_url IS NOT NULL AND o.dms_webhook_secret IS NOT NULL) AS dms_linked
                FROM organization_admins oa
                JOIN organizations o ON o.id = oa.organization_id
                WHERE o.slug = :slug AND oa.username = :username
                """
            ),
            {"slug": organization_slug, "username": username.strip()},
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=401, detail="Invalid company/admin login")
        if not row["is_active"]:
            raise HTTPException(status_code=401, detail="Invalid company/admin login")

        ok, needs_rehash = verify_password(password, row["password_hash"])
        if not ok:
            raise HTTPException(status_code=401, detail="Invalid company/admin login")
        if row["status"] != "active":
            raise HTTPException(status_code=402, detail="Organization is not active")

        if needs_rehash:
            new_hash = hash_password(password)
            await db.execute(
                text("UPDATE organization_admins SET password_hash = :h WHERE id = :id"),
                {"h": new_hash, "id": row["id"]},
            )
            await db.commit()
            password_hash_for_token = new_hash
        else:
            password_hash_for_token = row["password_hash"]

    token = admin_token(row["slug"], row["username"], password_hash_for_token)
    return {
        "authenticated": True,
        "organization": {
            "id": row["organization_id"],
            "name": row["organization_name"],
            "slug": row["slug"],
            "is_free": row["is_free"],
            "seats": row["seats"],
            "advance_amount": float(row["advance_amount"]),
            "dms_linked": bool(row["dms_linked"]),
        },
        "admin": {
            "username": row["username"],
            "full_name": row["full_name"],
        },
        "token": token,
    }


@app.post("/auth/password-reset/request")
@limiter.limit("5/minute")
async def request_password_reset(
    request: Request,
    organization_slug: str = Form(...),
    email: str = Form(...),
):
    normalized_email = email.strip().lower()
    generic_response = {
        "sent": True,
        "message": "If this email is registered, a reset link has been sent.",
    }
    if not normalized_email:
        return generic_response

    async with SessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT oa.id AS admin_id, o.id AS organization_id,
                       o.name AS organization_name, o.slug
                FROM organization_admins oa
                JOIN organizations o ON o.id = oa.organization_id
                WHERE o.slug = :slug
                  AND oa.username = :email
                  AND o.status = 'active'
                  AND COALESCE(oa.is_active, true) = true
                """
            ),
            {"slug": organization_slug, "email": normalized_email},
        )
        row = result.mappings().first()
        if not row:
            return generic_response
        if not password_reset_email_ready():
            raise HTTPException(status_code=503, detail="Password reset email is not configured.")

        token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=PASSWORD_RESET_TOKEN_TTL_MINUTES)
        await db.execute(
            text(
                """
                UPDATE password_reset_tokens
                SET used_at = now()
                WHERE admin_id = :admin_id AND used_at IS NULL
                """
            ),
            {"admin_id": row["admin_id"]},
        )
        await db.execute(
            text(
                """
                INSERT INTO password_reset_tokens (organization_id, admin_id, token_hash, expires_at)
                VALUES (:organization_id, :admin_id, :token_hash, :expires_at)
                """
            ),
            {
                "organization_id": row["organization_id"],
                "admin_id": row["admin_id"],
                "token_hash": password_reset_token_hash(token),
                "expires_at": expires_at,
            },
        )
        await db.commit()

    reset_link = password_reset_link(row["slug"], normalized_email, token)
    try:
        await asyncio.to_thread(send_password_reset_email, normalized_email, row["organization_name"], reset_link)
    except Exception as exc:
        LOGGER.exception("Password reset email failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not send reset email right now.")
    return generic_response


@app.post("/auth/password-reset/confirm")
@limiter.limit("5/minute")
async def confirm_password_reset(
    request: Request,
    organization_slug: str = Form(...),
    email: str = Form(...),
    token: str = Form(...),
    new_password: str = Form(...),
):
    normalized_email = email.strip().lower()
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters.")

    async with SessionLocal() as db:
        result = await db.execute(
            text(
                """
                SELECT prt.id AS token_id, oa.id AS admin_id
                FROM password_reset_tokens prt
                JOIN organization_admins oa ON oa.id = prt.admin_id
                JOIN organizations o ON o.id = prt.organization_id
                WHERE o.slug = :slug
                  AND oa.username = :email
                  AND COALESCE(oa.is_active, true) = true
                  AND prt.token_hash = :token_hash
                  AND prt.used_at IS NULL
                  AND prt.expires_at > now()
                """
            ),
            {
                "slug": organization_slug,
                "email": normalized_email,
                "token_hash": password_reset_token_hash(token.strip()),
            },
        )
        row = result.mappings().first()
        if not row:
            raise HTTPException(status_code=400, detail="Reset link is invalid or expired.")

        await db.execute(
            text("UPDATE organization_admins SET password_hash = :hash WHERE id = :admin_id"),
            {"hash": hash_password(new_password), "admin_id": row["admin_id"]},
        )
        await db.execute(
            text("UPDATE password_reset_tokens SET used_at = now() WHERE id = :token_id"),
            {"token_id": row["token_id"]},
        )
        await db.commit()
    return {"reset": True, "message": "Password updated. Please login with the new password."}


def to_vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"


def validate_client_embedding(embedding, expected_dim: int = CLIENT_FACE_EMBEDDING_DIM) -> list[float]:
    if not isinstance(embedding, list) or len(embedding) != expected_dim:
        raise HTTPException(status_code=400, detail=f"embedding must be a {expected_dim}-number list")

    cleaned = []
    for value in embedding:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="embedding contains a non-numeric value")
        if not math.isfinite(number):
            raise HTTPException(status_code=400, detail="embedding contains an invalid number")
        cleaned.append(number)

    norm = math.sqrt(sum(value * value for value in cleaned))
    if norm <= 0.0001:
        raise HTTPException(status_code=400, detail="embedding is empty")
    return cleaned


def validate_client_embeddings(raw_embeddings, min_count: int = 1, max_count: int = 10) -> list[list[float]]:
    if not isinstance(raw_embeddings, list) or len(raw_embeddings) < min_count:
        raise HTTPException(status_code=400, detail=f"Capture at least {min_count} client face samples")
    if len(raw_embeddings) > max_count:
        raw_embeddings = raw_embeddings[:max_count]
    return [validate_client_embedding(item) for item in raw_embeddings]


def client_confidence(distance: float, threshold: float = CLIENT_FACE_MATCH_THRESHOLD) -> int:
    if threshold <= 0:
        return 0
    return max(0, min(100, round((1.0 - (distance / threshold)) * 100)))


def median_distance(values: list[float]) -> float:
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2


def should_check_liveness() -> bool:
    return LIVENESS_MODE in {"basic", "strict"}


def normalize_person_type(person_type: str) -> str:
    normalized = person_type.strip().lower()
    if normalized not in VALID_PERSON_TYPES:
        raise HTTPException(status_code=400, detail="person_type must be student, staff, or teacher")
    return normalized


def _coerce_uuid(value: str | None):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise HTTPException(status_code=400, detail="dms_person_id must be a valid UUID")


async def embedding_from_upload(upload: UploadFile, access_context: dict | None = None):
    engine = get_face_engine(access_context)
    img_bytes = await read_image_upload(upload)
    img = engine.decode_image(img_bytes)
    if img is None:
        return None, None, "Invalid image"

    emb, det_score = engine.get_embedding(img)
    if emb is None:
        return None, None, "No face detected"

    return emb, det_score, None


async def embeddings_from_uploads(
    images: list[UploadFile],
    min_count: int = 1,
    max_count: int = 10,
    access_context: dict | None = None,
):
    if len(images) < min_count:
        raise HTTPException(status_code=400, detail=f"Capture at least {min_count} face samples")

    embeddings = []
    for index, upload in enumerate(images[:max_count], start=1):
        emb, det_score, error = await embedding_from_upload(upload, access_context)
        if error:
            raise HTTPException(status_code=422, detail=f"Sample {index}: {error}")
        embeddings.append((emb, det_score))

    return embeddings


async def attendance_embeddings_from_uploads(
    images: list[UploadFile],
    min_count: int = 2,
    max_count: int = 3,
    access_context: dict | None = None,
):
    if len(images) < min_count:
        raise HTTPException(status_code=400, detail=f"Capture at least {min_count} scan frames")

    engine = get_face_engine(access_context)
    embeddings = []
    errors = []
    for index, upload in enumerate(images[:max_count], start=1):
        img_bytes = await read_image_upload(upload)
        img = engine.decode_image(img_bytes)
        if img is None:
            errors.append(f"Frame {index}: invalid image")
            continue

        if should_check_liveness() and not engine.liveness_basic(img):
            errors.append(f"Frame {index}: liveness check failed")
            continue

        emb, det_score = engine.get_embedding(img)
        if emb is None:
            errors.append(f"Frame {index}: no face detected")
            continue
        embeddings.append((emb, det_score))

    if len(embeddings) < min_count:
        return embeddings, errors

    return embeddings, []


async def find_server_attendance_match(db: AsyncSession, embeddings: list[tuple[list[float], float]]):
    candidate_limit = max(5, min(FACE_SCAN_CANDIDATES, 50))
    required_hits = 1 if len(embeddings) == 1 else min(
        max(FACE_MULTI_MATCH_MIN_HITS, 2),
        len(embeddings),
    )
    candidates = {}
    nearest_distance = None

    for emb, _ in embeddings:
        result = await db.execute(
            text(
                """
                SELECT fe.student_id, s.full_name, s.person_type,
                       s.dms_person_kind, s.dms_person_id, s.student_code,
                       (fe.embedding <=> CAST(:emb AS vector)) AS distance
                FROM face_embeddings fe
                JOIN students s ON s.id = fe.student_id
                ORDER BY fe.embedding <=> CAST(:emb AS vector)
                LIMIT :candidate_limit
                """
            ),
            {"emb": to_vector_literal(emb), "candidate_limit": candidate_limit},
        )
        frame_best = {}
        for row in result.mappings().all():
            distance = float(row["distance"])
            nearest_distance = distance if nearest_distance is None else min(nearest_distance, distance)
            existing = frame_best.get(row["student_id"])
            if existing is None or distance < existing["distance"]:
                frame_best[row["student_id"]] = {**dict(row), "distance": distance}

        for row in frame_best.values():
            student_id = row["student_id"]
            distance = row["distance"]
            candidate = candidates.setdefault(
                student_id,
                {
                    "student_id": student_id,
                    "full_name": row["full_name"],
                    "person_type": row["person_type"],
                    "dms_person_kind": row["dms_person_kind"],
                    "dms_person_id": row["dms_person_id"],
                    "student_code": row["student_code"],
                    "hit_distances": [],
                },
            )
            if distance <= THRESH:
                candidate["hit_distances"].append(distance)

    eligible = []
    for candidate in candidates.values():
        hits = len(candidate["hit_distances"])
        if hits < required_hits:
            continue
        eligible.append(
            {
                **candidate,
                "distance": median_distance(candidate["hit_distances"]),
                "hits": hits,
            }
        )

    eligible.sort(key=lambda item: (item["distance"], -item["hits"]))
    row = eligible[0] if eligible else None
    if row and len(eligible) > 1:
        second = eligible[1]
        if second["distance"] - row["distance"] < FACE_MATCH_MARGIN:
            row = None

    return row, nearest_distance


async def find_duplicate_face(
    db: AsyncSession,
    embeddings: list[tuple[list[float], float]],
    exclude_student_code: str = "",
):
    best = None
    exclude_code = exclude_student_code.strip()

    for emb, _ in embeddings:
        result = await db.execute(
            text(
                """
                SELECT fe.student_id, s.student_code, s.full_name, s.person_type,
                       (fe.embedding <=> CAST(:emb AS vector)) AS distance
                FROM face_embeddings fe
                JOIN students s ON s.id = fe.student_id
                WHERE (:exclude_code = '' OR s.student_code <> :exclude_code)
                ORDER BY fe.embedding <=> CAST(:emb AS vector)
                LIMIT 1
                """
            ),
            {"emb": to_vector_literal(emb), "exclude_code": exclude_code},
        )
        row = result.mappings().first()
        if row and (best is None or float(row["distance"]) < best["distance"]):
            best = {
                "student_id": row["student_id"],
                "student_code": row["student_code"],
                "full_name": row["full_name"],
                "person_type": row["person_type"],
                "distance": float(row["distance"]),
            }

    if not best:
        return None

    best["confidence"] = max(0, min(100, round((1.0 - best["distance"]) * 100)))
    return best


async def ensure_not_duplicate_face(
    db: AsyncSession,
    embeddings: list[tuple[list[float], float]],
    student_code: str,
    allow_duplicate: bool,
):
    duplicate = await find_duplicate_face(db, embeddings, exclude_student_code=student_code)
    is_duplicate = bool(duplicate and duplicate["distance"] <= DUPLICATE_THRESH)

    if is_duplicate and not allow_duplicate:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Possible duplicate face found",
                "match": duplicate,
                "threshold": DUPLICATE_THRESH,
            },
        )

    return duplicate if is_duplicate else None


async def find_duplicate_client_face(
    db: AsyncSession,
    embeddings: list[list[float]],
    exclude_student_code: str = "",
    model_name: str = CLIENT_FACE_MODEL_NAME,
    model_version: str = CLIENT_FACE_MODEL_VERSION,
):
    best = None
    exclude_code = exclude_student_code.strip()

    for emb in embeddings:
        result = await db.execute(
            text(
                """
                SELECT cfe.student_id, s.student_code, s.full_name, s.person_type,
                       (cfe.embedding <-> CAST(:emb AS vector)) AS distance
                FROM client_face_embeddings cfe
                JOIN students s ON s.id = cfe.student_id
                WHERE cfe.model_name = :model_name
                  AND cfe.model_version = :model_version
                  AND (:exclude_code = '' OR s.student_code <> :exclude_code)
                ORDER BY cfe.embedding <-> CAST(:emb AS vector)
                LIMIT 1
                """
            ),
            {
                "emb": to_vector_literal(emb),
                "exclude_code": exclude_code,
                "model_name": model_name,
                "model_version": model_version,
            },
        )
        row = result.mappings().first()
        if row and (best is None or float(row["distance"]) < best["distance"]):
            distance = float(row["distance"])
            best = {
                "student_id": row["student_id"],
                "student_code": row["student_code"],
                "full_name": row["full_name"],
                "person_type": row["person_type"],
                "distance": distance,
                "confidence": client_confidence(distance, CLIENT_FACE_DUPLICATE_THRESHOLD),
            }

    return best


async def ensure_not_duplicate_client_face(
    db: AsyncSession,
    embeddings: list[list[float]],
    student_code: str,
    allow_duplicate: bool,
    model_name: str,
    model_version: str,
):
    duplicate = await find_duplicate_client_face(
        db,
        embeddings,
        exclude_student_code=student_code,
        model_name=model_name,
        model_version=model_version,
    )
    is_duplicate = bool(duplicate and duplicate["distance"] <= CLIENT_FACE_DUPLICATE_THRESHOLD)

    if is_duplicate and not allow_duplicate:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Possible duplicate face found",
                "match": duplicate,
                "threshold": CLIENT_FACE_DUPLICATE_THRESHOLD,
            },
        )

    return duplicate if is_duplicate else None


async def _upsert_student(
    db: AsyncSession,
    student_code: str,
    full_name: str,
    person_type: str,
    dms_person_kind: str | None,
    dms_person_id: UUID | None,
    clear_server_embeddings: bool = True,
):
    existing = await db.execute(
        text("SELECT id FROM students WHERE student_code = :code"), {"code": student_code}
    )
    existing_student = existing.first()
    if existing_student:
        result = await db.execute(
            text(
                """
                UPDATE students
                SET full_name = :name,
                    person_type = :person_type,
                    dms_person_kind = :dms_kind,
                    dms_person_id = :dms_id
                WHERE id = :sid
                RETURNING id, student_code, full_name, person_type, dms_person_kind, dms_person_id
                """
            ),
            {
                "sid": existing_student.id,
                "name": full_name,
                "person_type": person_type,
                "dms_kind": dms_person_kind,
                "dms_id": str(dms_person_id) if dms_person_id else None,
            },
        )
        if clear_server_embeddings:
            await db.execute(
                text("DELETE FROM face_embeddings WHERE student_id = :sid"),
                {"sid": existing_student.id},
            )
        return result.first(), True

    result = await db.execute(
        text(
            """
            INSERT INTO students (student_code, full_name, person_type, dms_person_kind, dms_person_id)
            VALUES (:code, :name, :person_type, :dms_kind, :dms_id)
            RETURNING id, student_code, full_name, person_type, dms_person_kind, dms_person_id
            """
        ),
        {
            "code": student_code,
            "name": full_name,
            "person_type": person_type,
            "dms_kind": dms_person_kind,
            "dms_id": str(dms_person_id) if dms_person_id else None,
        },
    )
    return result.first(), False


@app.post("/students/register-samples")
async def register_student_samples(
    student_code: str = Form(...),
    full_name: str = Form(...),
    person_type: str = Form("student"),
    allow_duplicate: bool = Form(False),
    dms_person_kind: str = Form(""),
    dms_person_id: str = Form(""),
    images: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    _admin: dict = Depends(require_admin),
):
    person_type = normalize_person_type(person_type)
    dms_kind = (dms_person_kind or "").strip().lower() or None
    if dms_kind and dms_kind not in {"student", "teacher"}:
        raise HTTPException(status_code=400, detail="dms_person_kind must be student or teacher")
    dms_uuid = _coerce_uuid(dms_person_id) if dms_kind else None

    embeddings = await embeddings_from_uploads(images, min_count=5, access_context=_admin)
    duplicate = await ensure_not_duplicate_face(db, embeddings, student_code, allow_duplicate)

    s, re_enrolled = await _upsert_student(
        db, student_code, full_name, person_type, dms_kind, dms_uuid
    )

    for emb, det_score in embeddings:
        await db.execute(
            text(
                """
                INSERT INTO face_embeddings (student_id, embedding, quality_score)
                VALUES (:sid, CAST(:emb AS vector), :q)
                """
            ),
            {"sid": s.id, "emb": to_vector_literal(emb), "q": int(float(det_score) * 100)},
        )

    await db.commit()

    return {
        "id": s.id,
        "student_code": s.student_code,
        "full_name": s.full_name,
        "person_type": s.person_type,
        "dms_person_kind": s.dms_person_kind,
        "dms_person_id": str(s.dms_person_id) if s.dms_person_id else None,
        "sample_count": len(embeddings),
        "re_enrolled": re_enrolled,
        "duplicate_override": bool(duplicate),
    }


@app.post("/students/register")
async def register_student(
    student_code: str = Form(...),
    full_name: str = Form(...),
    person_type: str = Form("student"),
    allow_duplicate: bool = Form(False),
    dms_person_kind: str = Form(""),
    dms_person_id: str = Form(""),
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    _admin: dict = Depends(require_admin),
):
    person_type = normalize_person_type(person_type)
    dms_kind = (dms_person_kind or "").strip().lower() or None
    if dms_kind and dms_kind not in {"student", "teacher"}:
        raise HTTPException(status_code=400, detail="dms_person_kind must be student or teacher")
    dms_uuid = _coerce_uuid(dms_person_id) if dms_kind else None

    emb, det_score, error = await embedding_from_upload(image, _admin)
    if error:
        raise HTTPException(status_code=422, detail=error)
    await ensure_not_duplicate_face(db, [(emb, det_score)], student_code, allow_duplicate)

    s, _ = await _upsert_student(db, student_code, full_name, person_type, dms_kind, dms_uuid)

    await db.execute(
        text(
            """
            INSERT INTO face_embeddings (student_id, embedding, quality_score)
            VALUES (:sid, CAST(:emb AS vector), :q)
            """
        ),
        {"sid": s.id, "emb": to_vector_literal(emb), "q": int(float(det_score) * 100)},
    )
    await db.commit()

    return {
        "id": s.id,
        "student_code": s.student_code,
        "full_name": s.full_name,
        "person_type": s.person_type,
        "dms_person_kind": s.dms_person_kind,
        "dms_person_id": str(s.dms_person_id) if s.dms_person_id else None,
    }


@app.post("/students/check-duplicate")
async def check_duplicate_student(
    student_code: str = Form(""),
    images: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    _admin: dict = Depends(require_admin),
):
    embeddings = await embeddings_from_uploads(images, min_count=1, access_context=_admin)
    match = await find_duplicate_face(db, embeddings, exclude_student_code=student_code)
    duplicate = bool(match and match["distance"] <= DUPLICATE_THRESH)

    return {
        "duplicate": duplicate,
        "match": match if duplicate else None,
        "nearest_match": match,
        "threshold": DUPLICATE_THRESH,
        "sample_count": len(embeddings),
    }


@app.post("/students/check-duplicate-client")
async def check_duplicate_student_client(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_tenant_db),
    _admin: dict = Depends(require_admin),
):
    embeddings = validate_client_embeddings(payload.get("embeddings"), min_count=1)
    student_code = str(payload.get("student_code") or "")
    model_name = str(payload.get("model_name") or CLIENT_FACE_MODEL_NAME)
    model_version = str(payload.get("model_version") or CLIENT_FACE_MODEL_VERSION)
    match = await find_duplicate_client_face(
        db,
        embeddings,
        exclude_student_code=student_code,
        model_name=model_name,
        model_version=model_version,
    )
    duplicate = bool(match and match["distance"] <= CLIENT_FACE_DUPLICATE_THRESHOLD)

    return {
        "duplicate": duplicate,
        "match": match if duplicate else None,
        "nearest_match": match,
        "threshold": CLIENT_FACE_DUPLICATE_THRESHOLD,
        "sample_count": len(embeddings),
    }


@app.post("/students/register-client-samples")
async def register_student_client_samples(
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_tenant_db),
    _admin: dict = Depends(require_admin),
):
    student_code = str(payload.get("student_code") or "").strip()
    full_name = str(payload.get("full_name") or "").strip()
    if not student_code or not full_name:
        raise HTTPException(status_code=400, detail="student_code and full_name are required")

    person_type = normalize_person_type(str(payload.get("person_type") or "student"))
    allow_duplicate = bool(payload.get("allow_duplicate", False))
    dms_kind = str(payload.get("dms_person_kind") or "").strip().lower() or None
    if dms_kind and dms_kind not in {"student", "teacher"}:
        raise HTTPException(status_code=400, detail="dms_person_kind must be student or teacher")
    dms_uuid = _coerce_uuid(payload.get("dms_person_id")) if dms_kind else None

    model_name = str(payload.get("model_name") or CLIENT_FACE_MODEL_NAME)
    model_version = str(payload.get("model_version") or CLIENT_FACE_MODEL_VERSION)
    embeddings = validate_client_embeddings(payload.get("embeddings"), min_count=5)
    quality_scores = payload.get("quality_scores") or []

    duplicate = await ensure_not_duplicate_client_face(
        db,
        embeddings,
        student_code,
        allow_duplicate,
        model_name,
        model_version,
    )

    s, re_enrolled = await _upsert_student(
        db,
        student_code,
        full_name,
        person_type,
        dms_kind,
        dms_uuid,
        clear_server_embeddings=False,
    )
    await db.execute(text("DELETE FROM client_face_embeddings WHERE student_id = :sid"), {"sid": s.id})

    for index, emb in enumerate(embeddings):
        raw_quality = quality_scores[index] if index < len(quality_scores) else 1.0
        try:
            quality = int(float(raw_quality) * 100)
        except (TypeError, ValueError):
            quality = 100
        await db.execute(
            text(
                """
                INSERT INTO client_face_embeddings
                  (student_id, model_name, model_version, embedding, quality_score)
                VALUES (:sid, :model_name, :model_version, CAST(:emb AS vector), :q)
                """
            ),
            {
                "sid": s.id,
                "model_name": model_name,
                "model_version": model_version,
                "emb": to_vector_literal(emb),
                "q": max(0, min(100, quality)),
            },
        )

    await db.commit()

    return {
        "id": s.id,
        "student_code": s.student_code,
        "full_name": s.full_name,
        "person_type": s.person_type,
        "dms_person_kind": s.dms_person_kind,
        "dms_person_id": str(s.dms_person_id) if s.dms_person_id else None,
        "sample_count": len(embeddings),
        "re_enrolled": re_enrolled,
        "duplicate_override": bool(duplicate),
        "model_name": model_name,
        "model_version": model_version,
    }


async def finalize_attendance_match(
    db: AsyncSession,
    operator: dict,
    row,
    distance: float,
    confidence: int,
    engine_name: str,
):
    existing_log = await db.execute(
        text(
            """
            SELECT id, marked_at
            FROM attendance_logs
            WHERE student_id = :sid
              AND (marked_at AT TIME ZONE :tz)::date = (now() AT TIME ZONE :tz)::date
            ORDER BY marked_at DESC
            LIMIT 1
            """
        ),
        {"sid": row["student_id"], "tz": APP_TIMEZONE},
    )
    existing = existing_log.first()
    if existing:
        return {
            "matched": True,
            "already_marked": True,
            "student_id": row["student_id"],
            "name": row["full_name"],
            "person_type": row["person_type"],
            "distance": distance,
            "confidence": confidence,
            "attendance_id": existing.id,
            "marked_at": str(existing.marked_at),
            "dms_synced": bool(row["dms_person_id"]),
        }

    log = await db.execute(
        text(
            """
            INSERT INTO attendance_logs (student_id, status, confidence)
            VALUES (:sid, 'present', :conf)
            RETURNING id, marked_at
            """
        ),
        {"sid": row["student_id"], "conf": confidence},
    )
    l = log.first()
    await db.commit()

    if row["dms_person_kind"] and row["dms_person_id"] and operator.get("dms_base_url") and operator.get("dms_webhook_secret"):
        async with SessionLocal() as central_db:
            await enqueue_attendance(
                central_db,
                organization_id=operator["organization_id"],
                person_kind=row["dms_person_kind"],
                person_id=str(row["dms_person_id"]),
                marked_at=l.marked_at if isinstance(l.marked_at, datetime) else datetime.now(timezone.utc),
                confidence=confidence,
                source_ref=f"biomatric:{operator['slug']}:{row['student_code']}",
            )
            await central_db.commit()

    return {
        "matched": True,
        "already_marked": False,
        "student_id": row["student_id"],
        "name": row["full_name"],
        "person_type": row["person_type"],
        "distance": distance,
        "confidence": confidence,
        "attendance_id": l.id,
        "marked_at": str(l.marked_at),
        "dms_synced": bool(row["dms_person_kind"] and row["dms_person_id"]),
    }


@app.post("/attendance/mark")
@limiter.limit("60/minute")
async def mark_attendance(
    request: Request,
    image: UploadFile = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    operator: dict = Depends(require_operator),
):
    engine = get_face_engine(operator)
    img_bytes = await read_image_upload(image)
    img = engine.decode_image(img_bytes)
    if img is None:
        raise HTTPException(status_code=400, detail="Invalid image")

    if should_check_liveness() and not engine.liveness_basic(img):
        raise HTTPException(status_code=403, detail="Liveness check failed")

    emb, _ = engine.get_embedding(img)
    if emb is None:
        raise HTTPException(status_code=422, detail="No face detected")

    result = await db.execute(
        text(
            """
            SELECT fe.student_id, s.full_name, s.person_type,
                   s.dms_person_kind, s.dms_person_id, s.student_code,
                   (fe.embedding <=> CAST(:emb AS vector)) AS distance
            FROM face_embeddings fe
            JOIN students s ON s.id = fe.student_id
            ORDER BY fe.embedding <=> CAST(:emb AS vector)
            LIMIT 1
            """
        ),
        {"emb": to_vector_literal(emb)},
    )
    row = result.mappings().first()

    if not row or float(row["distance"]) > THRESH:
        return {
            "matched": False,
            "reason": "unknown_face",
            "distance": None if not row else float(row["distance"]),
        }

    distance = float(row["distance"])
    return await finalize_attendance_match(
        db,
        operator,
        row,
        distance=distance,
        confidence=max(0, min(100, int((1.0 - distance) * 100))),
        engine_name="server",
    )


@app.post("/attendance/mark-samples")
@limiter.limit("40/minute")
async def mark_attendance_samples(
    request: Request,
    images: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_tenant_db),
    operator: dict = Depends(require_operator),
):
    embeddings, errors = await attendance_embeddings_from_uploads(
        images,
        min_count=2,
        max_count=3,
        access_context=operator,
    )
    if len(embeddings) < 2:
        return {
            "matched": False,
            "reason": "unclear_face",
            "distance": None,
            "confidence": 0,
            "frames_used": len(embeddings),
            "errors": errors[:3],
        }

    row, nearest_distance = await find_server_attendance_match(db, embeddings)
    if not row:
        return {
            "matched": False,
            "reason": "unknown_face",
            "distance": nearest_distance,
            "confidence": 0 if nearest_distance is None else max(0, min(100, int((1.0 - nearest_distance) * 100))),
            "frames_used": len(embeddings),
        }

    distance = float(row["distance"])
    result = await finalize_attendance_match(
        db,
        operator,
        row,
        distance=distance,
        confidence=max(0, min(100, int((1.0 - distance) * 100))),
        engine_name="server",
    )
    result["frames_used"] = len(embeddings)
    result["vote_hits"] = row.get("hits", len(embeddings))
    return result


@app.post("/attendance/mark-client")
@limiter.limit("90/minute")
async def mark_attendance_client(
    request: Request,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_tenant_db),
    operator: dict = Depends(require_operator),
):
    raw_embeddings = payload.get("embeddings")
    if raw_embeddings is not None:
        embeddings = validate_client_embeddings(raw_embeddings, min_count=1, max_count=5)
    else:
        embeddings = [validate_client_embedding(payload.get("embedding"))]
    model_name = str(payload.get("model_name") or CLIENT_FACE_MODEL_NAME)
    model_version = str(payload.get("model_version") or CLIENT_FACE_MODEL_VERSION)

    candidate_limit = max(5, min(CLIENT_FACE_SCAN_CANDIDATES, 50))
    required_hits = 1 if len(embeddings) == 1 else min(
        max(CLIENT_FACE_MULTI_MATCH_MIN_HITS, 2),
        len(embeddings),
    )
    candidates = {}
    nearest_distance = None

    for embedding in embeddings:
        result = await db.execute(
            text(
                """
                SELECT cfe.student_id, s.full_name, s.person_type,
                       s.dms_person_kind, s.dms_person_id, s.student_code,
                       (cfe.embedding <-> CAST(:emb AS vector)) AS distance
                FROM client_face_embeddings cfe
                JOIN students s ON s.id = cfe.student_id
                WHERE cfe.model_name = :model_name
                  AND cfe.model_version = :model_version
                ORDER BY cfe.embedding <-> CAST(:emb AS vector)
                LIMIT :candidate_limit
                """
            ),
            {
                "emb": to_vector_literal(embedding),
                "model_name": model_name,
                "model_version": model_version,
                "candidate_limit": candidate_limit,
            },
        )
        frame_best = {}
        for row in result.mappings().all():
            distance = float(row["distance"])
            nearest_distance = distance if nearest_distance is None else min(nearest_distance, distance)
            existing = frame_best.get(row["student_id"])
            if existing is None or distance < existing["distance"]:
                frame_best[row["student_id"]] = {**dict(row), "distance": distance}

        for row in frame_best.values():
            student_id = row["student_id"]
            distance = row["distance"]
            candidate = candidates.setdefault(
                student_id,
                {
                    "student_id": student_id,
                    "full_name": row["full_name"],
                    "person_type": row["person_type"],
                    "dms_person_kind": row["dms_person_kind"],
                    "dms_person_id": row["dms_person_id"],
                    "student_code": row["student_code"],
                    "distances": [],
                    "hit_distances": [],
                },
            )
            candidate["distances"].append(distance)
            if distance <= CLIENT_FACE_MATCH_THRESHOLD:
                candidate["hit_distances"].append(distance)

    eligible = []
    for candidate in candidates.values():
        hits = len(candidate["hit_distances"])
        if hits < required_hits:
            continue
        score = median_distance(candidate["hit_distances"])
        eligible.append(
            {
                **candidate,
                "distance": score,
                "hits": hits,
            }
        )

    eligible.sort(key=lambda item: (item["distance"], -item["hits"]))
    row = eligible[0] if eligible else None

    if row and len(eligible) > 1:
        second = eligible[1]
        if second["distance"] - row["distance"] < CLIENT_FACE_MATCH_MARGIN:
            row = None

    if not row:
        return {
            "matched": False,
            "reason": "unknown_face",
            "distance": nearest_distance,
            "confidence": 0 if nearest_distance is None else client_confidence(nearest_distance),
        }

    distance = float(row["distance"])
    return await finalize_attendance_match(
        db,
        operator,
        row,
        distance=distance,
        confidence=client_confidence(distance),
        engine_name="client",
    )


@app.get("/students")
async def list_students(db: AsyncSession = Depends(get_tenant_db), _admin: dict = Depends(require_admin)):
    res = await db.execute(
        text(
            """
            SELECT s.id, s.student_code, s.full_name, s.person_type,
                   s.dms_person_kind, s.dms_person_id, s.created_at,
                   COUNT(DISTINCT fe.id) AS server_sample_count,
                   COUNT(DISTINCT cfe.id) AS client_sample_count,
                   COUNT(DISTINCT fe.id) + COUNT(DISTINCT cfe.id) AS sample_count
            FROM students s
            LEFT JOIN face_embeddings fe ON fe.student_id = s.id
            LEFT JOIN client_face_embeddings cfe ON cfe.student_id = s.id
            GROUP BY s.id, s.student_code, s.full_name, s.person_type,
                     s.dms_person_kind, s.dms_person_id, s.created_at
            ORDER BY s.id DESC
            """
        )
    )
    rows = res.mappings().all()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("dms_person_id") is not None:
            d["dms_person_id"] = str(d["dms_person_id"])
        items.append(d)
    return {"items": items}


@app.get("/admin/summary")
async def admin_summary(db: AsyncSession = Depends(get_tenant_db), admin: dict = Depends(require_admin)):
    people = (await db.execute(
        text(
            """
            SELECT
              COUNT(*) AS total_people,
              COUNT(*) FILTER (WHERE person_type = 'student') AS students,
              COUNT(*) FILTER (WHERE person_type = 'staff') AS staff,
              COUNT(*) FILTER (WHERE person_type = 'teacher') AS teachers,
              COUNT(*) FILTER (WHERE dms_person_id IS NOT NULL) AS dms_linked
            FROM students
            """
        )
    )).mappings().first()

    today = (await db.execute(
        text(
            """
            SELECT COUNT(DISTINCT student_id) AS today_present
            FROM attendance_logs
            WHERE (marked_at AT TIME ZONE :tz)::date = (now() AT TIME ZONE :tz)::date
            """
        ),
        {"tz": APP_TIMEZONE},
    )).mappings().first()

    samples = (await db.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM face_embeddings) AS server_samples,
              (SELECT COUNT(*) FROM client_face_embeddings) AS client_samples,
              (SELECT COUNT(*) FROM face_embeddings) + (SELECT COUNT(*) FROM client_face_embeddings) AS total_samples
            """
        )
    )).mappings().first()

    async with SessionLocal() as central:
        outbox = (await central.execute(
            text(
                """
                SELECT
                  COUNT(*) FILTER (WHERE delivered_at IS NULL) AS pending,
                  COUNT(*) FILTER (WHERE delivered_at IS NOT NULL) AS delivered,
                  COUNT(*) FILTER (WHERE last_error IS NOT NULL AND delivered_at IS NULL) AS failing
                FROM dms_outbox
                WHERE organization_id = :org_id
                """
            ),
            {"org_id": admin["organization_id"]},
        )).mappings().first()

    return {
        **dict(people),
        **dict(today),
        **dict(samples),
        "dms_pending": int(outbox["pending"] or 0),
        "dms_delivered": int(outbox["delivered"] or 0),
        "dms_failing": int(outbox["failing"] or 0),
    }


@app.delete("/students/{student_id}")
async def delete_student(student_id: int, db: AsyncSession = Depends(get_tenant_db), _admin: dict = Depends(require_admin)):
    deleted = await db.execute(
        text(
            """
            DELETE FROM students
            WHERE id = :student_id
            RETURNING id, student_code, full_name
            """
        ),
        {"student_id": student_id},
    )
    row = deleted.first()
    if not row:
        raise HTTPException(status_code=404, detail="Student/staff/teacher not found")
    await db.commit()
    return {"deleted": True, "id": row.id, "student_code": row.student_code, "full_name": row.full_name}


@app.get("/attendance/report")
async def attendance_report(db: AsyncSession = Depends(get_tenant_db), _admin: dict = Depends(require_admin)):
    res = await db.execute(
        text(
            """
            SELECT a.id, s.student_code, s.full_name, s.person_type,
                   s.dms_person_kind, s.dms_person_id,
                   a.status, a.confidence, a.marked_at
            FROM attendance_logs a
            JOIN students s ON s.id = a.student_id
            ORDER BY a.marked_at DESC
            LIMIT 500
            """
        )
    )
    rows = res.mappings().all()
    items = []
    for r in rows:
        d = dict(r)
        if d.get("dms_person_id") is not None:
            d["dms_person_id"] = str(d["dms_person_id"])
        items.append(d)
    return {"items": items}


@app.delete("/attendance/{attendance_id}")
async def delete_attendance(attendance_id: int, db: AsyncSession = Depends(get_tenant_db), _admin: dict = Depends(require_admin)):
    deleted = await db.execute(
        text(
            """
            DELETE FROM attendance_logs
            WHERE id = :attendance_id
            RETURNING id
            """
        ),
        {"attendance_id": attendance_id},
    )
    row = deleted.first()
    if not row:
        raise HTTPException(status_code=404, detail="Attendance entry not found")
    await db.commit()
    return {"deleted": True, "id": row.id}


@app.delete("/attendance")
async def clear_attendance(db: AsyncSession = Depends(get_tenant_db), _admin: dict = Depends(require_admin)):
    result = await db.execute(text("DELETE FROM attendance_logs"))
    await db.commit()
    return {"deleted": True, "count": result.rowcount}


# ---------- DMS link admin endpoints ----------


@app.get("/dms/status")
async def dms_status(admin: dict = Depends(require_admin)):
    base_url = admin.get("dms_base_url")
    secret = admin.get("dms_webhook_secret")
    if not base_url or not secret:
        return {"linked": False}
    try:
        result = await dms_health_check(base_url, secret)
        return {"linked": True, "base_url": base_url, "remote": result}
    except httpx.HTTPError as exc:
        return {"linked": True, "base_url": base_url, "error": str(exc)}


@app.post("/dms/configure")
async def dms_configure(
    base_url: str = Form(...),
    webhook_secret: str = Form(...),
    admin: dict = Depends(require_admin),
):
    base_url = base_url.strip().rstrip("/")
    webhook_secret = webhook_secret.strip()
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="DMS base URL must start with http:// or https://")
    if len(webhook_secret) < 16:
        raise HTTPException(status_code=400, detail="Webhook secret must be at least 16 characters")
    try:
        await dms_health_check(base_url, webhook_secret)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"DMS unreachable or signature rejected: {exc}")

    async with SessionLocal() as db:
        await db.execute(
            text(
                """
                UPDATE organizations
                SET dms_base_url = :base, dms_webhook_secret = :secret
                WHERE id = :id
                """
            ),
            {"base": base_url, "secret": webhook_secret, "id": admin["organization_id"]},
        )
        await db.commit()
    return {"linked": True, "base_url": base_url}


@app.post("/dms/disconnect")
async def dms_disconnect(admin: dict = Depends(require_admin)):
    async with SessionLocal() as db:
        await db.execute(
            text(
                "UPDATE organizations SET dms_base_url = NULL, dms_webhook_secret = NULL WHERE id = :id"
            ),
            {"id": admin["organization_id"]},
        )
        await db.commit()
    return {"linked": False}


@app.get("/dms/roster")
async def dms_roster(admin: dict = Depends(require_admin)):
    base_url = admin.get("dms_base_url")
    secret = admin.get("dms_webhook_secret")
    if not base_url or not secret:
        raise HTTPException(status_code=409, detail="DMS is not linked for this organization")
    try:
        return await fetch_roster(base_url, secret)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"DMS roster fetch failed: {exc}")


@app.get("/dms/outbox")
async def dms_outbox(admin: dict = Depends(require_admin)):
    async with SessionLocal() as db:
        rows = (await db.execute(
            text(
                """
                SELECT id, endpoint, attempt_count, next_attempt_at, last_error,
                       delivered_at, created_at, payload_json
                FROM dms_outbox
                WHERE organization_id = :id
                ORDER BY id DESC
                LIMIT 100
                """
            ),
            {"id": admin["organization_id"]},
        )).mappings().all()
    return {"items": [dict(r) for r in rows]}
