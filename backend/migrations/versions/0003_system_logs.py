"""Persist application events and transactional administrator audit history."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0003_system_logs"
down_revision = "0002_realtime_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "system_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(36), nullable=True),
        sa.Column("timestamp", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("logger", sa.String(80), nullable=False),
        sa.Column("message", sa.String(300), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("method", sa.String(12), nullable=True),
        sa.Column("path", sa.String(160), nullable=True),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Float(), nullable=True),
        sa.Column("error_type", sa.String(80), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("target_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(80), nullable=True),
        sa.Column("changes", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_system_logs_event_id"),
    )
    op.create_index("ix_system_logs_timestamp_id", "system_logs", ["timestamp", "id"])
    op.create_index("ix_system_logs_kind_timestamp_id", "system_logs", ["kind", "timestamp", "id"])
    op.create_index("ix_system_logs_request_id", "system_logs", ["request_id"])
    op.create_index("ix_system_logs_actor_id", "system_logs", ["actor_id"])


def downgrade() -> None:
    op.drop_table("system_logs")
