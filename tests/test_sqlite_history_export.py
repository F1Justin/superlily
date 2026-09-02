from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from superlily_core.sqlite_history_export import export_sqlite_snapshot


def _fixture_database(path: Path, *, old_schema: bool) -> None:
    text_column = "alt_message" if old_schema else "plain_text"
    revision = "2cad88d938f1" if old_schema else "9bca28bcb998"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE nonebot_plugin_chatrecorder_alembic_version "
            "(version_num VARCHAR(32) PRIMARY KEY)"
        )
        connection.execute(
            "INSERT INTO nonebot_plugin_chatrecorder_alembic_version VALUES (?)",
            (revision,),
        )
        connection.execute(
            f"""
            CREATE TABLE nonebot_plugin_chatrecorder_messagerecord (
                id INTEGER PRIMARY KEY,
                platform VARCHAR NOT NULL,
                time DATETIME NOT NULL,
                type VARCHAR NOT NULL,
                detail_type VARCHAR NOT NULL,
                message_id VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                {text_column} VARCHAR NOT NULL,
                user_id VARCHAR NOT NULL,
                group_id VARCHAR,
                bot_type VARCHAR,
                bot_id VARCHAR,
                guild_id VARCHAR,
                channel_id VARCHAR
            )
            """
        )
        connection.execute(
            "INSERT INTO nonebot_plugin_chatrecorder_messagerecord "
            f"(id, platform, time, type, detail_type, message_id, message, {text_column}, "
            "user_id, group_id, bot_type, bot_id, guild_id, channel_id) "
            "VALUES (1, 'qq', '2023-01-01 10:00:00', 'message', 'group', 'm1', "
            "?, 'hello', '123', '456', ?, ?, NULL, NULL)",
            (
                json.dumps([{"type": "text", "data": {"text": "hello"}}]),
                None if old_schema else "OneBot V11",
                None if old_schema else "985393579",
            ),
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    ("source", "old_schema", "expected_schema"),
    [
        ("sqlite-data", True, "chatrecorder-sqlite-2cad88d938f1"),
        ("sqlite-data2", False, "chatrecorder-sqlite-9bca28bcb998"),
    ],
)
def test_export_sqlite_snapshot_is_read_only_and_normalizes_text_column(
    tmp_path: Path,
    source: str,
    old_schema: bool,
    expected_schema: str,
) -> None:
    database = tmp_path / "source.db"
    output = tmp_path / "snapshot.jsonl"
    _fixture_database(database, old_schema=old_schema)
    before = _sha256(database)

    report = export_sqlite_snapshot(
        source=source,
        database=database,
        output=output,
        expected_sha256=before,
    )

    assert _sha256(database) == before
    assert report["source_writes"] == 0
    assert report["rows"] == 1
    assert report["source_schema_version"] == expected_schema
    row = json.loads(output.read_text(encoding="utf-8"))
    assert row["plain_text"] == "hello"
    assert row["message_id"] == "m1"
    assert output.stat().st_mode & 0o777 == 0o600


def test_export_sqlite_snapshot_rejects_hash_mismatch(tmp_path: Path) -> None:
    database = tmp_path / "source.db"
    _fixture_database(database, old_schema=False)

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        export_sqlite_snapshot(
            source="sqlite-data3",
            database=database,
            output=tmp_path / "snapshot.jsonl",
            expected_sha256="0" * 64,
        )
