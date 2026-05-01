"""Database engine + session factory.

BIOMATRIC shares the DMS Postgres database (`delight_school`) and isolates
its own tables inside the `biomatric` schema. Multi-tenant paid orgs get
their own schemas under the same DB (`biomatric_tenant_<slug>`).

Every async connection sets `search_path` so plain SQL (no schema-qualified
identifiers) automatically reads/writes from the right schema.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase


DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required. Set it via environment or docker compose .env.")

DEFAULT_SCHEMA = os.getenv("BIOMATRIC_DB_SCHEMA", "biomatric").strip() or "biomatric"
SCHEMA_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def _validate_schema(value: str) -> str:
    if not SCHEMA_RE.fullmatch(value):
        raise ValueError(f"Unsafe schema name: {value!r}")
    return value


_validate_schema(DEFAULT_SCHEMA)


def quote_identifier(value: str) -> str:
    """Quote an identifier safely for raw SQL injection-free use."""
    value = _validate_schema(value)
    return f'"{value}"'


def safe_schema_name(value: str) -> str:
    """Normalize a tenant slug to a valid Postgres schema name."""
    name = re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_") or "tenant"
    if name != DEFAULT_SCHEMA and not name.startswith(f"{DEFAULT_SCHEMA}_tenant_"):
        name = f"{DEFAULT_SCHEMA}_tenant_{name}"
    return _validate_schema(name[:63].rstrip("_"))


# Backwards-compat alias for callers that still reference the old name.
safe_database_name = safe_schema_name


def _make_engine(schema: str):
    schema = _validate_schema(schema)
    eng = create_async_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        # Inject `SET search_path` on every fresh DB-API connection so the
        # subsequent SELECT/INSERT statements (which have no schema prefix)
        # land in the right namespace. asyncpg supports `server_settings`
        # natively, so this is a single round-trip during connect.
        connect_args={"server_settings": {"search_path": f"{schema},public"}},
    )

    @event.listens_for(eng.sync_engine, "connect")
    def _set_search_path(dbapi_conn, _conn_rec):
        # Belt-and-braces for environments where server_settings was
        # ignored (e.g. an older proxy). asyncpg cursor lives behind the
        # sync wrapper, so we use a plain SQL exec.
        try:
            cur = dbapi_conn.cursor()
            cur.execute(f'SET search_path TO {schema}, public')
            cur.close()
        except Exception:
            pass

    return eng


engine = _make_engine(DEFAULT_SCHEMA)
SessionLocal = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
_tenant_sessionmakers: dict[str, async_sessionmaker[AsyncSession]] = {DEFAULT_SCHEMA: SessionLocal}


class Base(DeclarativeBase):
    pass


def get_sessionmaker_for_schema(schema: str) -> async_sessionmaker[AsyncSession]:
    schema = _validate_schema(schema)
    if schema not in _tenant_sessionmakers:
        tenant_engine = _make_engine(schema)
        _tenant_sessionmakers[schema] = async_sessionmaker(
            bind=tenant_engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _tenant_sessionmakers[schema]


# Backwards-compat alias for `main.py` callers that still use database-name
# semantics. In the schema model, "database name" *is* the schema.
get_sessionmaker_for_database = get_sessionmaker_for_schema


async def ensure_schema_exists(schema: str) -> None:
    """CREATE SCHEMA IF NOT EXISTS — DMS bootstrap also does this for the
    default schema, but paid-org registration creates new ones at runtime."""
    schema = _validate_schema(schema)
    async with engine.connect() as conn:
        await conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}"))
        await conn.commit()


def admin_database_url() -> str:
    """Compatibility shim — the schema model never CREATEs a database, so
    this is unused by the new code paths. Kept as a no-op for old imports."""
    return DATABASE_URL
