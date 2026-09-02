"""Accept frozen SQLite chatrecorder sources and add their month partitions."""

from collections.abc import Sequence

from alembic import op

revision: str = "0028_sqlite_chatrecorder_archive"
down_revision: str | None = "0027_name_observation_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SOURCE_CONSTRAINTS = (
    ("import_batches", "ck_archive_import_batch_source"),
    ("conversation_mappings", "ck_archive_conversation_mapping_source"),
    ("legacy_messages", "ck_archive_legacy_message_source"),
    ("source_message_identities", "ck_archive_source_message_source"),
)
_LEGACY_SOURCES = (
    "lily.nonebot.chatrecorder.v2",
    "nekro.chat_message",
)
_ACCEPTED_SOURCES = _LEGACY_SOURCES + (
    "lily.nonebot.chatrecorder.sqlite.data1",
    "lily.nonebot.chatrecorder.sqlite.data2",
    "lily.nonebot.chatrecorder.sqlite.data3",
)


def _month_partitions() -> tuple[tuple[str, str, str], ...]:
    partitions: list[tuple[str, str, str]] = []
    year = 2022
    month = 12
    while (year, month) <= (2023, 11):
        next_year = year + 1 if month == 12 else year
        next_month = 1 if month == 12 else month + 1
        partitions.append(
            (
                f"legacy_messages_{year:04d}_{month:02d}",
                f"{year:04d}-{month:02d}-01 00:00:00+00",
                f"{next_year:04d}-{next_month:02d}-01 00:00:00+00",
            )
        )
        year, month = next_year, next_month
    return tuple(partitions)


_MONTH_PARTITIONS = _month_partitions()


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    accepted_sources = ", ".join(f"'{source}'" for source in _ACCEPTED_SOURCES)
    for table, constraint in _SOURCE_CONSTRAINTS:
        op.drop_constraint(constraint, table, schema="archive", type_="check")
        op.create_check_constraint(
            constraint,
            table,
            f"source_system IN ({accepted_sources})",
            schema="archive",
        )
    for name, start, end in _MONTH_PARTITIONS:
        op.execute(
            f"""
            CREATE TABLE archive.{name}
            PARTITION OF archive.legacy_messages
            FOR VALUES FROM ('{start}') TO ('{end}')
            """
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    legacy_sources = ", ".join(f"'{source}'" for source in _LEGACY_SOURCES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM archive.import_batches
                WHERE source_system NOT IN ({legacy_sources})
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0028 while SQLite archive batches exist';
            END IF;
            IF EXISTS (
                SELECT 1 FROM archive.legacy_messages
                WHERE occurred_at < TIMESTAMPTZ '2024-08-01 00:00:00+00'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade 0028 while pre-2024 archive rows exist';
            END IF;
        END
        $$
        """
    )
    for name, _, _ in reversed(_MONTH_PARTITIONS):
        op.execute(
            f"ALTER TABLE archive.legacy_messages "
            f"DETACH PARTITION archive.{name}"
        )
        op.execute(f"DROP TABLE archive.{name}")
    for table, constraint in _SOURCE_CONSTRAINTS:
        op.drop_constraint(constraint, table, schema="archive", type_="check")
        op.create_check_constraint(
            constraint,
            table,
            f"source_system IN ({legacy_sources})",
            schema="archive",
        )
