"""Enforce one active allow owner per source event."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0010_claim_owner_index"
down_revision: str | None = "0009_resolver_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_event_claim_enforced_allow_owner",
        "event_claims",
        ["source_event_id"],
        unique=True,
        postgresql_where=sa.text("enforced AND action = 'allow'"),
        sqlite_where=sa.text("enforced = 1 AND action = 'allow'"),
    )


def downgrade() -> None:
    op.drop_index("uq_event_claim_enforced_allow_owner", table_name="event_claims")
