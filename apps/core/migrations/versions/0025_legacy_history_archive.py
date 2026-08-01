"""Create the schema-only legacy-history archive read model.

The archive is deliberately separate from the public, online event tables.
This migration creates no connections to either legacy database and imports no
rows.  The source identity ledger is kept outside the partitioned message
table because PostgreSQL requires every UNIQUE/PRIMARY KEY on a partitioned
table to include its partition key.  Imported rows point back to the
partitioned parent through the message id and occurrence-time pair.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "0025_legacy_history_archive"
down_revision: str | None = "0024_agent_product_flow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_ARCHIVE_SCHEMA = "archive"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _archive_object(name: str, *, postgresql: bool) -> str:
    if postgresql:
        return f'"{_ARCHIVE_SCHEMA}"."{name}"'
    return f'"{name}"'


def _index_object(name: str) -> str:
    """Quote an index name without qualifying it as a schema object.

    PostgreSQL places an index in the table's schema, but its CREATE INDEX
    grammar does not accept a schema-qualified index name in this position.
    """

    return f'"{name}"'


def _public_object(name: str, *, postgresql: bool) -> str:
    if postgresql:
        return f'"public"."{name}"'
    return f'"{name}"'


def _month_partitions() -> tuple[tuple[str, str, str], ...]:
    """Return the known H0 history range as monthly partition bounds.

    The default partition below remains a safety net for a manifest that
    contains an out-of-range timestamp.  H2 should create/attach a specific
    month before loading any such data rather than relying on that fallback.
    """

    partitions: list[tuple[str, str, str]] = []
    year = 2024
    month = 8
    while (year, month) <= (2026, 7):
        next_year = year + 1 if month == 12 else year
        next_month = 1 if month == 12 else month + 1
        partition_name = f"legacy_messages_{year:04d}_{month:02d}"
        start = f"{year:04d}-{month:02d}-01 00:00:00+00"
        end = f"{next_year:04d}-{next_month:02d}-01 00:00:00+00"
        partitions.append((partition_name, start, end))
        year, month = next_year, next_month
    return tuple(partitions)


_MONTH_PARTITIONS = _month_partitions()
_EXPLICIT_INDEXES = (
    "ix_archive_import_batches_status",
    "ix_archive_conversation_mappings_lookup",
    "uq_archive_conversation_mappings_active",
    "ix_archive_legacy_messages_timeline",
    "ix_archive_legacy_messages_batch_occurred",
    "ix_archive_source_message_identities_batch_state",
    "ix_archive_source_message_identities_legacy_message",
)


def _create_schema(*, postgresql: bool) -> None:
    if postgresql:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{_ARCHIVE_SCHEMA}"')


def _create_import_batches(*, postgresql: bool) -> None:
    table = _archive_object("import_batches", postgresql=postgresql)
    json_type = "JSONB" if postgresql else "TEXT"
    empty_object = "'{}'::jsonb" if postgresql else "'{}'"
    op.execute(
        f"""
        CREATE TABLE {table} (
            "id" VARCHAR(36) NOT NULL,
            "source_system" VARCHAR(128) NOT NULL,
            "source_snapshot_id" VARCHAR(256) NOT NULL,
            "source_schema_version" VARCHAR(128) NOT NULL,
            "mapping_version" VARCHAR(128) NOT NULL,
            "cutover_boundary" {"TIMESTAMPTZ" if postgresql else "DATETIME"} NOT NULL,
            "status" VARCHAR(32) NOT NULL,
            "started_at" {"TIMESTAMPTZ" if postgresql else "DATETIME"},
            "finished_at" {"TIMESTAMPTZ" if postgresql else "DATETIME"},
            "source_row_count" BIGINT NOT NULL DEFAULT 0,
            "imported_count" BIGINT NOT NULL DEFAULT 0,
            "rejected_count" BIGINT NOT NULL DEFAULT 0,
            "duplicate_count" BIGINT NOT NULL DEFAULT 0,
            "manifest_hash" VARCHAR(64),
            "content_hash" VARCHAR(64),
            "checkpoint_json" {json_type} NOT NULL DEFAULT {empty_object},
            "error_summary_json" {json_type} NOT NULL DEFAULT {empty_object},
            "created_at" {"TIMESTAMPTZ" if postgresql else "DATETIME"} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" {"TIMESTAMPTZ" if postgresql else "DATETIME"} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "pk_archive_import_batches" PRIMARY KEY ("id"),
            CONSTRAINT "uq_archive_import_batch_snapshot"
                UNIQUE ("source_system", "source_snapshot_id"),
            CONSTRAINT "ck_archive_import_batch_source"
                CHECK ("source_system" IN ('lily.nonebot.chatrecorder.v2', 'nekro.chat_message')),
            CONSTRAINT "ck_archive_import_batch_status"
                CHECK ("status" IN ('planned', 'running', 'completed', 'failed', 'cancelled')),
            CONSTRAINT "ck_archive_import_batch_counts"
                CHECK (
                    "source_row_count" >= 0
                    AND "imported_count" >= 0
                    AND "rejected_count" >= 0
                    AND "duplicate_count" >= 0
                ),
            CONSTRAINT "ck_archive_import_batch_finished"
                CHECK (
                    "finished_at" IS NULL
                    OR "started_at" IS NULL
                    OR "finished_at" >= "started_at"
                ),
            CONSTRAINT "ck_archive_import_batch_terminal"
                CHECK (
                    ("status" IN ('completed', 'failed', 'cancelled') AND "finished_at" IS NOT NULL)
                    OR ("status" IN ('planned', 'running') AND "finished_at" IS NULL)
                )
        )
        """
    )


def _create_conversation_mappings(*, postgresql: bool) -> None:
    table = _archive_object("conversation_mappings", postgresql=postgresql)
    json_type = "JSONB" if postgresql else "TEXT"
    empty_object = "'{}'::jsonb" if postgresql else "'{}'"
    timestamp_type = "TIMESTAMPTZ" if postgresql else "DATETIME"
    op.execute(
        f"""
        CREATE TABLE {table} (
            "id" VARCHAR(36) NOT NULL,
            "source_system" VARCHAR(128) NOT NULL,
            "source_conversation_key" VARCHAR(512) NOT NULL,
            "source_conversation_type" VARCHAR(128),
            "platform" VARCHAR(64),
            "conversation_type" VARCHAR(32),
            "conversation_id" VARCHAR(256),
            "mapping_version" VARCHAR(64) NOT NULL,
            "mapping_status" VARCHAR(32) NOT NULL DEFAULT 'pending',
            "active" BOOLEAN NOT NULL DEFAULT TRUE,
            "mapping_reason" TEXT,
            "source_metadata_json" {json_type} NOT NULL DEFAULT {empty_object},
            "supersedes_id" VARCHAR(36),
            "created_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "pk_archive_conversation_mappings" PRIMARY KEY ("id"),
            CONSTRAINT "uq_archive_conversation_mapping_revision"
                UNIQUE ("source_system", "source_conversation_key", "mapping_version"),
            CONSTRAINT "ck_archive_conversation_mapping_source"
                CHECK ("source_system" IN ('lily.nonebot.chatrecorder.v2', 'nekro.chat_message')),
            CONSTRAINT "ck_archive_conversation_mapping_status"
                CHECK ("mapping_status" IN ('pending', 'mapped', 'rejected')),
            CONSTRAINT "ck_archive_conversation_mapping_key"
                CHECK (trim("source_conversation_key") <> ''),
            CONSTRAINT "ck_archive_conversation_mapping_target"
                CHECK (
                    "mapping_status" <> 'mapped'
                    OR (
                        "platform" IS NOT NULL
                        AND "conversation_type" IN ('group', 'private')
                        AND "conversation_id" IS NOT NULL
                    )
                )
        )
        """
    )


def _create_legacy_messages(*, postgresql: bool) -> None:
    table = _archive_object("legacy_messages", postgresql=postgresql)
    batches = _archive_object("import_batches", postgresql=postgresql)
    json_type = "JSONB" if postgresql else "TEXT"
    empty_object = "'{}'::jsonb" if postgresql else "'{}'"
    empty_array = "'[]'::jsonb" if postgresql else "'[]'"
    timestamp_type = "TIMESTAMPTZ" if postgresql else "DATETIME"
    source_identity_unique = (
        '("source_system", "source_table", "source_record_id", "occurred_at")'
        if postgresql
        else '("source_system", "source_table", "source_record_id")'
    )
    # Nekro's send_timestamp is the platform occurrence time; create_time is
    # retained separately as source_persisted_at and never substitutes for it.
    op.execute(
        f"""
        CREATE TABLE {table} (
            "id" VARCHAR(36) NOT NULL,
            "source_system" VARCHAR(128) NOT NULL,
            "source_table" VARCHAR(256) NOT NULL,
            "source_record_id" VARCHAR(512) NOT NULL,
            "import_batch_id" VARCHAR(36) NOT NULL,
            "mapping_version" VARCHAR(64) NOT NULL,
            "bot_id" VARCHAR(128),
            "source_conversation_key" VARCHAR(512) NOT NULL,
            "source_conversation_type" VARCHAR(128),
            "platform" VARCHAR(64),
            "conversation_type" VARCHAR(32),
            "conversation_id" VARCHAR(256),
            "sender_id" VARCHAR(256),
            "sender_name" VARCHAR(512),
            "direction" VARCHAR(16) NOT NULL,
            "occurred_at" {timestamp_type} NOT NULL,
            "source_persisted_at" {timestamp_type},
            "platform_message_id" VARCHAR(512),
            "content_text" TEXT,
            "segments_json" {json_type} NOT NULL DEFAULT {empty_array},
            "reply_hint_json" {json_type} NOT NULL DEFAULT {empty_object},
            "raw_fields_json" {json_type} NOT NULL DEFAULT {empty_object},
            "raw_storage_ref" VARCHAR(1024),
            "parse_warning" TEXT,
            "imported_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "pk_archive_legacy_messages" PRIMARY KEY ("id", "occurred_at"),
            CONSTRAINT "uq_archive_legacy_message_source_partition"
                UNIQUE {source_identity_unique},
            CONSTRAINT "fk_archive_legacy_message_batch"
                FOREIGN KEY ("import_batch_id")
                REFERENCES {batches} ("id")
                ON DELETE RESTRICT,
            CONSTRAINT "ck_archive_legacy_message_direction"
                CHECK ("direction" IN ('inbound', 'outbound', 'unknown')),
            CONSTRAINT "ck_archive_legacy_message_source"
                CHECK ("source_system" IN ('lily.nonebot.chatrecorder.v2', 'nekro.chat_message')),
            CONSTRAINT "ck_archive_legacy_message_conversation_type"
                CHECK ("conversation_type" IS NULL OR "conversation_type" IN ('group', 'private'))
        )
        {"PARTITION BY RANGE (\"occurred_at\")" if postgresql else ""}
        """
    )


def _create_legacy_partitions(*, postgresql: bool) -> None:
    if not postgresql:
        return
    parent = _archive_object("legacy_messages", postgresql=True)
    for name, start, end in _MONTH_PARTITIONS:
        partition = _archive_object(name, postgresql=True)
        op.execute(
            f"""
            CREATE TABLE {partition}
            PARTITION OF {parent}
            FOR VALUES FROM ('{start}') TO ('{end}')
            """
        )
    default_partition = _archive_object("legacy_messages_default", postgresql=True)
    op.execute(
        f"""
        CREATE TABLE {default_partition}
        PARTITION OF {parent} DEFAULT
        """
    )


def _create_source_message_identities(*, postgresql: bool) -> None:
    table = _archive_object("source_message_identities", postgresql=postgresql)
    batches = _archive_object("import_batches", postgresql=postgresql)
    messages = _archive_object("legacy_messages", postgresql=postgresql)
    timestamp_type = "TIMESTAMPTZ" if postgresql else "DATETIME"
    op.execute(
        f"""
        CREATE TABLE {table} (
            "source_system" VARCHAR(128) NOT NULL,
            "source_table" VARCHAR(256) NOT NULL,
            "source_record_id" VARCHAR(512) NOT NULL,
            "import_batch_id" VARCHAR(36) NOT NULL,
            "legacy_message_id" VARCHAR(36),
            "occurred_at" {timestamp_type},
            "payload_sha256" VARCHAR(64),
            "state" VARCHAR(32) NOT NULL DEFAULT 'reserved',
            "error_code" VARCHAR(128),
            "created_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" {timestamp_type} NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT "pk_archive_source_message_identity"
                PRIMARY KEY ("source_system", "source_table", "source_record_id"),
            CONSTRAINT "uq_archive_source_message_legacy_id"
                UNIQUE ("legacy_message_id"),
            CONSTRAINT "fk_archive_source_message_batch"
                FOREIGN KEY ("import_batch_id")
                REFERENCES {batches} ("id")
                ON DELETE RESTRICT,
            CONSTRAINT "fk_archive_source_message_legacy"
                FOREIGN KEY ("legacy_message_id", "occurred_at")
                REFERENCES {messages} ("id", "occurred_at")
                ON DELETE RESTRICT,
            CONSTRAINT "ck_archive_source_message_source"
                CHECK ("source_system" IN ('lily.nonebot.chatrecorder.v2', 'nekro.chat_message')),
            CONSTRAINT "ck_archive_source_message_state"
                CHECK ("state" IN ('reserved', 'imported', 'rejected', 'conflict')),
            CONSTRAINT "ck_archive_source_message_legacy_pair"
                CHECK (
                    ("legacy_message_id" IS NULL AND "occurred_at" IS NULL)
                    OR ("legacy_message_id" IS NOT NULL AND "occurred_at" IS NOT NULL)
                ),
            CONSTRAINT "ck_archive_source_message_imported"
                CHECK (
                    "state" <> 'imported'
                    OR ("legacy_message_id" IS NOT NULL AND "occurred_at" IS NOT NULL)
                )
        )
        """
    )


def _create_indexes(*, postgresql: bool) -> None:
    batches = _archive_object("import_batches", postgresql=postgresql)
    mappings = _archive_object("conversation_mappings", postgresql=postgresql)
    messages = _archive_object("legacy_messages", postgresql=postgresql)
    identities = _archive_object("source_message_identities", postgresql=postgresql)
    active_predicate = '"active" IS TRUE' if postgresql else '"active" = 1'

    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_import_batches_status')} "
        f"ON {batches} (\"source_system\", \"status\", \"updated_at\")"
    )
    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_conversation_mappings_lookup')} "
        f"ON {mappings} (\"source_system\", \"source_conversation_key\", \"active\")"
    )
    op.execute(
        f"CREATE UNIQUE INDEX {_index_object('uq_archive_conversation_mappings_active')} "
        f"ON {mappings} (\"source_system\", \"source_conversation_key\") "
        f"WHERE {active_predicate}"
    )
    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_legacy_messages_timeline')} "
        f"ON {messages} (\"conversation_type\", \"conversation_id\", \"occurred_at\", \"id\")"
    )
    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_legacy_messages_batch_occurred')} "
        f"ON {messages} (\"import_batch_id\", \"occurred_at\")"
    )
    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_source_message_identities_batch_state')} "
        f"ON {identities} (\"import_batch_id\", \"state\", \"updated_at\")"
    )
    op.execute(
        f"CREATE INDEX {_index_object('ix_archive_source_message_identities_legacy_message')} "
        f"ON {identities} (\"legacy_message_id\", \"occurred_at\")"
    )


def _link_reply_hint_expression(*, postgresql: bool) -> str:
    """Return a text JSON object expression accepted by both view dialects."""

    if postgresql:
        return """CAST(jsonb_build_object(
                'relation_type', link."relation_type",
                'from_source_event_id', link."from_source_event_id",
                'from_observation_id', link."from_observation_id",
                'to_source_event_id', link."to_source_event_id",
                'target_source_event_id', link."target_source_event_id",
                'target_platform_message_id', link."target_platform_message_id",
                'target_conversation_id', link."target_conversation_id",
                'target_conversation_type', link."target_conversation_type",
                'target_sender_id', link."target_sender_id",
                'confidence', link."confidence",
                'resolver_status', link."resolver_status",
                'raw', COALESCE(link."raw_json"::jsonb, '{}'::jsonb)
            ) AS TEXT)"""
    return """json_object(
                'relation_type', link."relation_type",
                'from_source_event_id', link."from_source_event_id",
                'from_observation_id', link."from_observation_id",
                'to_source_event_id', link."to_source_event_id",
                'target_source_event_id', link."target_source_event_id",
                'target_platform_message_id', link."target_platform_message_id",
                'target_conversation_id', link."target_conversation_id",
                'target_conversation_type', link."target_conversation_type",
                'target_sender_id', link."target_sender_id",
                'confidence', link."confidence",
                'resolver_status', link."resolver_status",
                'raw', json(COALESCE(CAST(link."raw_json" AS TEXT), '{}'))
            )"""


def _response_reply_hint_expression(*, postgresql: bool) -> str:
    if postgresql:
        return """CAST(jsonb_build_object(
                'reply_to_platform_message_id', response."reply_to_platform_message_id",
                'trigger_source_event_id', response."trigger_source_event_id"
            ) AS TEXT)"""
    return """json_object(
                'reply_to_platform_message_id', response."reply_to_platform_message_id",
                'trigger_source_event_id', response."trigger_source_event_id"
            )"""


def _create_timeline_view(*, postgresql: bool) -> None:
    view = _archive_object("message_timeline_v1", postgresql=postgresql)
    messages = _archive_object("legacy_messages", postgresql=postgresql)
    observations = _public_object("event_observations", postgresql=postgresql)
    ingress_receipts = _public_object("ingress_receipts", postgresql=postgresql)
    source_events = _public_object("source_events", postgresql=postgresql)
    event_links = _public_object("event_links", postgresql=postgresql)
    responses = _public_object("responses", postgresql=postgresql)
    timestamp_type = "TIMESTAMPTZ" if postgresql else "DATETIME"
    link_reply_hint = _link_reply_hint_expression(postgresql=postgresql)
    response_reply_hint = _response_reply_hint_expression(postgresql=postgresql)

    op.execute(
        f"""
        CREATE VIEW {view} (
            "id",
            "kind",
            "source_system",
            "source_table",
            "source_record_id",
            "import_batch_id",
            "mapping_version",
            "platform",
            "source_conversation_type",
            "conversation_type",
            "conversation_id",
            "source_conversation_key",
            "bot_id",
            "instance_id",
            "sender_id",
            "sender_name",
            "direction",
            "occurred_at",
            "captured_at",
            "source_persisted_at",
            "received_at",
            "platform_message_id",
            "text",
            "segments_json",
            "reply_hint_json",
            "raw_fields_json",
            "raw_storage_ref",
            "parse_warning",
            "source_event_id",
            "event_link_id",
            "from_source_event_id",
            "from_observation_id",
            "to_source_event_id",
            "relation_type",
            "target_source_event_id",
            "target_platform_message_id",
            "target_conversation_id",
            "target_conversation_type",
            "target_sender_id",
            "confidence",
            "resolver_status",
            "link_raw_json",
            "link_created_at",
            "trigger_source_event_id",
            "reply_to_platform_message_id",
            "response_id"
        ) AS
        SELECT
            legacy."id",
            'legacy_message',
            legacy."source_system",
            legacy."source_table",
            legacy."source_record_id",
            legacy."import_batch_id",
            legacy."mapping_version",
            legacy."platform",
            legacy."source_conversation_type",
            legacy."conversation_type",
            legacy."conversation_id",
            legacy."source_conversation_key",
            legacy."bot_id",
            CAST(NULL AS TEXT),
            legacy."sender_id",
            legacy."sender_name",
            legacy."direction",
            legacy."occurred_at",
            legacy."source_persisted_at",
            legacy."source_persisted_at",
            CAST(NULL AS {timestamp_type}),
            legacy."platform_message_id",
            legacy."content_text",
            CAST(legacy."segments_json" AS TEXT),
            CAST(legacy."reply_hint_json" AS TEXT),
            CAST(legacy."raw_fields_json" AS TEXT),
            legacy."raw_storage_ref",
            legacy."parse_warning",
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS INTEGER),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS {timestamp_type}),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT)
        FROM {messages} AS legacy

        UNION ALL

        SELECT
            CASE
                WHEN link."id" IS NULL
                THEN 'core:observation:' || observation."id"
                ELSE 'core:observation:' || observation."id" || ':link:' || link."id"
            END,
            'core_observation',
            'superlily.core',
            'event_observations',
            observation."id",
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            source_event."platform",
            CAST(NULL AS TEXT),
            source_event."conversation_type",
            source_event."conversation_id",
            CAST(NULL AS TEXT),
            observation."bot_id",
            observation."instance_id",
            observation."sender_id",
            observation."sender_name",
            'unknown',
            source_event."occurred_at",
            receipt."captured_at",
            CAST(NULL AS {timestamp_type}),
            observation."received_at",
            COALESCE(observation."platform_message_id", source_event."message_id"),
            observation."text",
            CAST(observation."segments_json" AS TEXT),
            CASE
                WHEN link."id" IS NULL THEN CAST(NULL AS TEXT)
                ELSE {link_reply_hint}
            END,
            CAST(observation."raw_json" AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            source_event."id",
            link."id",
            link."from_source_event_id",
            link."from_observation_id",
            link."to_source_event_id",
            link."relation_type",
            link."target_source_event_id",
            link."target_platform_message_id",
            link."target_conversation_id",
            link."target_conversation_type",
            link."target_sender_id",
            link."confidence",
            link."resolver_status",
            CAST(link."raw_json" AS TEXT),
            link."created_at",
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT)
        FROM {observations} AS observation
        JOIN {source_events} AS source_event
          ON source_event."id" = observation."source_event_id"
        LEFT JOIN {ingress_receipts} AS receipt
          ON receipt."observation_id" = observation."id"
        LEFT JOIN {event_links} AS link
          ON link."from_observation_id" = observation."id"

        UNION ALL

        SELECT
            'core:response:' || response."id",
            'core_response',
            'superlily.core',
            'responses',
            response."id",
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            response."platform",
            CAST(NULL AS TEXT),
            response."conversation_type",
            response."conversation_id",
            CAST(NULL AS TEXT),
            response."bot_id",
            response."instance_id",
            response."bot_id",
            CAST(NULL AS TEXT),
            'outbound',
            response."occurred_at",
            CAST(NULL AS {timestamp_type}),
            CAST(NULL AS {timestamp_type}),
            response."received_at",
            response."platform_message_id",
            response."text",
            CAST(response."segments_json" AS TEXT),
            {response_reply_hint},
            CAST(response."raw_json" AS TEXT),
            CAST(NULL AS TEXT),
            CASE WHEN response."success" THEN NULL ELSE response."error" END,
            response."trigger_source_event_id",
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS INTEGER),
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            CAST(NULL AS {timestamp_type}),
            response."trigger_source_event_id",
            response."reply_to_platform_message_id",
            response."id"
        FROM {responses} AS response
        """
    )


def upgrade() -> None:
    postgresql = _is_postgresql()
    _create_schema(postgresql=postgresql)
    _create_import_batches(postgresql=postgresql)
    _create_conversation_mappings(postgresql=postgresql)
    _create_legacy_messages(postgresql=postgresql)
    _create_legacy_partitions(postgresql=postgresql)
    _create_source_message_identities(postgresql=postgresql)
    _create_indexes(postgresql=postgresql)
    _create_timeline_view(postgresql=postgresql)


def _table_has_rows(bind: sa.engine.Connection, table: str, *, postgresql: bool) -> bool:
    inspector = sa.inspect(bind)
    schema = _ARCHIVE_SCHEMA if postgresql else None
    if not inspector.has_table(table, schema=schema):
        return False
    qualified = _archive_object(table, postgresql=postgresql)
    return bind.execute(sa.text(f"SELECT 1 FROM {qualified} LIMIT 1")).first() is not None


def _archive_schema_exists(bind: sa.engine.Connection, *, postgresql: bool) -> bool:
    if not postgresql:
        return True
    return sa.inspect(bind).has_schema(_ARCHIVE_SCHEMA)


def downgrade() -> None:
    """Remove only an empty archive, in the exact reverse dependency order.

    Imported rows are audit evidence.  Refusing to drop a non-empty archive
    makes a downgrade safe even if H2 has already started; an operator must
    make a separate data-disposition decision before removing those rows.
    """

    bind = op.get_bind()
    postgresql = bind.dialect.name == "postgresql"
    if not _archive_schema_exists(bind, postgresql=postgresql):
        return

    data_tables = (
        "import_batches",
        "conversation_mappings",
        "source_message_identities",
        "legacy_messages",
    )
    if any(_table_has_rows(bind, table, postgresql=postgresql) for table in data_tables):
        raise RuntimeError(
            "refusing to downgrade a non-empty archive; preserve imported history and "
            "make a separate data-disposition decision first"
        )

    view = _archive_object("message_timeline_v1", postgresql=postgresql)
    op.execute(f"DROP VIEW IF EXISTS {view}")

    for index in reversed(_EXPLICIT_INDEXES):
        op.execute(f"DROP INDEX IF EXISTS {_archive_object(index, postgresql=postgresql)}")

    op.execute(
        f"DROP TABLE IF EXISTS "
        f"{_archive_object('source_message_identities', postgresql=postgresql)}"
    )

    if postgresql:
        op.execute(
            f"DROP TABLE IF EXISTS "
            f"{_archive_object('legacy_messages_default', postgresql=True)}"
        )
        for name, _start, _end in reversed(_MONTH_PARTITIONS):
            op.execute(f"DROP TABLE IF EXISTS {_archive_object(name, postgresql=True)}")

    op.execute(
        f"DROP TABLE IF EXISTS {_archive_object('legacy_messages', postgresql=postgresql)}"
    )
    op.execute(
        f"DROP TABLE IF EXISTS "
        f"{_archive_object('conversation_mappings', postgresql=postgresql)}"
    )
    op.execute(
        f"DROP TABLE IF EXISTS {_archive_object('import_batches', postgresql=postgresql)}"
    )

    if postgresql:
        op.execute(f'DROP SCHEMA IF EXISTS "{_ARCHIVE_SCHEMA}"')
