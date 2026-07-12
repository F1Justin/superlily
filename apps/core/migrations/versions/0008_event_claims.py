"""Add fail-open event claim audit records."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0008_event_claims"
down_revision: str | None = "0007_runtime_command_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_claims",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "source_event_id",
            sa.String(length=512),
            sa.ForeignKey("source_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "instance_id",
            sa.String(length=128),
            sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column(
            "decision_id",
            sa.String(length=36),
            sa.ForeignKey("event_decisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decision_revision", sa.Integer(), nullable=True),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("ready", sa.Boolean(), nullable=False),
        sa.Column("enforced", sa.Boolean(), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "idempotency_key", name="uq_event_claim_idempotency"),
        sa.UniqueConstraint("source_event_id", "instance_id", name="uq_event_claim_source_instance"),
    )
    op.create_index("ix_event_claims_source_event", "event_claims", ["source_event_id"])
    op.create_index("ix_event_claims_created_at", "event_claims", ["created_at"])
    op.create_index("ix_event_claims_action", "event_claims", ["action"])


def downgrade() -> None:
    op.drop_index("ix_event_claims_action", table_name="event_claims")
    op.drop_index("ix_event_claims_created_at", table_name="event_claims")
    op.drop_index("ix_event_claims_source_event", table_name="event_claims")
    op.drop_table("event_claims")
