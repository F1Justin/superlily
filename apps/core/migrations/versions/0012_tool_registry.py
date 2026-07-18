"""Add the Phase 3a descriptor and provider registry without execution state."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0012_tool_registry"
down_revision: str | None = "0011_claim_ack"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tool_descriptors",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("schema_profile", sa.String(length=64), nullable=False),
        sa.Column("source_plugin", sa.String(length=512), nullable=False),
        sa.Column("review_status", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("source_commit", sa.String(length=64), nullable=False),
        sa.Column("bundle_hash", sa.String(length=64), nullable=False),
        sa.Column("reviewer", sa.String(length=256), nullable=False),
        sa.Column("canonical_json", sa.LargeBinary(), nullable=False),
        sa.Column("descriptor_json", sa.JSON(), nullable=False),
        sa.Column("import_outcome", sa.String(length=32), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("review_status IN ('reviewed')", name="ck_tool_descriptor_review_status"),
        sa.CheckConstraint(
            "lifecycle IN ('draft', 'reviewed', 'active', 'suspended', 'retired', 'revoked')",
            name="ck_tool_descriptor_lifecycle",
        ),
        sa.CheckConstraint("import_outcome IN ('accepted')", name="ck_tool_descriptor_import_outcome"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("descriptor_hash", name="uq_tool_descriptor_hash"),
        sa.UniqueConstraint("tool_id", "version", name="uq_tool_descriptor_identity"),
    )
    op.create_index("ix_tool_descriptors_tool_id", "tool_descriptors", ["tool_id"])
    op.create_index("ix_tool_descriptors_lifecycle", "tool_descriptors", ["lifecycle"])
    op.create_table(
        "tool_descriptor_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("descriptor_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_lifecycle", sa.String(length=32), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle IN ('draft', 'reviewed', 'active', 'suspended', 'retired', 'revoked')",
            name="ck_tool_descriptor_event_lifecycle",
        ),
        sa.ForeignKeyConstraint(["descriptor_id"], ["tool_descriptors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("descriptor_id", "sequence", name="uq_tool_descriptor_lifecycle_sequence"),
    )
    op.create_index(
        "ix_tool_descriptor_lifecycle_created",
        "tool_descriptor_lifecycle_events",
        ["descriptor_id", "created_at"],
    )

    op.create_table(
        "tool_providers",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("allowed_protocols_json", sa.JSON(), nullable=False),
        sa.Column("tool_selectors_json", sa.JSON(), nullable=False),
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
            "lifecycle IN ('registered', 'active', 'quarantined', 'retired', 'revoked')",
            name="ck_tool_provider_lifecycle",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_tool_providers_lifecycle", "tool_providers", ["lifecycle"])
    op.create_table(
        "tool_provider_credentials",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("source IN ('environment')", name="ck_tool_provider_credential_source"),
        sa.CheckConstraint(
            "lifecycle IN ('active', 'revoked')", name="ck_tool_provider_credential_lifecycle"
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_tool_provider_credentials_provider", "tool_provider_credentials", ["provider_id"]
    )
    op.create_table(
        "tool_provider_lifecycle_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("previous_lifecycle", sa.String(length=32), nullable=True),
        sa.Column("lifecycle", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=256), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "lifecycle IN ('registered', 'active', 'quarantined', 'retired', 'revoked')",
            name="ck_tool_provider_event_lifecycle",
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_id", "sequence", name="uq_tool_provider_lifecycle_sequence"),
    )
    op.create_index(
        "ix_tool_provider_lifecycle_created",
        "tool_provider_lifecycle_events",
        ["provider_id", "created_at"],
    )
    op.create_table(
        "tool_provider_inventory_snapshots",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("protocol_version", sa.String(length=64), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("id", name="uq_tool_inventory_snapshot_id"),
        sa.UniqueConstraint("provider_id", "idempotency_key", name="uq_tool_inventory_idempotency"),
    )
    op.create_index(
        "ix_tool_inventory_provider_received",
        "tool_provider_inventory_snapshots",
        ["provider_id", "received_at"],
    )
    op.create_index(
        "ix_tool_inventory_provider_hash",
        "tool_provider_inventory_snapshots",
        ["provider_id", "snapshot_hash"],
    )
    op.create_table(
        "tool_provider_inventory_entries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("tool_id", sa.String(length=128), nullable=False),
        sa.Column("descriptor_version", sa.String(length=64), nullable=False),
        sa.Column("descriptor_hash", sa.String(length=64), nullable=False),
        sa.Column("protocol_version", sa.String(length=64), nullable=False),
        sa.Column("implementation_hash", sa.String(length=64), nullable=False),
        sa.Column("budget_enforcement_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"], ["tool_provider_inventory_snapshots.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("snapshot_id", "tool_id", name="uq_tool_inventory_entry_tool"),
    )
    op.create_index(
        "ix_tool_inventory_entries_tool", "tool_provider_inventory_entries", ["tool_id"]
    )
    op.create_table(
        "tool_provider_heartbeats",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("inventory_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("health", sa.String(length=32), nullable=False),
        sa.Column("current_concurrency", sa.Integer(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("oldest_work_age_ms", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "health IN ('starting', 'healthy', 'degraded', 'unavailable', 'unknown')",
            name="ck_tool_provider_heartbeat_health",
        ),
        sa.CheckConstraint(
            "current_concurrency >= 0 AND current_concurrency <= max_concurrency",
            name="ck_tool_provider_heartbeat_capacity",
        ),
        sa.ForeignKeyConstraint(["provider_id"], ["tool_providers.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("id", name="uq_tool_provider_heartbeat_id"),
        sa.UniqueConstraint("provider_id", "observed_at", name="uq_tool_provider_heartbeat_observed"),
    )
    op.create_index(
        "ix_tool_provider_heartbeat_received",
        "tool_provider_heartbeats",
        ["provider_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_tool_provider_heartbeat_received", table_name="tool_provider_heartbeats")
    op.drop_table("tool_provider_heartbeats")
    op.drop_index("ix_tool_inventory_entries_tool", table_name="tool_provider_inventory_entries")
    op.drop_table("tool_provider_inventory_entries")
    op.drop_index("ix_tool_inventory_provider_hash", table_name="tool_provider_inventory_snapshots")
    op.drop_index("ix_tool_inventory_provider_received", table_name="tool_provider_inventory_snapshots")
    op.drop_table("tool_provider_inventory_snapshots")
    op.drop_index("ix_tool_provider_lifecycle_created", table_name="tool_provider_lifecycle_events")
    op.drop_table("tool_provider_lifecycle_events")
    op.drop_index("ix_tool_provider_credentials_provider", table_name="tool_provider_credentials")
    op.drop_table("tool_provider_credentials")
    op.drop_index("ix_tool_providers_lifecycle", table_name="tool_providers")
    op.drop_table("tool_providers")
    op.drop_index("ix_tool_descriptor_lifecycle_created", table_name="tool_descriptor_lifecycle_events")
    op.drop_table("tool_descriptor_lifecycle_events")
    op.drop_index("ix_tool_descriptors_lifecycle", table_name="tool_descriptors")
    op.drop_index("ix_tool_descriptors_tool_id", table_name="tool_descriptors")
    op.drop_table("tool_descriptors")
