import asyncio
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import math

import httpx
from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
from .security import admin_token, admin_token_secret, hash_password, verify_password


LOGGER = logging.getLogger("biomatric")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())


def _csv_env(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


THRESH = float(os.getenv("FACE_MATCH_THRESHOLD", "0.60"))
DUPLICATE_THRESH = float(os.getenv("FACE_DUPLICATE_THRESHOLD", os.getenv("FACE_MATCH_THRESHOLD", "0.60")))
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
LIVENESS_MODE = os.getenv("LIVENESS_MODE", "basic").lower()
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Kolkata")
VALID_PERSON_TYPES = {"student", "staff", "teacher"}
DEFAULT_ORG_NAME = os.getenv("DEFAULT_FREE_ORG_NAME", "Delight Model School")
DEFAULT_ORG_SLUG = os.getenv("DEFAULT_FREE_ORG_SLUG", "delight-model-school")
DEFAULT_ADMIN_USERNAME = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
DEFAULT_ADMIN_PASSWORD = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
PRICE_PER_USER_PER_DAY = Decimal(os.getenv("PRICE_PER_USER_PER_DAY", "3"))
DEFAULT_BILLING_DAYS = int(os.getenv("DEFAULT_BILLING_DAYS", "30"))
ALLOWED_ORIGINS = _csv_env("ALLOWED_ORIGINS") or ["http://localhost:7200"]
DEV_MODE = os.getenv("BIOMATRIC_DEV_MODE", "").lower() in {"1", "true", "yes"}

DMS_DEFAULT_BASE_URL = os.getenv("DMS_BASE_URL", "").strip() or None
DMS_DEFAULT_SECRET = os.getenv("DMS_WEBHOOK_SECRET", "").strip() or None


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
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS organization_admins (
  id SERIAL PRIMARY KEY,
  organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  username VARCHAR(80) NOT NULL,
  password_hash VARCHAR(255) NOT NULL,
  full_name VARCHAR(128),
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (organization_id, username)
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
"""


def rate_limit_key(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    return get_remote_address(request)


limiter = Limiter(key_func=rate_limit_key, default_limits=["120/minute"])
_face_engine = None


def get_face_engine():
    """Start the scanner engine only for the image upload flow."""
    if FACE_ENGINE_MODE in {"client", "off", "disabled", "false", "0"}:
        raise HTTPException(
            status_code=503,
            detail="Face scanner is not ready. Please refresh and try again.",
        )

    global _face_engine
    if _face_engine is None:
        from .face_engine import FaceEngine

        _face_engine = FaceEngine()
    return _face_engine


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
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_kind VARCHAR(16)",
            "ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_id UUID",
            "CREATE INDEX IF NOT EXISTS idx_students_dms_person ON students(dms_person_kind, dms_person_id)",
            "ALTER TABLE organization_admins ALTER COLUMN password_hash TYPE VARCHAR(255)",
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
            await db.execute(
                text(
                    """
                    INSERT INTO organization_admins (organization_id, username, password_hash, full_name)
                    VALUES (:organization_id, :username, :password_hash, 'Default Admin')
                    ON CONFLICT (organization_id, username) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        full_name = EXCLUDED.full_name
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
    try:
        yield
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            worker_task.cancel()


from slowapi import _rate_limit_exceeded_handler

app = FastAPI(title="Face Recognition Attendance System", version="1.1.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


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
                   dms_base_url, dms_webhook_secret
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
            SELECT oa.id, oa.username, oa.password_hash,
                   o.id AS organization_id, o.slug, o.status,
                   o.dms_base_url, o.dms_webhook_secret
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
        if not row or row["status"] != "active":
            raise HTTPException(status_code=401, detail="Invalid admin login")
        expected = admin_token(row["slug"], row["username"], row["password_hash"])
        import hmac as _hmac
        if not _hmac.compare_digest(expected, x_admin_token):
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
        if not row or row["status"] != "active":
            raise HTTPException(status_code=401, detail="Invalid attendance login")
        expected = admin_token(row["slug"], row["username"], row["password_hash"])
        import hmac as _hmac
        if not _hmac.compare_digest(expected, token):
            raise HTTPException(status_code=401, detail="Invalid attendance login")
        return dict(row)


@app.get("/health")
async def health():
    return {"ok": True, "version": app.version}


@app.get("/")
async def root():
    return {"service": "Face Recognition Attendance API", "health": "/health"}


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
                INSERT INTO organization_admins (organization_id, username, password_hash, full_name)
                VALUES (:organization_id, :username, :password_hash, :full_name)
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


async def embedding_from_upload(upload: UploadFile):
    engine = get_face_engine()
    img_bytes = await upload.read()
    img = engine.decode_image(img_bytes)
    if img is None:
        return None, None, "Invalid image"

    emb, det_score = engine.get_embedding(img)
    if emb is None:
        return None, None, "No face detected"

    return emb, det_score, None


async def embeddings_from_uploads(images: list[UploadFile], min_count: int = 1, max_count: int = 10):
    if len(images) < min_count:
        raise HTTPException(status_code=400, detail=f"Capture at least {min_count} face samples")

    embeddings = []
    for index, upload in enumerate(images[:max_count], start=1):
        emb, det_score, error = await embedding_from_upload(upload)
        if error:
            raise HTTPException(status_code=422, detail=f"Sample {index}: {error}")
        embeddings.append((emb, det_score))

    return embeddings


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

    embeddings = await embeddings_from_uploads(images, min_count=5)
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

    emb, det_score, error = await embedding_from_upload(image)
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
    embeddings = await embeddings_from_uploads(images, min_count=1)
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
    engine = get_face_engine()
    img_bytes = await image.read()
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
