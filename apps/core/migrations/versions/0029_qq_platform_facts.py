"""Store event-time QQ sender title and level facts."""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0029_qq_platform_facts"
down_revision: str | None = "0028_sqlite_chatrecorder_archive"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "event_observations",
        sa.Column("sender_title", sa.String(512)),
    )
    op.add_column(
        "event_observations",
        sa.Column("sender_level", sa.String(512)),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        op.drop_column("event_observations", "sender_level")
        op.drop_column("event_observations", "sender_title")
        return

    # SQLite validates every stored view during DROP COLUMN. Temporarily remove
    # and restore their own definitions so unrelated timeline views do not make
    # an otherwise supported column drop fail.
    views = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'view' AND sql IS NOT NULL ORDER BY name"
        )
    ).all()
    for name, _ in views:
        quoted_name = str(name).replace('"', '""')
        op.execute(f'DROP VIEW "{quoted_name}"')
    try:
        op.drop_column("event_observations", "sender_level")
        op.drop_column("event_observations", "sender_title")
    finally:
        for _, definition in views:
            op.execute(str(definition))
