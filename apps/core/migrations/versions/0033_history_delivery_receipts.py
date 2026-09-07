"""Additional delivery receipts without multiplying canonical archive rows."""

from alembic import op
import sqlalchemy as sa

revision = "0033_history_delivery_receipts"
down_revision = "0032_qq_history_recovery"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "history_delivery_receipts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("observation_id", sa.String(36), sa.ForeignKey("event_observations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instance_id", sa.String(128), sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("delivery_source_event_id", sa.String(512), nullable=False),
        sa.Column("spool_id", sa.String(128)),
        sa.Column("collector_sequence", sa.BigInteger()),
        sa.Column("record_sha256", sa.String(64)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column("committed_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.UniqueConstraint("observation_id", "delivery_source_event_id", name="uq_history_receipt_delivery"),
        sa.UniqueConstraint("instance_id", "spool_id", "collector_sequence", name="uq_history_receipt_sequence"),
        sa.CheckConstraint(
            "(spool_id IS NULL AND collector_sequence IS NULL AND record_sha256 IS NULL AND captured_at IS NULL) "
            "OR (spool_id IS NOT NULL AND collector_sequence IS NOT NULL AND record_sha256 IS NOT NULL AND captured_at IS NOT NULL)",
            name="ck_history_receipt_binding",
        ),
        sa.CheckConstraint("collector_sequence IS NULL OR collector_sequence >= 1", name="ck_history_receipt_sequence"),
    )


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM history_delivery_receipts LIMIT 1")).first():
        raise RuntimeError("Cannot discard history delivery receipts; preserve receipt evidence")
    op.drop_table("history_delivery_receipts")
