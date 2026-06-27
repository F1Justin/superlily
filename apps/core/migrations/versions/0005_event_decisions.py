"""Add shadow event decisions."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0005_event_decisions"
down_revision: str | None = "0004_event_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_decisions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "source_event_id",
            sa.String(length=512),
            sa.ForeignKey("source_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "deciding_observation_id",
            sa.String(length=36),
            sa.ForeignKey("event_observations.id", ondelete="SET NULL"),
        ),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("decision_type", sa.String(length=64), nullable=False),
        sa.Column("target_instance_id", sa.String(length=128)),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source_event_id", name="uq_event_decisions_source_event"),
    )
    op.create_index("ix_event_decisions_created_at", "event_decisions", ["created_at"])
    op.create_index("ix_event_decisions_decision_type", "event_decisions", ["decision_type"])
    op.create_index("ix_event_decisions_target_instance", "event_decisions", ["target_instance_id"])


def downgrade() -> None:
    op.drop_index("ix_event_decisions_target_instance", table_name="event_decisions")
    op.drop_index("ix_event_decisions_decision_type", table_name="event_decisions")
    op.drop_index("ix_event_decisions_created_at", table_name="event_decisions")
    op.drop_table("event_decisions")
