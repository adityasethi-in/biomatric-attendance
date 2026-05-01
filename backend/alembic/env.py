from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, event, pool


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("DATABASE_URL")
if not database_url:
    raise RuntimeError("DATABASE_URL is required for Alembic migrations")
config.set_main_option("sqlalchemy.url", database_url.replace("+asyncpg", ""))

SCHEMA = os.getenv("BIOMATRIC_DB_SCHEMA", "biomatric").strip() or "biomatric"

target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=SCHEMA,
        include_schemas=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    @event.listens_for(connectable, "connect")
    def _bootstrap_schema(dbapi_conn, _conn_rec):
        prev_autocommit = dbapi_conn.autocommit
        dbapi_conn.autocommit = True
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{SCHEMA}"')
                cur.execute(f'SET search_path TO "{SCHEMA}", public')
        finally:
            dbapi_conn.autocommit = prev_autocommit

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            version_table_schema=SCHEMA,
            include_schemas=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
