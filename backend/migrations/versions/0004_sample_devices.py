"""Identify equipment without guessing which device produced legacy measurements."""

import sqlalchemy as sa
from alembic import op

revision = "0004_sample_devices"
down_revision = "0003_system_logs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "realtime_samples",
        sa.Column("device_id", sa.String(64), nullable=False, server_default="legacy"),
    )
    op.add_column(
        "realtime_samples",
        sa.Column("device_name", sa.String(100), nullable=False, server_default="舊版未指定設備"),
    )
    op.add_column(
        "realtime_samples", sa.Column("unit", sa.String(16), nullable=False, server_default="")
    )
    op.create_index(
        "ix_realtime_samples_device_timestamp_id",
        "realtime_samples",
        ["device_id", "timestamp", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_realtime_samples_device_timestamp_id", table_name="realtime_samples")
    op.drop_column("realtime_samples", "unit")
    op.drop_column("realtime_samples", "device_name")
    op.drop_column("realtime_samples", "device_id")
