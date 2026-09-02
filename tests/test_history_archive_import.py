from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import superlily_core.history_archive_import as archive_import
from superlily_core.history_archive_import import (
    ImportSelection,
    apply_archive_import,
    build_archive_record,
)
from superlily_core.history_import import dry_run_legacy_rows

LILY_CUTOVER = "2026-06-19T11:45:17.171050+00:00"
NEKRO_CUTOVER = "2026-06-19T11:49:44.696404+00:00"


def _lily_row(record_id: str, *, time: str = "2026-06-01 10:00:00") -> dict:
    return {
        "id": record_id,
        "session_persist_id": "session-1",
        "time": time,
        "type": "message",
        "message_id": f"message-{record_id}",
        "message": [
            {"type": "reply", "data": {"id": "quoted-1"}},
            {"type": "text", "data": {"text": "hello"}},
        ],
        "plain_text": "hello",
        "scene_id": "706356075",
        "scene_type": 1,
        "bot_id": "985393579",
        "sender_id": "123456",
        "sender_name": "Tester",
    }


def _nekro_row(record_id: str) -> dict:
    return {
        "id": record_id,
        "sender_id": "123456",
        "sender_name": "Tester",
        "sender_nickname": None,
        "adapter_key": "onebot_v11",
        "message_id": f"message-{record_id}",
        "chat_key": "onebot_v11-private_123456",
        "chat_type": "ChatType.PRIVATE",
        "platform_userid": "123456",
        "content_text": "hello\x00world",
        "content_data": json.dumps([{"type": "text", "text": "hello\x00world"}]),
        "raw_cq_code": None,
        "ext_data": json.dumps(
            {"ref_chat_key": "", "ref_msg_id": "quoted-2", "ref_sender_id": "42"}
        ),
        "send_timestamp": "1780000000",
        "create_time": "2026-05-28T20:26:40.500000+00:00",
        "update_time": "2026-05-28T20:26:41+00:00",
        "is_tome": 0,
    }


def _sqlite_row(record_id: str) -> dict:
    return {
        "id": record_id,
        "platform": "qq",
        "time": "2023-07-17 10:04:36.326594",
        "type": "message",
        "detail_type": "group",
        "message_id": f"sqlite-message-{record_id}",
        "message": [
            {"type": "reply", "data": {"id": "sqlite-quoted-1"}},
            {"type": "text", "data": {"text": "hello"}},
        ],
        "plain_text": "hello",
        "user_id": "123456",
        "group_id": "1080353942",
        "bot_type": "OneBot V11",
        "bot_id": "985393579",
        "guild_id": None,
        "channel_id": None,
    }


def _write_snapshot(path: Path, rows: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _manifest(source: str, rows: list[dict], snapshot_id: str) -> dict:
    if source == "lily":
        return dry_run_legacy_rows(
            source,
            rows,
            LILY_CUTOVER,
            source_snapshot_id=snapshot_id,
            source_schema_version="chatrecorder-v2",
            mapping_version="history-map-v1",
        )
    if source == "nekro":
        return dry_run_legacy_rows(
            source,
            rows,
            NEKRO_CUTOVER,
            source_snapshot_id=snapshot_id,
            source_schema_version="nekro-chat-message-v1",
            mapping_version="history-map-v1",
        )
    return dry_run_legacy_rows(
        source,
        rows,
        "2024-08-28T13:25:30+00:00",
        source_snapshot_id=snapshot_id,
        source_schema_version="chatrecorder-sqlite-9bca28bcb998",
        mapping_version="history-map-v1",
    )


def test_build_lily_archive_record_preserves_reply_scope_and_provenance() -> None:
    record = build_archive_record(
        "lily", _lily_row("1"), source_snapshot_id="lily-fixture"
    )

    assert record.conversation_type == "group"
    assert record.conversation_id == "706356075"
    assert record.source_conversation_key == "session-1"
    assert record.sender_id == "123456"
    assert json.loads(record.reply_hint_json) == {
        "scope": "source_conversation_and_bot",
        "target_platform_message_id": "quoted-1",
    }
    assert record.raw_storage_ref.endswith("/nonebot_plugin_chatrecorder_messagerecord_v2/1")


def test_build_nekro_archive_record_uses_platform_time_and_sanitizes_nul() -> None:
    record = build_archive_record(
        "nekro", _nekro_row("2"), source_snapshot_id="nekro-fixture"
    )

    assert record.conversation_type == "private"
    assert record.conversation_id == "123456"
    assert record.occurred_at == datetime.fromtimestamp(1780000000, tz=timezone.utc)
    assert record.source_persisted_at == datetime.fromisoformat(
        "2026-05-28T20:26:40.500000+00:00"
    )
    assert "\x00" not in record.content_text
    assert "nul_replaced" in str(record.parse_warning)
    assert json.loads(record.reply_hint_json)["target_platform_message_id"] == "quoted-2"


def test_build_sqlite_archive_record_preserves_direct_conversation_and_reply() -> None:
    record = build_archive_record(
        "sqlite-data2",
        _sqlite_row("2"),
        source_snapshot_id="sqlite-data2-fixture",
    )

    assert record.source_system == "lily.nonebot.chatrecorder.sqlite.data2"
    assert record.source_table == "nonebot_plugin_chatrecorder_messagerecord"
    assert record.conversation_type == "group"
    assert record.conversation_id == "1080353942"
    assert record.source_conversation_key == (
        "onebot_v11:985393579:group:1080353942"
    )
    assert record.occurred_at == datetime.fromisoformat(
        "2023-07-17T10:04:36.326594+00:00"
    )
    assert json.loads(record.reply_hint_json) == {
        "scope": "source_conversation_and_bot",
        "target_platform_message_id": "sqlite-quoted-1",
    }


def test_import_selection_contract() -> None:
    item = {
        "occurred_at": datetime(2026, 6, 1, tzinfo=timezone.utc),
        "source_conversation_key": "session-1",
    }
    assert ImportSelection("full").includes(item, 999)
    assert ImportSelection("month", month="2026-06").includes(item, 0)
    assert not ImportSelection("month", month="2026-05").includes(item, 0)
    assert ImportSelection(
        "sample", conversation_key="session-1", max_rows=2
    ).includes(item, 1)
    assert not ImportSelection(
        "sample", conversation_key="session-1", max_rows=2
    ).includes(item, 2)


def test_postgres_archive_writer_is_checkpointed_and_idempotent(tmp_path: Path) -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPERLILY_TEST_DATABASE_URL is not configured")
    fixture_id = str(uuid4())
    rows = [
        _lily_row(f"archive-writer-1-{fixture_id}"),
        _lily_row(f"archive-writer-2-{fixture_id}"),
    ]
    snapshot_id = f"lily-writer-fixture-{fixture_id}"
    manifest = _manifest("lily", rows, snapshot_id)
    jsonl_path = tmp_path / "lily.jsonl"
    _write_snapshot(jsonl_path, rows)

    first = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("full"),
            chunk_size=1,
        )
    )
    second = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("full"),
            chunk_size=1,
        )
    )

    assert first["inserted"] == 2
    assert first["existing"] == 0
    assert second["inserted"] == 0
    assert second["writes"] == 0

    async def verify() -> tuple[int, int, str]:
        from superlily_core.history_archive_import import _asyncpg_dsn

        conn = await asyncpg.connect(_asyncpg_dsn(database_url))
        try:
            messages = await conn.fetchval(
                "SELECT count(*) FROM archive.legacy_messages WHERE import_batch_id=$1",
                first["batch_id"],
            )
            identities = await conn.fetchval(
                "SELECT count(*) FROM archive.source_message_identities WHERE import_batch_id=$1",
                first["batch_id"],
            )
            status = await conn.fetchval(
                "SELECT status FROM archive.import_batches WHERE id=$1", first["batch_id"]
            )
            return int(messages), int(identities), str(status)
        finally:
            await conn.close()

    assert asyncio.run(verify()) == (2, 2, "completed")


def test_postgres_archive_writer_accepts_sqlite_source_and_old_partition(
    tmp_path: Path,
) -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPERLILY_TEST_DATABASE_URL is not configured")
    fixture_id = str(uuid4())
    row = _sqlite_row(f"sqlite-writer-{fixture_id}")
    row["group_id"] = f"fixture-group-{fixture_id}"
    snapshot_id = f"sqlite-writer-fixture-{fixture_id}"
    manifest = _manifest("sqlite-data2", [row], snapshot_id)
    jsonl_path = tmp_path / "sqlite-data2.jsonl"
    _write_snapshot(jsonl_path, [row])

    report = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="sqlite-data2",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("full"),
            chunk_size=1,
        )
    )

    async def verify() -> tuple[str, str]:
        conn = await asyncpg.connect(archive_import._asyncpg_dsn(database_url))
        try:
            row = await conn.fetchrow(
                """
                SELECT source_system, tableoid::regclass::text AS partition
                FROM archive.legacy_messages
                WHERE import_batch_id=$1
                """,
                report["batch_id"],
            )
            return str(row["source_system"]), str(row["partition"])
        finally:
            await conn.close()

    async def cleanup() -> None:
        conn = await asyncpg.connect(archive_import._asyncpg_dsn(database_url))
        try:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM archive.source_message_identities WHERE import_batch_id=$1",
                    report["batch_id"],
                )
                await conn.execute(
                    "DELETE FROM archive.legacy_messages WHERE import_batch_id=$1",
                    report["batch_id"],
                )
                await conn.execute(
                    "DELETE FROM archive.conversation_mappings "
                    "WHERE source_system=$1 AND source_conversation_key=$2",
                    "lily.nonebot.chatrecorder.sqlite.data2",
                    f"onebot_v11:985393579:group:fixture-group-{fixture_id}",
                )
                await conn.execute(
                    "DELETE FROM archive.import_batches WHERE id=$1",
                    report["batch_id"],
                )
        finally:
            await conn.close()

    try:
        assert report["inserted"] == 1
        assert asyncio.run(verify()) == (
            "lily.nonebot.chatrecorder.sqlite.data2",
            "archive.legacy_messages_2023_07",
        )
    finally:
        asyncio.run(cleanup())


def test_postgres_archive_writer_progresses_sample_month_full(tmp_path: Path) -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPERLILY_TEST_DATABASE_URL is not configured")
    fixture_id = str(uuid4())
    first_row = _lily_row(
        f"progressive-1-{fixture_id}", time="2026-05-31 23:59:59"
    )
    second_row = _lily_row(
        f"progressive-2-{fixture_id}", time="2026-06-01 00:00:00"
    )
    second_row["session_persist_id"] = "session-2"
    rows = [first_row, second_row]
    snapshot_id = f"lily-progressive-fixture-{fixture_id}"
    manifest = _manifest("lily", rows, snapshot_id)
    jsonl_path = tmp_path / "lily-progressive.jsonl"
    _write_snapshot(jsonl_path, rows)

    sample = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection(
                "sample", conversation_key="session-1", max_rows=1
            ),
            chunk_size=1,
        )
    )
    month = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("month", month="2026-06"),
            chunk_size=1,
        )
    )
    full = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("full"),
            chunk_size=1,
        )
    )

    assert sample["inserted"] == 1
    assert month["inserted"] == 1
    assert full["inserted"] == 0
    assert full["existing"] == 2


def test_postgres_archive_writer_resumes_after_checkpointed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPERLILY_TEST_DATABASE_URL is not configured")
    fixture_id = str(uuid4())
    rows = [
        _lily_row(f"resume-{index}-{fixture_id}")
        for index in range(1, 4)
    ]
    manifest = _manifest("lily", rows, f"lily-resume-fixture-{fixture_id}")
    jsonl_path = tmp_path / "lily-resume.jsonl"
    _write_snapshot(jsonl_path, rows)
    original_write_chunk = archive_import._write_chunk
    calls = 0

    async def fail_second_chunk(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("fixture interruption")
        return await original_write_chunk(*args, **kwargs)

    monkeypatch.setattr(archive_import, "_write_chunk", fail_second_chunk)
    with pytest.raises(RuntimeError, match="fixture interruption"):
        asyncio.run(
            apply_archive_import(
                database_url=database_url,
                source="lily",
                jsonl_path=jsonl_path,
                manifest=manifest,
                selection=ImportSelection("full"),
                chunk_size=1,
            )
        )
    monkeypatch.setattr(archive_import, "_write_chunk", original_write_chunk)

    resumed = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source="lily",
            jsonl_path=jsonl_path,
            manifest=manifest,
            selection=ImportSelection("full"),
            chunk_size=1,
        )
    )

    assert resumed["selected"] == 3
    assert resumed["inserted"] == 3
    assert resumed["existing"] == 0

    async def verify() -> tuple[int, int, str]:
        conn = await asyncpg.connect(archive_import._asyncpg_dsn(database_url))
        try:
            batch_id = resumed["batch_id"]
            messages = await conn.fetchval(
                "SELECT count(*) FROM archive.legacy_messages WHERE import_batch_id=$1",
                batch_id,
            )
            imported_count, status = await conn.fetchrow(
                "SELECT imported_count, status FROM archive.import_batches WHERE id=$1",
                batch_id,
            )
            return int(messages), int(imported_count), str(status)
        finally:
            await conn.close()

    assert asyncio.run(verify()) == (3, 3, "completed")
