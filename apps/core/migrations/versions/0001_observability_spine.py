"""Create the first observability-spine schema."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0001_observability_spine"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bot_instances",
        sa.Column("id", sa.String(length=128), primary_key=True),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("bot_id", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=256)),
        sa.Column("version", sa.String(length=128)),
        sa.Column("reported_status", sa.String(length=32), nullable=False, server_default="unknown"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("last_event_at", sa.DateTime(timezone=True)),
        sa.Column("last_response_at", sa.DateTime(timezone=True)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_table(
        "source_events",
        sa.Column("id", sa.String(length=512), primary_key=True),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=256), nullable=False),
        sa.Column("conversation_type", sa.String(length=32), nullable=False),
        sa.Column("message_id", sa.String(length=512)),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_received_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_events_occurred_at", "source_events", ["occurred_at"])
    op.create_table(
        "event_observations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_event_id", sa.String(length=512), sa.ForeignKey("source_events.id", ondelete="CASCADE"), nullable=False),
        sa.Column("instance_id", sa.String(length=128), sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("bot_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_name", sa.String(length=512)),
        sa.Column("sender_id", sa.String(length=256)),
        sa.Column("sender_name", sa.String(length=512)),
        sa.Column("sender_roles_json", sa.JSON(), nullable=False),
        sa.Column("text", sa.Text()),
        sa.Column("segments_json", sa.JSON(), nullable=False),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.Column("raw_json", sa.JSON()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "idempotency_key", name="uq_event_observation_idempotency"),
    )
    op.create_index("ix_event_observations_received_at", "event_observations", ["received_at"])
    op.create_index("ix_event_observations_source_event_id", "event_observations", ["source_event_id"])
    op.create_table(
        "responses",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source_response_id", sa.String(length=512), nullable=False),
        sa.Column("instance_id", sa.String(length=128), sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("trigger_observation_id", sa.String(length=36), sa.ForeignKey("event_observations.id", ondelete="SET NULL")),
        sa.Column("trigger_source_event_id", sa.String(length=512)),
        sa.Column("trace_id", sa.String(length=128)),
        sa.Column("response_type", sa.String(length=128), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("bot_id", sa.String(length=128), nullable=False),
        sa.Column("conversation_id", sa.String(length=256), nullable=False),
        sa.Column("conversation_type", sa.String(length=32), nullable=False),
        sa.Column("platform_message_id", sa.String(length=512)),
        sa.Column("reply_to_platform_message_id", sa.String(length=512)),
        sa.Column("text", sa.Text()),
        sa.Column("segments_json", sa.JSON(), nullable=False),
        sa.Column("attachments_json", sa.JSON(), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("raw_json", sa.JSON()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "idempotency_key", name="uq_response_idempotency"),
    )
    op.create_index("ix_responses_received_at", "responses", ["received_at"])
    op.create_table(
        "instance_status_transitions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("instance_id", sa.String(length=128), sa.ForeignKey("bot_instances.id", ondelete="CASCADE"), nullable=False),
        sa.Column("previous_status", sa.String(length=32)),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("detail_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_status_transitions_instance_created",
        "instance_status_transitions",
        ["instance_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_status_transitions_instance_created", table_name="instance_status_transitions")
    op.drop_table("instance_status_transitions")
    op.drop_index("ix_responses_received_at", table_name="responses")
    op.drop_table("responses")
    op.drop_index("ix_event_observations_source_event_id", table_name="event_observations")
    op.drop_index("ix_event_observations_received_at", table_name="event_observations")
    op.drop_table("event_observations")
    op.drop_index("ix_source_events_occurred_at", table_name="source_events")
    op.drop_table("source_events")
    op.drop_table("bot_instances")
