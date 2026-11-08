"""Immutable simulator measurements, separate from user-editable CRUD records."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UTCDateTime, utc_now
from app.schemas.device import LEGACY_DEVICE_ID, LEGACY_DEVICE_NAME


class RealtimeSample(Base):
    __tablename__ = "realtime_samples"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_realtime_samples_event_id"),
        Index("ix_realtime_samples_timestamp_id", "timestamp", "id"),
        Index("ix_realtime_samples_category_timestamp_id", "category", "timestamp", "id"),
        Index("ix_realtime_samples_device_timestamp_id", "device_id", "timestamp", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    device_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default=LEGACY_DEVICE_ID, server_default=LEGACY_DEVICE_ID
    )
    device_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default=LEGACY_DEVICE_NAME, server_default=LEGACY_DEVICE_NAME
    )
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="", server_default="")
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    threshold: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
