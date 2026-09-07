"""Queryable, append-only history recovery progress."""

from alembic import op
import sqlalchemy as sa

revision = "0032_qq_history_recovery"
down_revision = "0031_platform_api_calls"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "qq_history_recovery_progress",
        sa.Column("observation_id", sa.String(36), sa.ForeignKey("event_observations.id"), primary_key=True),
        sa.Column("instance_id", sa.String(128), nullable=False),
        sa.Column("conversation_type", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(256), nullable=False),
        sa.Column("job_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("captured", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(256)),
        sa.Column("cursor", sa.String(128)),
        sa.UniqueConstraint("instance_id", "job_id", "revision", name="uq_history_recovery_revision"),
    )
    op.create_index("ix_history_recovery_conversation", "qq_history_recovery_progress",
                    ["instance_id", "conversation_type", "conversation_id", "window_end"])


def downgrade():
    op.drop_table("qq_history_recovery_progress")
