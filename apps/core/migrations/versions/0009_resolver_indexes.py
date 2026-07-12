"""Add lookup indexes for link resolution and response attribution."""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_resolver_indexes"
down_revision: str | None = "0008_event_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_event_observations_instance_message",
        "event_observations",
        ["instance_id", "platform_message_id"],
    )
    op.create_index(
        "ix_event_links_pending_target",
        "event_links",
        [
            "resolver_status",
            "target_platform_message_id",
            "target_conversation_id",
            "target_conversation_type",
        ],
    )
    op.create_index(
        "ix_responses_trigger_source",
        "responses",
        ["trigger_source_event_id"],
    )
    op.create_index(
        "ix_responses_platform_message",
        "responses",
        ["platform", "conversation_type", "platform_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_responses_platform_message", table_name="responses")
    op.drop_index("ix_responses_trigger_source", table_name="responses")
    op.drop_index("ix_event_links_pending_target", table_name="event_links")
    op.drop_index("ix_event_observations_instance_message", table_name="event_observations")
