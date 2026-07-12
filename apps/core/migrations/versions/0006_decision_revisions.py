"""Track canonical decision recomputation."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0006_decision_revisions"
down_revision: str | None = "0005_event_decisions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_decisions",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "event_decisions",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute("UPDATE event_decisions SET updated_at = created_at WHERE updated_at IS NULL")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("event_decisions") as batch_op:
            batch_op.alter_column(
                "updated_at",
                existing_type=sa.DateTime(timezone=True),
                nullable=False,
            )
            batch_op.alter_column(
                "revision",
                existing_type=sa.Integer(),
                existing_nullable=False,
                server_default=None,
            )
    else:
        op.alter_column("event_decisions", "updated_at", nullable=False)
        op.alter_column("event_decisions", "revision", server_default=None)
    op.create_index("ix_event_decisions_updated_at", "event_decisions", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_event_decisions_updated_at", table_name="event_decisions")
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("event_decisions") as batch_op:
            batch_op.drop_column("updated_at")
            batch_op.drop_column("revision")
    else:
        op.drop_column("event_decisions", "updated_at")
        op.drop_column("event_decisions", "revision")
