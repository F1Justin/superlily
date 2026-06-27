"""Add first-class event reference links."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0004_event_links"
down_revision: str | None = "0003_normalize_sources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_links",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "from_source_event_id",
            sa.String(length=512),
            sa.ForeignKey("source_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "from_observation_id",
            sa.String(length=36),
            sa.ForeignKey("event_observations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "to_source_event_id",
            sa.String(length=512),
            sa.ForeignKey("source_events.id", ondelete="SET NULL"),
        ),
        sa.Column("relation_type", sa.String(length=64), nullable=False),
        sa.Column("target_source_event_id", sa.String(length=512)),
        sa.Column("target_platform_message_id", sa.String(length=512)),
        sa.Column("target_conversation_id", sa.String(length=256)),
        sa.Column("target_conversation_type", sa.String(length=32)),
        sa.Column("target_sender_id", sa.String(length=256)),
        sa.Column("confidence", sa.Integer()),
        sa.Column("resolver_status", sa.String(length=32), nullable=False),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_links_from_source", "event_links", ["from_source_event_id"])
    op.create_index("ix_event_links_from_observation", "event_links", ["from_observation_id"])
    op.create_index("ix_event_links_to_source", "event_links", ["to_source_event_id"])
    op.create_index("ix_event_links_resolver_status", "event_links", ["resolver_status"])


def downgrade() -> None:
    op.drop_index("ix_event_links_resolver_status", table_name="event_links")
    op.drop_index("ix_event_links_to_source", table_name="event_links")
    op.drop_index("ix_event_links_from_observation", table_name="event_links")
    op.drop_index("ix_event_links_from_source", table_name="event_links")
    op.drop_table("event_links")
