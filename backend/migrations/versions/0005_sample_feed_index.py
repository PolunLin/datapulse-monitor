"""Index device-scoped insertion cursors without changing existing samples."""

from alembic import op

revision = "0005_sample_feed_index"
down_revision = "0004_sample_devices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_realtime_samples_device_id", "realtime_samples", ["device_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_realtime_samples_device_id", table_name="realtime_samples")
