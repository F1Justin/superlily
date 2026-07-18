"""Add the authority-neutral C0-D collection reliability foundation."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0013_collection_reliability"
down_revision: str | None = "0012_tool_registry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_capture_profiles",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=64), nullable=False),
        sa.Column("conversation_type", sa.String(length=32), nullable=False),
        sa.Column("conversation_id", sa.String(length=256), nullable=False),
        sa.Column("capture_profile", sa.String(length=32), nullable=False),
        sa.Column(
            "image_policy",
            sa.String(length=32),
            server_default="metadata_only",
            nullable=False,
        ),
        sa.Column(
            "binary_policy",
            sa.String(length=32),
            server_default="metadata_only",
            nullable=False,
        ),
        sa.Column("retention_class", sa.String(length=64), nullable=False),
        sa.Column("policy_version", sa.String(length=64), nullable=False),
        sa.Column("source_commit", sa.String(length=64), nullable=True),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
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
        sa.CheckConstraint(
            "capture_profile IN ('off', 'operational', 'archive_full')",
            name="ck_conversation_capture_profile",
        ),
        sa.CheckConstraint(
            "image_policy IN ('metadata_only')",
            name="ck_conversation_capture_image_policy",
        ),
        sa.CheckConstraint(
            "binary_policy IN ('metadata_only', 'object_store')",
            name="ck_conversation_capture_binary_policy",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform",
            "conversation_type",
            "conversation_id",
            name="uq_conversation_capture_profile_scope",
        ),
    )
    op.create_index(
        "ix_conversation_capture_profiles_active",
        "conversation_capture_profiles",
        ["active"],
    )

    with op.batch_alter_table("event_observations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "capture_profile",
                sa.String(length=32),
                server_default="operational",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "capture_policy_version",
                sa.String(length=64),
                server_default="default-operational-v1",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "capture_status",
                sa.String(length=32),
                server_default="unassessed",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "sanitizer_version",
                sa.String(length=64),
                server_default="superlily.sanitizer.v1",
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("collector_sanitizer_version", sa.String(length=64))
        )
        batch_op.add_column(sa.Column("original_payload_sha256", sa.String(length=64)))
        batch_op.add_column(sa.Column("original_payload_size_bytes", sa.BigInteger()))
        batch_op.add_column(
            sa.Column(
                "omitted_fields_json",
                sa.JSON(),
                server_default=sa.text("'[]'"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "platform_extra_json",
                sa.JSON(),
                server_default=sa.text("'{}'"),
                nullable=False,
            )
        )
        batch_op.add_column(sa.Column("capture_reason", sa.Text()))
        batch_op.create_check_constraint(
            "ck_event_observation_capture_profile",
            "capture_profile IN ('off', 'operational', 'archive_full')",
        )
        batch_op.create_check_constraint(
            "ck_event_observation_capture_status",
            "capture_status IN ('unassessed', 'complete', 'partial', 'unavailable')",
        )
        batch_op.create_check_constraint(
            "ck_event_observation_payload_size",
            "original_payload_size_bytes IS NULL OR original_payload_size_bytes >= 0",
        )

    op.create_table(
        "platform_action_observations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("observer_instance_id", sa.String(length=128), nullable=False),
        sa.Column("action_index", sa.Integer(), nullable=False),
        sa.Column("action_kind", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("actor_principal_id", sa.String(length=256)),
        sa.Column("subject_principal_id", sa.String(length=256)),
        sa.Column("target_reported_source_event_id", sa.String(length=512)),
        sa.Column("target_platform_message_id", sa.String(length=512)),
        sa.Column("target_conversation_id", sa.String(length=256), nullable=False),
        sa.Column("target_conversation_type", sa.String(length=32), nullable=False),
        sa.Column("target_source_event_id", sa.String(length=512)),
        sa.Column(
            "resolver_status",
            sa.String(length=32),
            server_default="unresolved",
            nullable=False,
        ),
        sa.Column("value_json", sa.JSON(), nullable=False),
        sa.Column("capture_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "operation IN ('add', 'remove', 'update', 'observed_state', 'unknown')",
            name="ck_platform_action_operation",
        ),
        sa.CheckConstraint(
            "capture_status IN ('unassessed', 'complete', 'partial', 'unavailable')",
            name="ck_platform_action_capture_status",
        ),
        sa.CheckConstraint(
            "resolver_status IN ('resolved', 'unresolved', 'ambiguous', 'unavailable')",
            name="ck_platform_action_resolver_status",
        ),
        sa.CheckConstraint(
            "target_reported_source_event_id IS NOT NULL "
            "OR target_platform_message_id IS NOT NULL "
            "OR subject_principal_id IS NOT NULL",
            name="ck_platform_action_target_hint",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["event_observations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["observer_instance_id"], ["bot_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["target_source_event_id"], ["source_events.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "observation_id", "action_index", name="uq_platform_action_observation_index"
        ),
    )
    op.create_index(
        "ix_platform_actions_observation",
        "platform_action_observations",
        ["observation_id"],
    )
    op.create_index(
        "ix_platform_actions_target_source",
        "platform_action_observations",
        ["target_source_event_id"],
    )
    op.create_index(
        "ix_platform_actions_pending_target",
        "platform_action_observations",
        [
            "observer_instance_id",
            "target_conversation_type",
            "target_conversation_id",
            "target_platform_message_id",
        ],
    )

    op.create_table(
        "ingress_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("observation_id", sa.String(length=36), nullable=False),
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("spool_id", sa.String(length=128)),
        sa.Column("collector_sequence", sa.BigInteger()),
        sa.Column("record_sha256", sa.String(length=64)),
        sa.Column("captured_at", sa.DateTime(timezone=True)),
        sa.Column(
            "committed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(spool_id IS NULL AND collector_sequence IS NULL AND record_sha256 IS NULL "
            "AND captured_at IS NULL) OR (spool_id IS NOT NULL AND collector_sequence IS NOT NULL "
            "AND record_sha256 IS NOT NULL AND captured_at IS NOT NULL)",
            name="ck_ingress_receipt_spool_binding",
        ),
        sa.CheckConstraint(
            "collector_sequence IS NULL OR collector_sequence >= 1",
            name="ck_ingress_receipt_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"], ["event_observations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id", name="uq_ingress_receipt_observation"),
        sa.UniqueConstraint(
            "instance_id",
            "spool_id",
            "collector_sequence",
            name="uq_ingress_receipt_spool_sequence",
        ),
    )
    op.create_index(
        "ix_ingress_receipts_instance_committed",
        "ingress_receipts",
        ["instance_id", "committed_at"],
    )

    op.create_table(
        "collector_watermarks",
        sa.Column("instance_id", sa.String(length=128), nullable=False),
        sa.Column("spool_id", sa.String(length=128), nullable=False),
        sa.Column(
            "highest_contiguous_sequence", sa.BigInteger(), server_default="0", nullable=False
        ),
        sa.Column("highest_seen_sequence", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("last_receipt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "highest_contiguous_sequence >= 0 "
            "AND highest_seen_sequence >= highest_contiguous_sequence",
            name="ck_collector_watermark_order",
        ),
        sa.ForeignKeyConstraint(["instance_id"], ["bot_instances.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("instance_id", "spool_id"),
    )
    op.create_index(
        "ix_collector_watermarks_updated", "collector_watermarks", ["updated_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_collector_watermarks_updated", table_name="collector_watermarks")
    op.drop_table("collector_watermarks")
    op.drop_index("ix_ingress_receipts_instance_committed", table_name="ingress_receipts")
    op.drop_table("ingress_receipts")
    op.drop_index(
        "ix_platform_actions_pending_target", table_name="platform_action_observations"
    )
    op.drop_index(
        "ix_platform_actions_target_source", table_name="platform_action_observations"
    )
    op.drop_index("ix_platform_actions_observation", table_name="platform_action_observations")
    op.drop_table("platform_action_observations")

    with op.batch_alter_table("event_observations") as batch_op:
        batch_op.drop_constraint("ck_event_observation_payload_size", type_="check")
        batch_op.drop_constraint("ck_event_observation_capture_status", type_="check")
        batch_op.drop_constraint("ck_event_observation_capture_profile", type_="check")
        batch_op.drop_column("capture_reason")
        batch_op.drop_column("platform_extra_json")
        batch_op.drop_column("omitted_fields_json")
        batch_op.drop_column("original_payload_size_bytes")
        batch_op.drop_column("original_payload_sha256")
        batch_op.drop_column("collector_sanitizer_version")
        batch_op.drop_column("sanitizer_version")
        batch_op.drop_column("capture_status")
        batch_op.drop_column("capture_policy_version")
        batch_op.drop_column("capture_profile")

    op.drop_index(
        "ix_conversation_capture_profiles_active", table_name="conversation_capture_profiles"
    )
    op.drop_table("conversation_capture_profiles")
