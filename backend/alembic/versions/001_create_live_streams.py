"""create live_streams table

Revision ID: 001
Revises:
Create Date: 2026-04-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "live_streams",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("stream_key", sa.String(255), nullable=False, index=True),
        sa.Column("educator_id", UUID(as_uuid=False), sa.ForeignKey("educators.id", ondelete="SET NULL"), nullable=True, index=True),
        sa.Column("title", sa.String(500), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="waiting", index=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Float, nullable=True),
        sa.Column("viewer_peak", sa.Integer, nullable=False, server_default="0"),
        sa.Column("hls_manifest_url", sa.String(1024), nullable=True),
        sa.Column("dvr_local_path", sa.String(1024), nullable=True),
        sa.Column("do_mp4_path", sa.String(1024), nullable=True),
        sa.Column("do_hls_path", sa.String(1024), nullable=True),
        sa.Column("srs_client_id", sa.String(100), nullable=True),
        sa.Column("publisher_ip", sa.String(45), nullable=True),
        sa.Column("timeout_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade():
    op.drop_table("live_streams")
