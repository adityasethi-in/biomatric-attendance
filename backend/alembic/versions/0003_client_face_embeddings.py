"""Add browser face samples table.

Revision ID: 0003_client_face_embeddings
Revises: 0002_widen_password_hash
Create Date: 2026-05-02
"""

from __future__ import annotations

from alembic import op


revision = "0003_client_face_embeddings"
down_revision = "0002_widen_password_hash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS client_face_embeddings (
          id SERIAL PRIMARY KEY,
          student_id INT NOT NULL REFERENCES students(id) ON DELETE CASCADE,
          model_name VARCHAR(80) NOT NULL DEFAULT 'face-api-128',
          model_version VARCHAR(80) NOT NULL DEFAULT 'vladmandic-face-api-1.7.15',
          embedding vector(128) NOT NULL,
          quality_score INT DEFAULT 100,
          created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_client_face_embeddings_model
        ON client_face_embeddings(model_name, model_version)
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_client_face_embeddings_ivfflat
        ON client_face_embeddings USING ivfflat (embedding vector_l2_ops) WITH (lists = 100)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS client_face_embeddings")
