import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey,
    Integer, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Existing tables (recordings project) — reflected here for FK relationships
# ---------------------------------------------------------------------------

class Educator(Base):
    __tablename__ = "educators"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    name: Mapped[str] = mapped_column(String(255), index=True)
    event_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    stream_key: Mapped[str | None] = mapped_column(String(255))
    stream_url: Mapped[str | None] = mapped_column(String(512))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    live_streams: Mapped[list["LiveStream"]] = relationship("LiveStream", back_populates="educator")


# ---------------------------------------------------------------------------
# New table — live stream sessions
# ---------------------------------------------------------------------------

class LiveStream(Base):
    __tablename__ = "live_streams"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=new_uuid)
    stream_key: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    educator_id: Mapped[str | None] = mapped_column(UUID(as_uuid=False), ForeignKey("educators.id", ondelete="SET NULL"), index=True)

    title: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    # waiting | live | ended | error

    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    viewer_peak: Mapped[int] = mapped_column(Integer, default=0)

    # HLS delivery URL (CDN)
    hls_manifest_url: Mapped[str | None] = mapped_column(String(1024))

    # DVR local path (inside container)
    dvr_local_path: Mapped[str | None] = mapped_column(String(1024))

    # Post-stream storage (DO Spaces)
    do_mp4_path: Mapped[str | None] = mapped_column(String(1024))
    do_hls_path: Mapped[str | None] = mapped_column(String(1024))

    # SRS client info
    srs_client_id: Mapped[str | None] = mapped_column(String(100))
    publisher_ip: Mapped[str | None] = mapped_column(String(45))

    # 120-min timeout tracking
    timeout_at: Mapped[datetime | None] = mapped_column(DateTime)

    error_message: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    educator: Mapped["Educator | None"] = relationship("Educator", back_populates="live_streams")
