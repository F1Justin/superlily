"""Store authenticated runtime command-registry snapshots."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0007_runtime_command_registry"
down_revision: str | None = "0006_decision_revisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "command_registry_snapshots",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "instance_id",
            sa.String(length=128),
            sa.ForeignKey("bot_instances.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plugins_json", sa.JSON(), nullable=False),
        sa.Column("candidates_json", sa.JSON(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("instance_id", "snapshot_hash", name="uq_command_registry_snapshot_hash"),
    )
    op.create_index(
        "ix_command_registry_snapshots_instance_received",
        "command_registry_snapshots",
        ["instance_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_command_registry_snapshots_instance_received", table_name="command_registry_snapshots")
    op.drop_table("command_registry_snapshots")
