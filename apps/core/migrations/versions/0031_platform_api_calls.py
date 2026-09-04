"""Materialize durable non-send OneBot side-effect API calls."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0031_platform_api_calls"
down_revision: str | None = "0030_qq_directory_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_api_calls",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(128),
            sa.ForeignKey("bot_instances.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(128), nullable=False),
        sa.Column("api_name", sa.String(128), nullable=False),
        sa.Column("target_conversation_id", sa.String(256), nullable=False),
        sa.Column("target_conversation_type", sa.String(32), nullable=False),
        sa.Column("trigger_reported_source_event_id", sa.String(512)),
        sa.Column("safe_parameters_json", sa.JSON(), nullable=False),
        sa.Column(
            "started_observation_id",
            sa.String(36),
            sa.ForeignKey("event_observations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "completed_observation_id",
            sa.String(36),
            sa.ForeignKey("event_observations.id", ondelete="SET NULL"),
        ),
        sa.Column("start_observed", sa.Boolean(), nullable=False),
        sa.Column("result_observed", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("success", sa.Boolean()),
        sa.Column("return_code", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("result_message_ids_json", sa.JSON(), nullable=False),
        sa.Column("safe_error_code", sa.String(128)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("instance_id", "call_id", name="uq_platform_api_call_identity"),
        sa.CheckConstraint(
            "target_conversation_type IN ('group', 'private', 'channel', 'system', 'unknown')",
            name="ck_platform_api_call_conversation_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('pending', 'succeeded', 'failed', 'ambiguous')",
            name="ck_platform_api_call_outcome",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms BETWEEN 0 AND 86400000",
            name="ck_platform_api_call_duration",
        ),
        sa.CheckConstraint(
            "(start_observed AND started_at IS NOT NULL AND started_observation_id IS NOT NULL) OR "
            "(NOT start_observed AND started_at IS NULL AND started_observation_id IS NULL)",
            name="ck_platform_api_call_start_state",
        ),
        sa.CheckConstraint(
            "(NOT result_observed AND outcome = 'pending' AND success IS NULL AND completed_at IS NULL "
            "AND completed_observation_id IS NULL) OR "
            "(result_observed AND outcome <> 'pending' AND success IS NOT NULL AND completed_at IS NOT NULL "
            "AND completed_observation_id IS NOT NULL)",
            name="ck_platform_api_call_terminal_state",
        ),
    )
    op.create_index(
        "ix_platform_api_calls_instance_started",
        "platform_api_calls",
        ["instance_id", "started_at", "id"],
    )
    op.create_index(
        "ix_platform_api_calls_conversation_started",
        "platform_api_calls",
        ["target_conversation_id", "started_at", "id"],
    )
    op.create_index(
        "ix_platform_api_calls_api_started",
        "platform_api_calls",
        ["api_name", "started_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_platform_api_calls_api_started", table_name="platform_api_calls")
    op.drop_index("ix_platform_api_calls_conversation_started", table_name="platform_api_calls")
    op.drop_index("ix_platform_api_calls_instance_started", table_name="platform_api_calls")
    op.drop_table("platform_api_calls")
