from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

REPOSITORY_ROOT = Path(__file__).parents[1]
PARENT_REVISION = "0024_agent_product_flow"
ARCHIVE_REVISION = "0025_legacy_history_archive"
HEAD_REVISION = "0031_platform_api_calls"
SQLITE_ARCHIVE_PREFIX = "archive_"

BEHAVIOR_BATCH_ID = "batch-history-behavior-001"
BEHAVIOR_LEGACY_ID = "legacy-history-behavior-001"
BEHAVIOR_IDENTITY_RECORD_ID = "legacy-source-record-behavior-001"
BEHAVIOR_BAD_IDENTITY_RECORD_ID = "legacy-source-record-bad-001"
BEHAVIOR_SOURCE_EVENT_A_ID = "source-event-history-a"
BEHAVIOR_SOURCE_EVENT_B_ID = "source-event-history-b"
BEHAVIOR_OBSERVATION_A_ID = "observation-history-a"
BEHAVIOR_OBSERVATION_B_ID = "observation-history-b"
BEHAVIOR_EVENT_LINK_REPLY_ID = "event-link-history-reply"
BEHAVIOR_EVENT_LINK_MENTION_ID = "event-link-history-mention"
BEHAVIOR_RESPONSE_ID = "response-history-b"
BEHAVIOR_INSTANCE_A_ID = "instance-history-a"
BEHAVIOR_INSTANCE_B_ID = "instance-history-b"

BEHAVIOR_LEGACY_OCCURRED_AT = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
BEHAVIOR_LEGACY_PERSISTED_AT = datetime(2026, 6, 1, 10, 0, 5, tzinfo=timezone.utc)
BEHAVIOR_EVENT_A_OCCURRED_AT = datetime(2026, 6, 2, 11, 0, tzinfo=timezone.utc)
BEHAVIOR_OBSERVATION_A_CAPTURED_AT = datetime(2026, 6, 2, 11, 0, 2, tzinfo=timezone.utc)
BEHAVIOR_OBSERVATION_A_RECEIVED_AT = datetime(2026, 6, 2, 11, 0, 7, tzinfo=timezone.utc)
BEHAVIOR_EVENT_B_OCCURRED_AT = datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc)
BEHAVIOR_OBSERVATION_B_CAPTURED_AT = datetime(2026, 6, 3, 12, 0, 3, tzinfo=timezone.utc)
BEHAVIOR_OBSERVATION_B_RECEIVED_AT = datetime(2026, 6, 3, 12, 0, 8, tzinfo=timezone.utc)
BEHAVIOR_RESPONSE_OCCURRED_AT = datetime(2026, 6, 4, 13, 0, tzinfo=timezone.utc)
BEHAVIOR_RESPONSE_RECEIVED_AT = datetime(2026, 6, 4, 13, 0, 9, tzinfo=timezone.utc)


def _timestamp_param(value: datetime) -> str:
    return value.isoformat()


def _archive_behavior_timestamp(value: datetime, *, postgresql: bool) -> datetime | str:
    return value if postgresql else _timestamp_param(value)


def _run_alembic(
    env: dict[str, str], *arguments: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY_ROOT,
        env=env,
        check=check,
        capture_output=True,
        text=True,
    )


def _sqlite_object(
    connection: sqlite3.Connection, logical_name: str, object_type: str
) -> str:
    candidates = (f"{SQLITE_ARCHIVE_PREFIX}{logical_name}", logical_name)
    placeholders = ", ".join("?" for _ in candidates)
    rows = connection.execute(
        f"SELECT name FROM sqlite_master WHERE type = ? AND name IN ({placeholders})",
        (object_type, *candidates),
    ).fetchall()
    assert len(rows) == 1, (
        f"expected exactly one SQLite {object_type} for archive.{logical_name}, "
        f"found {[row[0] for row in rows]}"
    )
    return str(rows[0][0])


def _sqlite_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    }


def _sqlite_unique_column_sets(
    connection: sqlite3.Connection, table_name: str
) -> set[tuple[str, ...]]:
    unique_sets: set[tuple[str, ...]] = set()
    indexes = connection.execute(f'PRAGMA index_list("{table_name}")').fetchall()
    for index in indexes:
        if not bool(index[2]):
            continue
        index_name = str(index[1])
        columns = tuple(
            str(row[2])
            for row in connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
        )
        unique_sets.add(columns)
    return unique_sets


def _sqlite_foreign_keys(
    connection: sqlite3.Connection, table_name: str
) -> list[tuple[str, str, str]]:
    return [
        (str(row[2]), str(row[3]), str(row[4]))
        for row in connection.execute(f'PRAGMA foreign_key_list("{table_name}")').fetchall()
    ]


def _sqlite_sql(connection: sqlite3.Connection, object_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (object_name,)
    ).fetchone()
    assert row is not None and row[0] is not None, f"missing SQL for {object_name}"
    return str(row[0]).lower()


def _assert_has_any(columns: set[str], names: tuple[str, ...], meaning: str) -> str:
    present = columns.intersection(names)
    assert present, f"{meaning} is absent; expected one of {names}, got {sorted(columns)}"
    return sorted(present)[0]


def _assert_sqlite_archive_contract(
    connection: sqlite3.Connection,
) -> dict[str, str]:
    tables = {
        logical_name: _sqlite_object(connection, logical_name, "table")
        for logical_name in (
            "import_batches",
            "conversation_mappings",
            "legacy_messages",
            "source_message_identities",
        )
    }
    timeline_view = _sqlite_object(connection, "message_timeline_v1", "view")

    import_columns = _sqlite_columns(connection, tables["import_batches"])
    assert {
        "id",
        "source_system",
        "cutover_boundary",
        "status",
        "started_at",
    }.issubset(import_columns)
    _assert_has_any(
        import_columns,
        ("source_snapshot_id", "snapshot_id", "source_snapshot_identity"),
        "import batch snapshot identity",
    )
    _assert_has_any(
        import_columns,
        ("source_schema_version", "schema_version"),
        "import batch source schema version",
    )
    _assert_has_any(import_columns, ("mapping_version",), "import batch mapping version")
    _assert_has_any(
        import_columns,
        ("completed_at", "ended_at", "finished_at"),
        "import batch end time",
    )
    _assert_has_any(
        import_columns,
        ("imported_count", "imported_rows", "row_count"),
        "import batch imported count",
    )
    _assert_has_any(
        import_columns,
        ("rejected_count", "rejected_rows"),
        "import batch rejected count",
    )
    _assert_has_any(
        import_columns,
        ("source_hash", "snapshot_hash", "manifest_hash"),
        "import batch source hash",
    )
    _assert_has_any(
        import_columns,
        ("error_summary", "error_summary_json"),
        "import batch error summary",
    )

    mapping_columns = _sqlite_columns(connection, tables["conversation_mappings"])
    assert {
        "id",
        "source_system",
        "source_conversation_key",
        "platform",
        "conversation_type",
        "conversation_id",
    }.issubset(mapping_columns)
    mapping_unique_sets = _sqlite_unique_column_sets(
        connection, tables["conversation_mappings"]
    )
    assert any(
        {"source_system", "source_conversation_key"}.issubset(columns)
        for columns in mapping_unique_sets
    ), mapping_unique_sets

    legacy_columns = _sqlite_columns(connection, tables["legacy_messages"])
    assert {
        "id",
        "source_system",
        "source_table",
        "source_record_id",
        "conversation_type",
        "conversation_id",
        "sender_id",
        "direction",
        "occurred_at",
        "platform_message_id",
        "content_text",
        "parse_warning",
    }.issubset(legacy_columns)
    batch_column = _assert_has_any(
        legacy_columns,
        ("import_batch_id", "batch_id"),
        "legacy message import batch reference",
    )
    assert "mapping_version" in legacy_columns
    assert "source_persisted_at" in legacy_columns
    _assert_has_any(
        legacy_columns,
        ("source_conversation_key", "raw_conversation_key"),
        "legacy message raw conversation key",
    )
    _assert_has_any(
        legacy_columns,
        ("account_id", "bot_id", "instance_id"),
        "legacy message account identity",
    )
    _assert_has_any(
        legacy_columns,
        ("segments_json", "segments", "parsed_segments_json"),
        "legacy message parsed segments",
    )
    _assert_has_any(
        legacy_columns,
        ("reply_to_platform_message_id", "reply_hint", "reply_hint_json"),
        "legacy message reply hint",
    )
    _assert_has_any(
        legacy_columns,
        ("raw_json", "raw_fields_json", "raw_reference_json"),
        "legacy message raw field reference",
    )
    assert ("source_system", "source_table", "source_record_id") in _sqlite_unique_column_sets(
        connection, tables["legacy_messages"]
    )

    foreign_keys = _sqlite_foreign_keys(connection, tables["legacy_messages"])
    assert any(
        target_table == tables["import_batches"] and source_column == batch_column
        for target_table, source_column, _target_column in foreign_keys
    ), foreign_keys

    identity_columns = _sqlite_columns(connection, tables["source_message_identities"])
    assert {
        "source_system",
        "source_table",
        "source_record_id",
        "import_batch_id",
        "state",
    }.issubset(identity_columns)
    assert (
        "source_system",
        "source_table",
        "source_record_id",
    ) in _sqlite_unique_column_sets(connection, tables["source_message_identities"])

    archive_sql = "\n".join(
        _sqlite_sql(connection, tables[logical_name])
        for logical_name in (
            "import_batches",
            "conversation_mappings",
            "legacy_messages",
            "source_message_identities",
        )
    )
    assert "lily.nonebot.chatrecorder.v2" in archive_sql
    assert "nekro.chat_message" in archive_sql
    assert "conversation_type" in archive_sql
    assert "group" in archive_sql and "private" in archive_sql
    assert "direction" in archive_sql
    assert "unknown" in archive_sql

    view_columns = _sqlite_columns(connection, timeline_view)
    assert {
        "source_system",
        "mapping_version",
        "occurred_at",
        "captured_at",
        "source_persisted_at",
        "received_at",
        "instance_id",
        "event_link_id",
        "relation_type",
        "target_platform_message_id",
        "reply_hint_json",
    }.issubset(view_columns)
    assert "conversation_id" in view_columns
    connection.execute(f'SELECT * FROM "{timeline_view}" LIMIT 0').fetchall()
    assert connection.execute(f'SELECT COUNT(*) FROM "{timeline_view}"').fetchone()[0] == 0

    return {**tables, "message_timeline_v1": timeline_view}


def _archive_behavior_archive_tables(*, postgresql: bool) -> dict[str, str]:
    if postgresql:
        return {
            name: f'"archive"."{name}"'
            for name in (
                "import_batches",
                "legacy_messages",
                "source_message_identities",
                "message_timeline_v1",
            )
        }
    return {
        name: f'"{name}"'
        for name in (
            "import_batches",
            "legacy_messages",
            "source_message_identities",
            "message_timeline_v1",
        )
    }


def _archive_behavior_public_table(name: str, *, postgresql: bool) -> str:
    if postgresql:
        return f'"public"."{name}"'
    return f'"{name}"'


def _archive_behavior_json_param(
    name: str, *, postgresql: bool, archive: bool
) -> str:
    if not postgresql:
        return f":{name}"
    json_type = "JSONB" if archive else "JSON"
    return f"CAST(:{name} AS {json_type})"


def _archive_behavior_statements(
    archive_tables: dict[str, str], *, postgresql: bool
) -> list[tuple[str, dict[str, Any]]]:
    public = lambda name: _archive_behavior_public_table(name, postgresql=postgresql)
    archive_json = lambda name: _archive_behavior_json_param(
        name, postgresql=postgresql, archive=True
    )
    public_json = lambda name: _archive_behavior_json_param(
        name, postgresql=postgresql, archive=False
    )
    ts = lambda value: _archive_behavior_timestamp(value, postgresql=postgresql)
    statements: list[tuple[str, dict[str, Any]]] = []

    statements.append(
        (
            f"""
            INSERT INTO {archive_tables['import_batches']} (
                id, source_system, source_snapshot_id, source_schema_version,
                mapping_version, cutover_boundary, status, started_at, finished_at,
                source_row_count, imported_count, rejected_count, duplicate_count,
                manifest_hash, content_hash, checkpoint_json, error_summary_json
            ) VALUES (
                :id, :source_system, :source_snapshot_id, :source_schema_version,
                :mapping_version, :cutover_boundary, :status, :started_at, :finished_at,
                :source_row_count, :imported_count, :rejected_count, :duplicate_count,
                :manifest_hash, :content_hash,
                {archive_json('checkpoint_json')}, {archive_json('error_summary_json')}
            )
            """.strip(),
            {
                "id": BEHAVIOR_BATCH_ID,
                "source_system": "lily.nonebot.chatrecorder.v2",
                "source_snapshot_id": "snapshot-history-behavior-001",
                "source_schema_version": "chatrecorder-v2",
                "mapping_version": "history-map-v1",
                "cutover_boundary": ts(datetime(2026, 6, 19, 11, 45, 17, 171050, tzinfo=timezone.utc)),
                "status": "completed",
                "started_at": ts(datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)),
                "finished_at": ts(datetime(2026, 6, 5, 0, 1, tzinfo=timezone.utc)),
                "source_row_count": 1,
                "imported_count": 1,
                "rejected_count": 0,
                "duplicate_count": 0,
                "manifest_hash": "a" * 64,
                "content_hash": "b" * 64,
                "checkpoint_json": json.dumps({"last_source_record_id": "legacy-001"}),
                "error_summary_json": json.dumps({}),
            },
        )
    )
    statements.append(
        (
            f"""
            INSERT INTO {archive_tables['legacy_messages']} (
                id, source_system, source_table, source_record_id, import_batch_id,
                mapping_version, bot_id, source_conversation_key,
                source_conversation_type, platform, conversation_type, conversation_id,
                sender_id, sender_name, direction, occurred_at, source_persisted_at,
                platform_message_id, content_text, segments_json, reply_hint_json,
                raw_fields_json, raw_storage_ref, parse_warning
            ) VALUES (
                :id, :source_system, :source_table, :source_record_id, :import_batch_id,
                :mapping_version, :bot_id, :source_conversation_key,
                :source_conversation_type, :platform, :conversation_type, :conversation_id,
                :sender_id, :sender_name, :direction, :occurred_at, :source_persisted_at,
                :platform_message_id, :content_text,
                {archive_json('segments_json')}, {archive_json('reply_hint_json')},
                {archive_json('raw_fields_json')}, :raw_storage_ref, :parse_warning
            )
            """.strip(),
            {
                "id": BEHAVIOR_LEGACY_ID,
                "source_system": "lily.nonebot.chatrecorder.v2",
                "source_table": "nonebot_plugin_chatrecorder_messagerecord_v2",
                "source_record_id": "legacy-source-record-001",
                "import_batch_id": BEHAVIOR_BATCH_ID,
                "mapping_version": "history-map-v1",
                "bot_id": "legacy-instance-a",
                "source_conversation_key": "session-persist-history-a",
                "source_conversation_type": "scene_type=1",
                "platform": "qq",
                "conversation_type": "group",
                "conversation_id": "history-group-a",
                "sender_id": "legacy-sender-a",
                "sender_name": "Legacy Sender A",
                "direction": "inbound",
                "occurred_at": ts(BEHAVIOR_LEGACY_OCCURRED_AT),
                "source_persisted_at": ts(BEHAVIOR_LEGACY_PERSISTED_AT),
                "platform_message_id": "legacy-platform-message-a",
                "content_text": "legacy history message",
                "segments_json": json.dumps([{"type": "text", "text": "legacy history message"}]),
                "reply_hint_json": json.dumps(
                    {
                        "relation_type": "reply_to",
                        "target_platform_message_id": "legacy-target-message",
                        "target_conversation_id": "history-group-a",
                    }
                ),
                "raw_fields_json": json.dumps({"source": "fixture"}),
                "raw_storage_ref": "archive-fixture://legacy-001",
                "parse_warning": None,
            },
        )
    )
    statements.append(
        (
            f"""
            INSERT INTO {archive_tables['source_message_identities']} (
                source_system, source_table, source_record_id, import_batch_id,
                legacy_message_id, occurred_at, payload_sha256, state, error_code
            ) VALUES (
                :source_system, :source_table, :source_record_id, :import_batch_id,
                :legacy_message_id, :occurred_at, :payload_sha256, :state, :error_code
            )
            """.strip(),
            {
                "source_system": "lily.nonebot.chatrecorder.v2",
                "source_table": "nonebot_plugin_chatrecorder_messagerecord_v2",
                "source_record_id": BEHAVIOR_IDENTITY_RECORD_ID,
                "import_batch_id": BEHAVIOR_BATCH_ID,
                "legacy_message_id": BEHAVIOR_LEGACY_ID,
                "occurred_at": ts(BEHAVIOR_LEGACY_OCCURRED_AT),
                "payload_sha256": "c" * 64,
                "state": "imported",
                "error_code": None,
            },
        )
    )

    statements.extend(
        [
            (
                f"""
                INSERT INTO {public('bot_instances')} (
                    id, platform, adapter, bot_id, role, reported_status,
                    first_seen_at, metadata_json
                ) VALUES (
                    :id, :platform, :adapter, :bot_id, :role, :reported_status,
                    :first_seen_at, {public_json('metadata_json')}
                )
                """.strip(),
                {
                    "id": BEHAVIOR_INSTANCE_A_ID,
                    "platform": "qq",
                    "adapter": "onebot_v11",
                    "bot_id": "bot-history-a",
                    "role": "primary",
                    "reported_status": "online",
                    "first_seen_at": ts(datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)),
                    "metadata_json": json.dumps({"fixture": "history-a"}),
                },
            ),
            (
                f"""
                INSERT INTO {public('bot_instances')} (
                    id, platform, adapter, bot_id, role, reported_status,
                    first_seen_at, metadata_json
                ) VALUES (
                    :id, :platform, :adapter, :bot_id, :role, :reported_status,
                    :first_seen_at, {public_json('metadata_json')}
                )
                """.strip(),
                {
                    "id": BEHAVIOR_INSTANCE_B_ID,
                    "platform": "telegram",
                    "adapter": "telegram_bot_api",
                    "bot_id": "bot-history-b",
                    "role": "secondary",
                    "reported_status": "online",
                    "first_seen_at": ts(datetime(2026, 6, 1, 0, 0, 1, tzinfo=timezone.utc)),
                    "metadata_json": json.dumps({"fixture": "history-b"}),
                },
            ),
        ]
    )
    statements.extend(
        [
            (
                f"""
                INSERT INTO {public('source_events')} (
                    id, platform, event_type, conversation_id, conversation_type,
                    message_id, occurred_at, first_received_at
                ) VALUES (
                    :id, :platform, :event_type, :conversation_id, :conversation_type,
                    :message_id, :occurred_at, :first_received_at
                )
                """.strip(),
                {
                    "id": BEHAVIOR_SOURCE_EVENT_A_ID,
                    "platform": "qq",
                    "event_type": "message",
                    "conversation_id": "history-group-a",
                    "conversation_type": "group",
                    "message_id": "source-platform-message-a",
                    "occurred_at": ts(BEHAVIOR_EVENT_A_OCCURRED_AT),
                    "first_received_at": ts(BEHAVIOR_OBSERVATION_A_RECEIVED_AT),
                },
            ),
            (
                f"""
                INSERT INTO {public('source_events')} (
                    id, platform, event_type, conversation_id, conversation_type,
                    message_id, occurred_at, first_received_at
                ) VALUES (
                    :id, :platform, :event_type, :conversation_id, :conversation_type,
                    :message_id, :occurred_at, :first_received_at
                )
                """.strip(),
                {
                    "id": BEHAVIOR_SOURCE_EVENT_B_ID,
                    "platform": "telegram",
                    "event_type": "message",
                    "conversation_id": "history-private-b",
                    "conversation_type": "private",
                    "message_id": "source-platform-message-b",
                    "occurred_at": ts(BEHAVIOR_EVENT_B_OCCURRED_AT),
                    "first_received_at": ts(BEHAVIOR_OBSERVATION_B_RECEIVED_AT),
                },
            ),
        ]
    )

    observation_sql = f"""
        INSERT INTO {public('event_observations')} (
            id, source_event_id, reported_source_event_id, platform_message_id,
            instance_id, idempotency_key, adapter, bot_id, conversation_name,
            sender_id, sender_name, sender_roles_json, text, segments_json,
            attachments_json, raw_json, metadata_json, capture_profile,
            capture_policy_version, capture_status, sanitizer_version,
            collector_sanitizer_version, original_payload_sha256,
            original_payload_size_bytes, omitted_fields_json, platform_extra_json,
            capture_reason, received_at
        ) VALUES (
            :id, :source_event_id, :reported_source_event_id, :platform_message_id,
            :instance_id, :idempotency_key, :adapter, :bot_id, :conversation_name,
            :sender_id, :sender_name, {public_json('sender_roles_json')}, :text,
            {public_json('segments_json')}, {public_json('attachments_json')},
            {public_json('raw_json')}, {public_json('metadata_json')},
            :capture_profile, :capture_policy_version, :capture_status,
            :sanitizer_version, :collector_sanitizer_version,
            :original_payload_sha256, :original_payload_size_bytes,
            {public_json('omitted_fields_json')}, {public_json('platform_extra_json')},
            :capture_reason, :received_at
        )
        """.strip()
    statements.extend(
        [
            (
                observation_sql,
                {
                    "id": BEHAVIOR_OBSERVATION_A_ID,
                    "source_event_id": BEHAVIOR_SOURCE_EVENT_A_ID,
                    "reported_source_event_id": "reported-history-a",
                    "platform_message_id": "observation-platform-message-a",
                    "instance_id": BEHAVIOR_INSTANCE_A_ID,
                    "idempotency_key": "idempotency-history-a",
                    "adapter": "onebot_v11",
                    "bot_id": "bot-history-a",
                    "conversation_name": "History Group A",
                    "sender_id": "sender-history-a",
                    "sender_name": "Sender History A",
                    "sender_roles_json": json.dumps(["member"]),
                    "text": "observation history a",
                    "segments_json": json.dumps([{"type": "text", "text": "observation history a"}]),
                    "attachments_json": json.dumps([]),
                    "raw_json": json.dumps({"fixture": "observation-a"}),
                    "metadata_json": json.dumps({"fixture": "observation-a"}),
                    "capture_profile": "operational",
                    "capture_policy_version": "default-operational-v1",
                    "capture_status": "complete",
                    "sanitizer_version": "superlily.sanitizer.v1",
                    "collector_sanitizer_version": "bridge.sanitizer.v1",
                    "original_payload_sha256": "d" * 64,
                    "original_payload_size_bytes": 128,
                    "omitted_fields_json": json.dumps([]),
                    "platform_extra_json": json.dumps({"fixture": True}),
                    "capture_reason": "fixture",
                    "received_at": ts(BEHAVIOR_OBSERVATION_A_RECEIVED_AT),
                },
            ),
            (
                observation_sql,
                {
                    "id": BEHAVIOR_OBSERVATION_B_ID,
                    "source_event_id": BEHAVIOR_SOURCE_EVENT_B_ID,
                    "reported_source_event_id": "reported-history-b",
                    "platform_message_id": None,
                    "instance_id": BEHAVIOR_INSTANCE_B_ID,
                    "idempotency_key": "idempotency-history-b",
                    "adapter": "telegram_bot_api",
                    "bot_id": "bot-history-b",
                    "conversation_name": "History Private B",
                    "sender_id": "sender-history-b",
                    "sender_name": "Sender History B",
                    "sender_roles_json": json.dumps(["user"]),
                    "text": "observation history b",
                    "segments_json": json.dumps([{"type": "text", "text": "observation history b"}]),
                    "attachments_json": json.dumps([]),
                    "raw_json": json.dumps({"fixture": "observation-b"}),
                    "metadata_json": json.dumps({"fixture": "observation-b"}),
                    "capture_profile": "operational",
                    "capture_policy_version": "default-operational-v1",
                    "capture_status": "complete",
                    "sanitizer_version": "superlily.sanitizer.v1",
                    "collector_sanitizer_version": "bridge.sanitizer.v1",
                    "original_payload_sha256": "e" * 64,
                    "original_payload_size_bytes": 129,
                    "omitted_fields_json": json.dumps([]),
                    "platform_extra_json": json.dumps({"fixture": True}),
                    "capture_reason": "fixture",
                    "received_at": ts(BEHAVIOR_OBSERVATION_B_RECEIVED_AT),
                },
            ),
        ]
    )

    receipt_sql = f"""
        INSERT INTO {public('ingress_receipts')} (
            id, observation_id, instance_id, spool_id, collector_sequence,
            record_sha256, captured_at, committed_at
        ) VALUES (
            :id, :observation_id, :instance_id, :spool_id, :collector_sequence,
            :record_sha256, :captured_at, :committed_at
        )
        """.strip()
    statements.extend(
        [
            (
                receipt_sql,
                {
                    "id": "receipt-history-a",
                    "observation_id": BEHAVIOR_OBSERVATION_A_ID,
                    "instance_id": BEHAVIOR_INSTANCE_A_ID,
                    "spool_id": "spool-history-a",
                    "collector_sequence": 1,
                    "record_sha256": "a" * 64,
                    "captured_at": ts(BEHAVIOR_OBSERVATION_A_CAPTURED_AT),
                    "committed_at": ts(
                        datetime(2026, 6, 2, 11, 0, 8, tzinfo=timezone.utc)
                    ),
                },
            ),
            (
                receipt_sql,
                {
                    "id": "receipt-history-b",
                    "observation_id": BEHAVIOR_OBSERVATION_B_ID,
                    "instance_id": BEHAVIOR_INSTANCE_B_ID,
                    "spool_id": "spool-history-b",
                    "collector_sequence": 1,
                    "record_sha256": "b" * 64,
                    "captured_at": ts(BEHAVIOR_OBSERVATION_B_CAPTURED_AT),
                    "committed_at": ts(
                        datetime(2026, 6, 3, 12, 0, 9, tzinfo=timezone.utc)
                    ),
                },
            ),
        ]
    )

    link_sql = f"""
        INSERT INTO {public('event_links')} (
            id, from_source_event_id, from_observation_id, to_source_event_id,
            relation_type, target_source_event_id, target_platform_message_id,
            target_conversation_id, target_conversation_type, target_sender_id,
            confidence, resolver_status, raw_json, created_at
        ) VALUES (
            :id, :from_source_event_id, :from_observation_id, :to_source_event_id,
            :relation_type, :target_source_event_id, :target_platform_message_id,
            :target_conversation_id, :target_conversation_type, :target_sender_id,
            :confidence, :resolver_status, {public_json('raw_json')}, :created_at
        )
        """.strip()
    statements.extend(
        [
            (
                link_sql,
                {
                    "id": BEHAVIOR_EVENT_LINK_REPLY_ID,
                    "from_source_event_id": BEHAVIOR_SOURCE_EVENT_A_ID,
                    "from_observation_id": BEHAVIOR_OBSERVATION_A_ID,
                    "to_source_event_id": None,
                    "relation_type": "reply_to",
                    "target_source_event_id": None,
                    "target_platform_message_id": "observation-platform-message-a",
                    "target_conversation_id": "history-group-a",
                    "target_conversation_type": "group",
                    "target_sender_id": "sender-history-a",
                    "confidence": 91,
                    "resolver_status": "resolved",
                    "raw_json": json.dumps({"fixture": "reply-link"}),
                    "created_at": ts(datetime(2026, 6, 2, 11, 0, 8, tzinfo=timezone.utc)),
                },
            ),
            (
                link_sql,
                {
                    "id": BEHAVIOR_EVENT_LINK_MENTION_ID,
                    "from_source_event_id": BEHAVIOR_SOURCE_EVENT_A_ID,
                    "from_observation_id": BEHAVIOR_OBSERVATION_A_ID,
                    "to_source_event_id": None,
                    "relation_type": "mentions",
                    "target_source_event_id": "target-source-event-mention",
                    "target_platform_message_id": "target-platform-message-mention",
                    "target_conversation_id": "target-history-private",
                    "target_conversation_type": "private",
                    "target_sender_id": "target-sender-mention",
                    "confidence": 73,
                    "resolver_status": "resolved",
                    "raw_json": json.dumps({"fixture": "mention-link"}),
                    "created_at": ts(datetime(2026, 6, 2, 11, 0, 9, tzinfo=timezone.utc)),
                },
            ),
        ]
    )

    statements.append(
        (
            f"""
            INSERT INTO {public('responses')} (
                id, source_response_id, instance_id, idempotency_key,
                trigger_observation_id, trigger_source_event_id, trace_id,
                response_type, platform, adapter, bot_id, conversation_id,
                conversation_type, platform_message_id, reply_to_platform_message_id,
                text, segments_json, attachments_json, success, error, latency_ms,
                raw_json, metadata_json, occurred_at, received_at
            ) VALUES (
                :id, :source_response_id, :instance_id, :idempotency_key,
                :trigger_observation_id, :trigger_source_event_id, :trace_id,
                :response_type, :platform, :adapter, :bot_id, :conversation_id,
                :conversation_type, :platform_message_id, :reply_to_platform_message_id,
                :text, {public_json('segments_json')}, {public_json('attachments_json')},
                :success, :error, :latency_ms, {public_json('raw_json')},
                {public_json('metadata_json')}, :occurred_at, :received_at
            )
            """.strip(),
            {
                "id": BEHAVIOR_RESPONSE_ID,
                "source_response_id": "source-response-history-b",
                "instance_id": BEHAVIOR_INSTANCE_B_ID,
                "idempotency_key": "idempotency-response-history-b",
                "trigger_observation_id": BEHAVIOR_OBSERVATION_B_ID,
                "trigger_source_event_id": BEHAVIOR_SOURCE_EVENT_B_ID,
                "trace_id": "trace-history-b",
                "response_type": "message",
                "platform": "telegram",
                "adapter": "telegram_bot_api",
                "bot_id": "bot-history-b",
                "conversation_id": "history-private-b",
                "conversation_type": "private",
                "platform_message_id": "response-platform-message-b",
                "reply_to_platform_message_id": "source-platform-message-b",
                "text": "response history b",
                "segments_json": json.dumps([{"type": "text", "text": "response history b"}]),
                "attachments_json": json.dumps([]),
                "success": True,
                "error": None,
                "latency_ms": 12,
                "raw_json": json.dumps({"fixture": "response-b"}),
                "metadata_json": json.dumps({"fixture": "response-b"}),
                "occurred_at": ts(BEHAVIOR_RESPONSE_OCCURRED_AT),
                "received_at": ts(BEHAVIOR_RESPONSE_RECEIVED_AT),
            },
        )
    )
    return statements


def _insert_archive_behavior_fixture_sqlite(
    connection: sqlite3.Connection, tables: dict[str, str]
) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    for statement, parameters in _archive_behavior_statements(
        {key: f'"{value}"' for key, value in tables.items() if key != "message_timeline_v1"},
        postgresql=False,
    ):
        connection.execute(statement, parameters)
    connection.commit()


async def _insert_archive_behavior_fixture_postgres(connection: Any) -> None:
    for statement, parameters in _archive_behavior_statements(
        _archive_behavior_archive_tables(postgresql=True), postgresql=True
    ):
        await connection.execute(text(statement), parameters)


def _timeline_json_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _timeline_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _assert_timeline_hint(row: dict[str, Any], *needles: str | None) -> None:
    hint = _timeline_json_text(row.get("reply_hint_json"))
    assert hint.strip() not in {"", "{}", "null"}, row
    for needle in needles:
        if needle is None:
            continue
        assert needle in hint, (needle, hint, row)


def _assert_archive_behavior_rows(
    view_columns: set[str], rows: list[dict[str, Any]]
) -> None:
    assert {
        "id",
        "kind",
        "instance_id",
        "conversation_id",
        "platform_message_id",
        "occurred_at",
        "captured_at",
        "source_persisted_at",
        "received_at",
        "reply_hint_json",
        "event_link_id",
        "relation_type",
        "target_source_event_id",
        "target_platform_message_id",
        "target_conversation_id",
        "target_conversation_type",
    }.issubset(view_columns)
    assert len(rows) == 5, rows
    assert len({row["id"] for row in rows}) == len(rows), rows

    legacy_rows = [row for row in rows if row["kind"] == "legacy_message"]
    observation_rows = [row for row in rows if row["kind"] == "core_observation"]
    response_rows = [row for row in rows if row["kind"] == "core_response"]
    assert len(legacy_rows) == 1
    assert len(observation_rows) == 3
    assert len(response_rows) == 1

    legacy = legacy_rows[0]
    assert legacy["id"] == BEHAVIOR_LEGACY_ID
    assert legacy["source_system"] == "lily.nonebot.chatrecorder.v2"
    assert legacy["source_record_id"] == "legacy-source-record-001"
    assert legacy["import_batch_id"] == BEHAVIOR_BATCH_ID
    assert legacy["mapping_version"] == "history-map-v1"
    assert legacy["conversation_id"] == "history-group-a"
    assert legacy["platform_message_id"] == "legacy-platform-message-a"
    assert _timeline_time(legacy["occurred_at"]) == BEHAVIOR_LEGACY_OCCURRED_AT
    assert _timeline_time(legacy["source_persisted_at"]) == BEHAVIOR_LEGACY_PERSISTED_AT
    assert _timeline_time(legacy["captured_at"]) == BEHAVIOR_LEGACY_PERSISTED_AT
    _assert_timeline_hint(
        legacy, "reply_to", "legacy-target-message", "history-group-a"
    )

    observation_a_rows = [
        row
        for row in observation_rows
        if row["from_observation_id"] == BEHAVIOR_OBSERVATION_A_ID
    ]
    observation_b_rows = [
        row
        for row in observation_rows
        if row["from_observation_id"] is None
        and row["source_record_id"] == BEHAVIOR_OBSERVATION_B_ID
    ]
    assert len(observation_a_rows) == 2
    assert len(observation_b_rows) == 1

    expected_links = {
        BEHAVIOR_EVENT_LINK_REPLY_ID: {
            "relation_type": "reply_to",
            "target_source_event_id": None,
            "target_platform_message_id": "observation-platform-message-a",
            "target_conversation_id": "history-group-a",
            "target_conversation_type": "group",
        },
        BEHAVIOR_EVENT_LINK_MENTION_ID: {
            "relation_type": "mentions",
            "target_source_event_id": "target-source-event-mention",
            "target_platform_message_id": "target-platform-message-mention",
            "target_conversation_id": "target-history-private",
            "target_conversation_type": "private",
        },
    }
    for row in observation_a_rows:
        link_id = row["event_link_id"]
        assert link_id in expected_links
        expected = expected_links[link_id]
        assert row["id"] == (
            f"core:observation:{BEHAVIOR_OBSERVATION_A_ID}:link:{link_id}"
        )
        for field, value in expected.items():
            assert row[field] == value, (field, row)
        assert row["instance_id"] == BEHAVIOR_INSTANCE_A_ID
        assert row["conversation_id"] == "history-group-a"
        assert row["platform_message_id"] == "observation-platform-message-a"
        assert _timeline_time(row["occurred_at"]) == BEHAVIOR_EVENT_A_OCCURRED_AT
        assert _timeline_time(row["captured_at"]) == BEHAVIOR_OBSERVATION_A_CAPTURED_AT
        assert _timeline_time(row["received_at"]) == BEHAVIOR_OBSERVATION_A_RECEIVED_AT
        _assert_timeline_hint(
            row,
            expected["relation_type"],
            expected["target_source_event_id"],
            expected["target_platform_message_id"],
            expected["target_conversation_id"],
        )

    observation_b = observation_b_rows[0]
    assert observation_b["instance_id"] == BEHAVIOR_INSTANCE_B_ID
    assert observation_b["conversation_id"] == "history-private-b"
    assert observation_b["platform_message_id"] == "source-platform-message-b"
    assert observation_b["event_link_id"] is None
    assert observation_b["relation_type"] is None
    assert _timeline_time(observation_b["occurred_at"]) == BEHAVIOR_EVENT_B_OCCURRED_AT
    assert _timeline_time(observation_b["captured_at"]) == BEHAVIOR_OBSERVATION_B_CAPTURED_AT
    assert _timeline_time(observation_b["received_at"]) == BEHAVIOR_OBSERVATION_B_RECEIVED_AT

    response = response_rows[0]
    assert response["id"] == f"core:response:{BEHAVIOR_RESPONSE_ID}"
    assert response["instance_id"] == BEHAVIOR_INSTANCE_B_ID
    assert response["conversation_id"] == "history-private-b"
    assert response["platform_message_id"] == "response-platform-message-b"
    assert response["source_event_id"] == BEHAVIOR_SOURCE_EVENT_B_ID
    assert response["captured_at"] is None
    assert _timeline_time(response["occurred_at"]) == BEHAVIOR_RESPONSE_OCCURRED_AT
    assert _timeline_time(response["received_at"]) == BEHAVIOR_RESPONSE_RECEIVED_AT
    _assert_timeline_hint(
        response, "source-platform-message-b", BEHAVIOR_SOURCE_EVENT_B_ID
    )


def _assert_sqlite_identity_composite_fk_rolls_back(
    connection: sqlite3.Connection, identity_table: str
) -> None:
    invalid_insert = f"""
        INSERT INTO "{identity_table}" (
            source_system, source_table, source_record_id, import_batch_id,
            legacy_message_id, occurred_at, payload_sha256, state
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """
    connection.execute("BEGIN")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                invalid_insert,
                (
                    "lily.nonebot.chatrecorder.v2",
                    "nonebot_plugin_chatrecorder_messagerecord_v2",
                    BEHAVIOR_BAD_IDENTITY_RECORD_ID,
                    BEHAVIOR_BATCH_ID,
                    "missing-legacy-message",
                    _timestamp_param(BEHAVIOR_LEGACY_OCCURRED_AT),
                    "f" * 64,
                    "imported",
                ),
            )
    finally:
        connection.rollback()
    count = connection.execute(
        f"SELECT COUNT(*) FROM \"{identity_table}\" WHERE source_record_id = ?",
        (BEHAVIOR_BAD_IDENTITY_RECORD_ID,),
    ).fetchone()[0]
    assert count == 0


async def _assert_postgres_identity_composite_fk_rolls_back(
    connection: Any, identity_table: str
) -> None:
    invalid_insert = f"""
        INSERT INTO {identity_table} (
            source_system, source_table, source_record_id, import_batch_id,
            legacy_message_id, occurred_at, payload_sha256, state
        ) VALUES (
            :source_system, :source_table, :source_record_id, :import_batch_id,
            :legacy_message_id, :occurred_at, :payload_sha256, :state
        )
    """.strip()
    transaction = await connection.begin()
    try:
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(invalid_insert),
                {
                    "source_system": "lily.nonebot.chatrecorder.v2",
                    "source_table": "nonebot_plugin_chatrecorder_messagerecord_v2",
                    "source_record_id": BEHAVIOR_BAD_IDENTITY_RECORD_ID,
                    "import_batch_id": BEHAVIOR_BATCH_ID,
                    "legacy_message_id": "missing-legacy-message",
                    "occurred_at": BEHAVIOR_LEGACY_OCCURRED_AT,
                    "payload_sha256": "f" * 64,
                    "state": "imported",
                },
            )
    finally:
        await transaction.rollback()
    count = await connection.scalar(
        text(
            f"SELECT COUNT(*) FROM {identity_table} "
            "WHERE source_record_id = :source_record_id"
        ),
        {"source_record_id": BEHAVIOR_BAD_IDENTITY_RECORD_ID},
    )
    assert count == 0


def test_sqlite_legacy_history_archive_contract_and_round_trip(tmp_path: Path) -> None:
    database_path = tmp_path / "history_archive.sqlite"
    env = {
        **os.environ,
        "SUPERLILY_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
    }

    _run_alembic(env, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == (HEAD_REVISION,)
        _assert_sqlite_archive_contract(connection)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='view' AND name='archive_message_timeline_v2'"
        ).fetchone() == (1,)

    _run_alembic(env, "check")
    _run_alembic(env, "downgrade", PARENT_REVISION)
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == (PARENT_REVISION,)
        archive_objects = connection.execute(
            "SELECT name FROM sqlite_master "
            "WHERE name LIKE 'archive_%' AND name NOT LIKE 'archive_version%'"
        ).fetchall()
        assert archive_objects == []

    _run_alembic(env, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version == (HEAD_REVISION,)
        _assert_sqlite_archive_contract(connection)


def test_sqlite_non_empty_archive_timeline_and_identity_behavior(tmp_path: Path) -> None:
    database_path = tmp_path / "history_archive_behavior.sqlite"
    env = {
        **os.environ,
        "SUPERLILY_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
    }

    _run_alembic(env, "upgrade", "head")
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        tables = _assert_sqlite_archive_contract(connection)
        _insert_archive_behavior_fixture_sqlite(connection, tables)

        view_name = tables["message_timeline_v1"]
        result = connection.execute(
            f'SELECT * FROM "{view_name}" ORDER BY kind, id'
        )
        rows = [dict(row) for row in result.fetchall()]
        view_columns = {str(column[0]) for column in result.description}
        _assert_archive_behavior_rows(view_columns, rows)
        _assert_sqlite_identity_composite_fk_rolls_back(
            connection, tables["source_message_identities"]
        )


async def _postgres_archive_snapshot(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            schema_exists = bool(
                await connection.scalar(
                    text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'archive')")
                )
            )
            tables = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT c.relname FROM pg_class AS c "
                            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'archive' AND c.relkind IN ('r', 'p')"
                        )
                    )
                ).all()
            )
            views = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT c.relname FROM pg_class AS c "
                            "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                            "WHERE n.nspname = 'archive' AND c.relkind = 'v'"
                        )
                    )
                ).all()
            )
            columns = {
                table_name: set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = 'archive' AND table_name = :table_name"
                            ),
                            {"table_name": table_name},
                        )
                    ).all()
                )
                for table_name in (
                    "import_batches",
                    "conversation_mappings",
                    "legacy_messages",
                    "source_message_identities",
                )
            }
            constraints = (
                await connection.execute(
                    text(
                        "SELECT t.relname AS table_name, c.conname, c.contype::text AS contype, "
                        "pg_get_constraintdef(c.oid) AS definition, "
                        "array_agg(a.attname ORDER BY key.ordinality) AS columns "
                        "FROM pg_constraint AS c "
                        "JOIN pg_class AS t ON t.oid = c.conrelid "
                        "JOIN pg_namespace AS n ON n.oid = t.relnamespace "
                        "LEFT JOIN LATERAL unnest(c.conkey) WITH ORDINALITY "
                        "AS key(attnum, ordinality) ON TRUE "
                        "LEFT JOIN pg_attribute AS a ON a.attrelid = t.oid "
                        "AND a.attnum = key.attnum "
                        "WHERE n.nspname = 'archive' "
                        "GROUP BY t.relname, c.conname, c.contype, c.oid"
                    )
                )
            ).mappings().all()
            partitioning = (
                await connection.execute(
                    text(
                        "SELECT c.relkind::text AS relkind, p.partstrat::text AS partstrat, "
                        "pg_get_partkeydef(c.oid) AS partkey "
                        "FROM pg_partitioned_table AS p "
                        "JOIN pg_class AS c ON c.oid = p.partrelid "
                        "JOIN pg_namespace AS n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'archive' AND c.relname = 'legacy_messages'"
                    )
                )
            ).mappings().first()
            partitions = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT child.relname FROM pg_inherits AS inheritance "
                            "JOIN pg_class AS child ON child.oid = inheritance.inhrelid "
                            "JOIN pg_class AS parent ON parent.oid = inheritance.inhparent "
                            "JOIN pg_namespace AS n ON n.oid = parent.relnamespace "
                            "WHERE n.nspname = 'archive' AND parent.relname = 'legacy_messages'"
                        )
                    )
                ).all()
            )
            timeline_count = await connection.scalar(
                text("SELECT COUNT(*) FROM archive.message_timeline_v1")
            )
            view_columns = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'archive' "
                            "AND table_name = 'message_timeline_v1'"
                        )
                    )
                ).all()
            )
            view_columns_v2 = set(
                (
                    await connection.scalars(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'archive' "
                            "AND table_name = 'message_timeline_v2'"
                        )
                    )
                ).all()
            )
            return {
                "version": version,
                "schema_exists": schema_exists,
                "tables": tables,
                "views": views,
                "columns": columns,
                "constraints": constraints,
                "partitioning": partitioning,
                "partitions": partitions,
                "timeline_count": timeline_count,
                "view_columns": view_columns,
                "view_columns_v2": view_columns_v2,
            }
    finally:
        await engine.dispose()


async def _cleanup_postgres_archive_behavior_fixture(connection: Any) -> None:
    public = lambda name: _archive_behavior_public_table(name, postgresql=True)
    archive = _archive_behavior_archive_tables(postgresql=True)
    for table, column, values in (
        (public("responses"), "id", (BEHAVIOR_RESPONSE_ID,)),
        (
            public("event_links"),
            "id",
            (BEHAVIOR_EVENT_LINK_REPLY_ID, BEHAVIOR_EVENT_LINK_MENTION_ID),
        ),
        (
            public("ingress_receipts"),
            "id",
            ("receipt-history-a", "receipt-history-b"),
        ),
        (
            public("event_observations"),
            "id",
            (BEHAVIOR_OBSERVATION_A_ID, BEHAVIOR_OBSERVATION_B_ID),
        ),
        (
            public("source_events"),
            "id",
            (BEHAVIOR_SOURCE_EVENT_A_ID, BEHAVIOR_SOURCE_EVENT_B_ID),
        ),
        (
            public("bot_instances"),
            "id",
            (BEHAVIOR_INSTANCE_A_ID, BEHAVIOR_INSTANCE_B_ID),
        ),
        (archive["source_message_identities"], "source_record_id", (BEHAVIOR_IDENTITY_RECORD_ID, BEHAVIOR_BAD_IDENTITY_RECORD_ID)),
        (archive["legacy_messages"], "id", (BEHAVIOR_LEGACY_ID,)),
        (archive["import_batches"], "id", (BEHAVIOR_BATCH_ID,)),
    ):
        for value in values:
            await connection.execute(
                text(f"DELETE FROM {table} WHERE {column} = :value"),
                {"value": value},
            )


async def _exercise_postgres_archive_behavior(database_url: str) -> None:
    engine = create_async_engine(database_url)
    archive = _archive_behavior_archive_tables(postgresql=True)
    try:
        try:
            async with engine.begin() as connection:
                await _insert_archive_behavior_fixture_postgres(connection)

            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        f"SELECT * FROM {archive['message_timeline_v1']} "
                        "ORDER BY kind, id"
                    )
                )
                rows = [dict(row) for row in result.mappings().all()]
                _assert_archive_behavior_rows(set(result.keys()), rows)
                export_rows = (
                    await connection.execute(
                        text(
                            "SELECT kind, source_record_id, event_type, display_key, "
                            "display_priority, actions_json, reply_target_sender_id, "
                            "reply_target_text FROM archive.message_timeline_v2 "
                            "WHERE source_record_id IN (:legacy_id, :observation_id, :response_id) "
                            "ORDER BY kind, display_priority, id"
                        ),
                        {
                            "legacy_id": "legacy-source-record-001",
                            "observation_id": BEHAVIOR_OBSERVATION_A_ID,
                            "response_id": BEHAVIOR_RESPONSE_ID,
                        },
                    )
                ).mappings().all()
                legacy_export = next(
                    row for row in export_rows if row["kind"] == "legacy_message"
                )
                assert legacy_export["event_type"] == "message"
                core_exports = [
                    row for row in export_rows if row["kind"] == "core_observation"
                ]
                assert len(core_exports) == 2
                assert {row["display_key"] for row in core_exports} == {
                    f"event:{BEHAVIOR_SOURCE_EVENT_A_ID}"
                }
                assert core_exports[0]["display_priority"] < core_exports[1]["display_priority"]
                assert core_exports[0]["reply_target_sender_id"] == "sender-history-a"
                assert core_exports[0]["reply_target_text"] == "observation history a"
                assert all(json.loads(row["actions_json"]) == [] for row in core_exports)
                response_export = next(
                    row for row in export_rows if row["kind"] == "core_response"
                )
                assert response_export["event_type"] == "message.response"
                await connection.rollback()
                await _assert_postgres_identity_composite_fk_rolls_back(
                    connection, archive["source_message_identities"]
                )
        finally:
            async with engine.begin() as connection:
                await _cleanup_postgres_archive_behavior_fixture(connection)
    finally:
        await engine.dispose()


def test_postgres_legacy_history_archive_contract_and_round_trip() -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    assert database_url, "SUPERLILY_TEST_DATABASE_URL must be configured for PostgreSQL coverage"

    parsed = make_url(database_url)
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.database is not None and parsed.database.endswith("_test")
    env = {**os.environ, "SUPERLILY_DATABASE_URL": database_url}

    _run_alembic(env, "downgrade", "base")
    try:
        _run_alembic(env, "upgrade", "head")
        snapshot = asyncio.run(_postgres_archive_snapshot(database_url))
        assert snapshot["version"] == HEAD_REVISION
        assert snapshot["schema_exists"] is True
        assert {
            "import_batches",
            "conversation_mappings",
            "legacy_messages",
            "source_message_identities",
        }.issubset(
            snapshot["tables"]
        )
        assert "message_timeline_v1" in snapshot["views"]
        assert "message_timeline_v2" in snapshot["views"]

        import_columns = snapshot["columns"]["import_batches"]
        assert {
            "id",
            "source_system",
            "cutover_boundary",
            "status",
            "started_at",
        }.issubset(import_columns)
        _assert_has_any(
            import_columns,
            ("source_snapshot_id", "snapshot_id", "source_snapshot_identity"),
            "import batch snapshot identity",
        )
        _assert_has_any(
            import_columns,
            ("source_schema_version", "schema_version"),
            "import batch source schema version",
        )
        _assert_has_any(import_columns, ("mapping_version",), "import batch mapping version")

        mapping_columns = snapshot["columns"]["conversation_mappings"]
        assert {
            "id",
            "source_system",
            "source_conversation_key",
            "platform",
            "conversation_type",
            "conversation_id",
        }.issubset(mapping_columns)

        identity_columns = snapshot["columns"]["source_message_identities"]
        assert {
            "source_system",
            "source_table",
            "source_record_id",
            "import_batch_id",
            "state",
        }.issubset(identity_columns)

        legacy_columns = snapshot["columns"]["legacy_messages"]
        assert {
            "id",
            "source_system",
            "source_table",
            "source_record_id",
            "conversation_type",
            "conversation_id",
            "sender_id",
            "direction",
            "occurred_at",
            "platform_message_id",
            "content_text",
            "parse_warning",
        }.issubset(legacy_columns)
        assert "mapping_version" in legacy_columns
        assert "source_persisted_at" in legacy_columns
        _assert_has_any(
            legacy_columns,
            ("import_batch_id", "batch_id"),
            "legacy message import batch reference",
        )
        _assert_has_any(
            legacy_columns,
            ("segments_json", "segments", "parsed_segments_json"),
            "legacy message parsed segments",
        )
        _assert_has_any(
            legacy_columns,
            ("reply_to_platform_message_id", "reply_hint", "reply_hint_json"),
            "legacy message reply hint",
        )
        _assert_has_any(
            legacy_columns,
            ("raw_json", "raw_fields_json", "raw_reference_json"),
            "legacy message raw field reference",
        )

        constraint_rows = snapshot["constraints"]
        legacy_unique_sets = {
            tuple(row["columns"])
            for row in constraint_rows
            if row["table_name"] == "legacy_messages" and row["contype"] in {"u", "p"}
        }
        assert any(
            {"source_system", "source_table", "source_record_id"}.issubset(columns)
            for columns in legacy_unique_sets
        ), legacy_unique_sets
        identity_unique_sets = {
            tuple(row["columns"])
            for row in constraint_rows
            if row["table_name"] == "source_message_identities"
            and row["contype"] in {"u", "p"}
        }
        assert (
            "source_system",
            "source_table",
            "source_record_id",
        ) in identity_unique_sets, identity_unique_sets
        identity_foreign_key_sets = {
            tuple(row["columns"])
            for row in constraint_rows
            if row["table_name"] == "source_message_identities"
            and row["contype"] == "f"
        }
        assert ("legacy_message_id", "occurred_at") in identity_foreign_key_sets
        assert any(
            row["table_name"] == "legacy_messages" and row["contype"] == "f"
            for row in constraint_rows
        )
        constraint_definitions = "\n".join(
            str(row["definition"]).lower()
            for row in constraint_rows
            if row["table_name"] in {"import_batches", "conversation_mappings", "legacy_messages"}
        )
        assert "lily.nonebot.chatrecorder.v2" in constraint_definitions
        assert "nekro.chat_message" in constraint_definitions
        assert "lily.nonebot.chatrecorder.sqlite.data1" in constraint_definitions
        assert "lily.nonebot.chatrecorder.sqlite.data2" in constraint_definitions
        assert "lily.nonebot.chatrecorder.sqlite.data3" in constraint_definitions

        partitioning = snapshot["partitioning"]
        assert partitioning is not None
        assert partitioning["relkind"] == "p"
        assert partitioning["partstrat"] == "r"
        assert "occurred_at" in str(partitioning["partkey"])
        assert snapshot["partitions"] or partitioning["relkind"] == "p"
        assert {
            "source_system",
            "mapping_version",
            "occurred_at",
            "conversation_id",
            "captured_at",
            "source_persisted_at",
            "received_at",
            "instance_id",
            "event_link_id",
            "relation_type",
            "target_platform_message_id",
            "reply_hint_json",
        }.issubset(
            snapshot["view_columns"]
        )
        assert snapshot["timeline_count"] == 0
        assert {
            "event_type",
            "correlation_fingerprint",
            "display_key",
            "display_priority",
            "actions_json",
            "reply_target_sender_id",
            "reply_target_text",
        }.issubset(snapshot["view_columns_v2"])
        asyncio.run(_exercise_postgres_archive_behavior(database_url))
        after_behavior = asyncio.run(_postgres_archive_snapshot(database_url))
        assert after_behavior["timeline_count"] == 0

        _run_alembic(env, "check")
        _run_alembic(env, "downgrade", PARENT_REVISION)
        after_downgrade = asyncio.run(_postgres_archive_snapshot_without_archive(database_url))
        assert after_downgrade["version"] == PARENT_REVISION
        assert after_downgrade["schema_exists"] is False

        _run_alembic(env, "upgrade", "head")
        restored = asyncio.run(_postgres_archive_snapshot(database_url))
        assert restored["version"] == HEAD_REVISION
        assert restored["schema_exists"] is True
        _run_alembic(env, "check")
    finally:
        _run_alembic(env, "downgrade", "base", check=False)


async def _postgres_archive_snapshot_without_archive(database_url: str) -> dict[str, Any]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return {
                "version": await connection.scalar(text("SELECT version_num FROM alembic_version")),
                "schema_exists": bool(
                    await connection.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'archive')")
                    )
                ),
            }
    finally:
        await engine.dispose()
