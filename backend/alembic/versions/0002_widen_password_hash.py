"""Widen admin password hash storage.

Revision ID: 0002_widen_password_hash
Revises: 0001_initial_schema
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op


revision = "0002_widen_password_hash"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE organization_admins ALTER COLUMN password_hash TYPE VARCHAR(255)")


def downgrade() -> None:
    op.execute("ALTER TABLE organization_admins ALTER COLUMN password_hash TYPE VARCHAR(128)")
