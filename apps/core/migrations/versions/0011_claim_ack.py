"""Require an acknowledged suppression before granting an exclusive owner."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0011_claim_ack"
down_revision: str | None = "0010_claim_owner_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_claims",
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("event_claims", "acknowledged_at")
