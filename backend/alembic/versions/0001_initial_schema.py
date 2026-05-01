"""Initial BIOMATRIC central and default-tenant schema.

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-04-30
"""

from __future__ import annotations

from alembic import op


revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS students (
          id SERIAL PRIMARY KEY,
          student_code VARCHAR(64) UNIQUE NOT NULL,
          full_name VARCHAR(128) NOT NULL,
          person_type VARCHAR(16) NOT NULL DEFAULT 'student',
          dms_person_kind VARCHAR(16),
          dms_person_id UUID,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS person_type VARCHAR(16) NOT NULL DEFAULT 'student'")
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_kind VARCHAR(16)")
    op.execute("ALTER TABLE students ADD COLUMN IF NOT EXISTS dms_person_id UUID")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS face_embeddings (
          id SERIAL PRIMARY KEY,
          student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
          embedding vector(512) NOT NULL,
          quality_score INT DEFAULT 100,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS attendance_logs (
          id SERIAL PRIMARY KEY,
          student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
          status VARCHAR(16) DEFAULT 'present',
          confidence INT DEFAULT 0,
          marked_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
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
        )
        """
    )
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS dms_base_url VARCHAR(255)")
    op.execute("ALTER TABLE organizations ADD COLUMN IF NOT EXISTS dms_webhook_secret VARCHAR(255)")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS organization_admins (
          id SERIAL PRIMARY KEY,
          organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          username VARCHAR(80) NOT NULL,
          password_hash VARCHAR(255) NOT NULL,
          full_name VARCHAR(128),
          created_at TIMESTAMPTZ DEFAULT now(),
          UNIQUE (organization_id, username)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
          id SERIAL PRIMARY KEY,
          organization_id INT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
          amount NUMERIC(12,2) NOT NULL,
          status VARCHAR(24) NOT NULL DEFAULT 'paid',
          reference VARCHAR(128),
          notes TEXT,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
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
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_students_code ON students(student_code)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_students_dms_person ON students(dms_person_kind, dms_person_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_attendance_marked_at ON attendance_logs(marked_at)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_dms_outbox_pending ON dms_outbox(delivered_at, next_attempt_at)")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_face_embeddings_ivfflat
        ON face_embeddings USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS dms_outbox")
    op.execute("DROP TABLE IF EXISTS payments")
    op.execute("DROP TABLE IF EXISTS organization_admins")
    op.execute("DROP TABLE IF EXISTS organizations")
    op.execute("DROP TABLE IF EXISTS attendance_logs")
    op.execute("DROP TABLE IF EXISTS face_embeddings")
    op.execute("DROP TABLE IF EXISTS students")
