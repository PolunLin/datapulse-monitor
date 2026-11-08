"""Add immutable realtime samples and a unique event identifier for retry deduplication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision = "0002_realtime_samples"
down_revision = "0001_users_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "realtime_samples",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("value", sa.Numeric(18, 6), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("timestamp", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("threshold", sa.Numeric(18, 6), nullable=False),
        sa.Column("is_anomaly", sa.Boolean(), nullable=False),
        sa.Column("created_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_realtime_samples_event_id"),
    )
    op.create_index("ix_realtime_samples_timestamp_id", "realtime_samples", ["timestamp", "id"])
    op.create_index(
        "ix_realtime_samples_category_timestamp_id",
        "realtime_samples",
        ["category", "timestamp", "id"],
    )


def downgrade() -> None:
    op.drop_table("realtime_samples")
