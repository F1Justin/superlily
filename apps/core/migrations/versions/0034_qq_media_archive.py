"""QQ forward nodes and independently retrievable media content evidence."""
from alembic import op
import sqlalchemy as sa

revision = "0034_qq_media_archive"
down_revision = "0033_history_delivery_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("qq_media_blobs",
        sa.Column("instance_id", sa.String(128), primary_key=True),
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("qq_media_archive_items",
        sa.Column("observation_id", sa.String(36), sa.ForeignKey("event_observations.id"), primary_key=True),
        sa.Column("path", sa.String(128), primary_key=True),
        *[sa.Column(name, sa.String(size), nullable=False) for name, size in (
            ("instance_id",128), ("conversation_type",32), ("conversation_id",256),
            ("parent_source_event_id",512), ("parent_message_id",512), ("job_id",64), ("kind",32), ("state",32))],
        sa.Column("revision", sa.Integer(), nullable=False),
        *[sa.Column(name, sa.String(size)) for name, size in (
            ("platform_id",512), ("name",512), ("media_type",256), ("content_ref",256),
            ("sender_id",256), ("sender_name",512), ("sha256",64), ("reason",128))],
        sa.Column("occurred_at", sa.DateTime(timezone=True)),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("segments_json", sa.JSON(), nullable=False),
        sa.Column("omitted_fields_json", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.UniqueConstraint("instance_id", "job_id", "revision", "path", name="uq_qq_media_revision_path"))
    op.create_index("ix_qq_media_parent", "qq_media_archive_items",
                    ["instance_id", "conversation_type", "conversation_id", "parent_message_id"])


def downgrade():
    op.drop_table("qq_media_archive_items")
    op.drop_table("qq_media_blobs")
