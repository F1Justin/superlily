"""Add provenance-backed identity and conversation name history."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0027_name_observation_history"
down_revision: str | None = "0026_history_timeline_export"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "identity_name_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(256), nullable=False),
        sa.Column("conversation_type", sa.String(32)),
        sa.Column("conversation_id", sa.String(256)),
        sa.Column("name_kind", sa.String(32), nullable=False),
        sa.Column("name_value", sa.String(512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instance_id", sa.String(128)),
        sa.Column("source_system", sa.String(128), nullable=False),
        sa.Column("source_record_type", sa.String(64), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=False),
        sa.Column("observation_method", sa.String(64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "name_kind IN ('account_name', 'conversation_display_name', 'effective_display_name')",
            name="ck_identity_name_observation_kind",
        ),
        sa.CheckConstraint(
            "conversation_type IS NULL OR conversation_type IN ('group', 'private', 'channel', 'system', 'unknown')",
            name="ck_identity_name_observation_conversation_type",
        ),
        sa.CheckConstraint(
            "(conversation_type IS NULL) = (conversation_id IS NULL)",
            name="ck_identity_name_observation_conversation_scope",
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_record_type",
            "source_record_id",
            "name_kind",
            name="uq_identity_name_observation_source_kind",
        ),
    )
    op.create_index(
        "ix_identity_name_observations_user_time",
        "identity_name_observations",
        ["platform", "user_id", "observed_at", "id"],
    )
    op.create_index(
        "ix_identity_name_observations_user_conversation_time",
        "identity_name_observations",
        [
            "platform",
            "user_id",
            "conversation_type",
            "conversation_id",
            "observed_at",
            "id",
        ],
    )
    op.create_index(
        "ix_identity_name_observations_latest",
        "identity_name_observations",
        [
            "platform",
            "user_id",
            "name_kind",
            "instance_id",
            "conversation_type",
            "conversation_id",
            "observed_at",
        ],
    )

    op.create_table(
        "conversation_name_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("platform", sa.String(64), nullable=False),
        sa.Column("conversation_type", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(256), nullable=False),
        sa.Column("name_value", sa.String(512), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("instance_id", sa.String(128)),
        sa.Column("source_system", sa.String(128), nullable=False),
        sa.Column("source_record_type", sa.String(64), nullable=False),
        sa.Column("source_record_id", sa.String(512), nullable=False),
        sa.Column("observation_method", sa.String(64), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "conversation_type IN ('group', 'private', 'channel', 'system', 'unknown')",
            name="ck_conversation_name_observation_type",
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_record_type",
            "source_record_id",
            name="uq_conversation_name_observation_source",
        ),
    )
    op.create_index(
        "ix_conversation_name_observations_conversation_time",
        "conversation_name_observations",
        ["platform", "conversation_type", "conversation_id", "observed_at", "id"],
    )
    op.create_index(
        "ix_conversation_name_observations_latest",
        "conversation_name_observations",
        [
            "platform",
            "conversation_type",
            "conversation_id",
            "instance_id",
            "observed_at",
        ],
    )

    op.create_table(
        "name_observation_backfill_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_system", sa.String(128), nullable=False),
        sa.Column("source_scope", sa.String(64), nullable=False),
        sa.Column("source_snapshot_id", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cursor_json", sa.JSON(), nullable=False),
        sa.Column("selected_count", sa.BigInteger(), nullable=False),
        sa.Column("written_count", sa.BigInteger(), nullable=False),
        sa.Column("existing_count", sa.BigInteger(), nullable=False),
        sa.Column("error_summary", sa.Text()),
        sa.Column(
            "started_at",
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
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_name_observation_backfill_batch_status",
        ),
        sa.CheckConstraint(
            "selected_count >= 0 AND written_count >= 0 AND existing_count >= 0",
            name="ck_name_observation_backfill_batch_counts",
        ),
        sa.UniqueConstraint(
            "source_system",
            "source_scope",
            "source_snapshot_id",
            name="uq_name_observation_backfill_batch_source",
        ),
    )
    op.create_index(
        "ix_name_observation_backfill_batches_status",
        "name_observation_backfill_batches",
        ["status", "updated_at"],
    )

    schema = "archive." if _postgresql() else "archive_"
    op.execute(
        f"""
        CREATE VIEW {schema}identity_name_timeline_v1 AS
        SELECT id, platform, user_id, conversation_type, conversation_id,
               name_kind, name_value, observed_at, instance_id, source_system,
               source_record_type, source_record_id, observation_method,
               provenance_json, recorded_at
        FROM identity_name_observations
        """
    )
    op.execute(
        f"""
        CREATE VIEW {schema}conversation_name_timeline_v1 AS
        SELECT id, platform, conversation_type, conversation_id, name_value,
               observed_at, instance_id, source_system, source_record_type,
               source_record_id, observation_method, provenance_json, recorded_at
        FROM conversation_name_observations
        """
    )


def downgrade() -> None:
    schema = "archive." if _postgresql() else "archive_"
    op.execute(f"DROP VIEW IF EXISTS {schema}conversation_name_timeline_v1")
    op.execute(f"DROP VIEW IF EXISTS {schema}identity_name_timeline_v1")
    op.drop_table("name_observation_backfill_batches")
    op.drop_table("conversation_name_observations")
    op.drop_table("identity_name_observations")
