"""Normalize source fields created during correlation rollout."""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_normalize_sources"
down_revision: str | None = "0002_event_correlation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE source_events
        SET event_type = 'message',
            message_id = NULL
        WHERE correlation_version = 'qq-text-v1'
          AND (event_type <> 'message' OR message_id IS NOT NULL)
        """
    )


def downgrade() -> None:
    # Account-local values live on event_observations and cannot be restored
    # unambiguously to a canonical source row.
    pass
