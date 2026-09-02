"""Read-only exporter for frozen pre-v2 chatrecorder SQLite databases."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator

from .history_import import (
    SQLITE_DATA2_SOURCE_SYSTEM,
    SQLITE_DATA3_SOURCE_SYSTEM,
    SQLITE_DATA_SOURCE_SYSTEM,
    _canonical_source,
    source_profile,
)

_TABLE = "nonebot_plugin_chatrecorder_messagerecord"
_VERSION_TABLE = "nonebot_plugin_chatrecorder_alembic_version"
_SOURCE_ALIASES = ("sqlite-data", "sqlite-data2", "sqlite-data3")
_EXPECTED_REVISIONS = {
    SQLITE_DATA_SOURCE_SYSTEM: "2cad88d938f1",
    SQLITE_DATA2_SOURCE_SYSTEM: "9bca28bcb998",
    SQLITE_DATA3_SOURCE_SYSTEM: "9bca28bcb998",
}
_COMMON_COLUMNS = {
    "id",
    "platform",
    "time",
    "type",
    "detail_type",
    "message_id",
    "message",
    "user_id",
    "group_id",
    "bot_type",
    "bot_id",
    "guild_id",
    "channel_id",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _validate_source(
    connection: sqlite3.Connection,
    *,
    source_system: str,
) -> tuple[tuple[str, ...], str]:
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check is None or quick_check[0] != "ok":
        raise ValueError(f"SQLite quick_check failed: {quick_check!r}")
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?",
        (_TABLE,),
    ).fetchone()
    if table is None:
        raise ValueError(f"required table {_TABLE} is missing")
    columns = tuple(
        str(row[1]) for row in connection.execute(f'PRAGMA table_info("{_TABLE}")')
    )
    missing = sorted(_COMMON_COLUMNS - set(columns))
    if missing:
        raise ValueError("SQLite chatrecorder columns are missing: " + ", ".join(missing))
    text_column = "alt_message" if source_system == SQLITE_DATA_SOURCE_SYSTEM else "plain_text"
    if text_column not in columns:
        raise ValueError(f"required text column {text_column} is missing")
    revision_row = connection.execute(
        f'SELECT version_num FROM "{_VERSION_TABLE}"'
    ).fetchone()
    revision = "" if revision_row is None else str(revision_row[0])
    expected_revision = _EXPECTED_REVISIONS[source_system]
    if revision != expected_revision:
        raise ValueError(
            f"chatrecorder revision must be {expected_revision}, got {revision or 'missing'}"
        )
    return columns, text_column


def _rows(
    connection: sqlite3.Connection,
    *,
    text_column: str,
) -> Iterator[dict[str, Any]]:
    selected = (
        "id, platform, time, type, detail_type, message_id, message, "
        f'"{text_column}" AS plain_text, user_id, group_id, bot_type, bot_id, '
        "guild_id, channel_id"
    )
    cursor = connection.execute(f'SELECT {selected} FROM "{_TABLE}" ORDER BY id')
    while batch := cursor.fetchmany(5000):
        for row in batch:
            yield {key: row[key] for key in row.keys()}


def export_sqlite_snapshot(
    *,
    source: str,
    database: Path,
    output: Path,
    expected_sha256: str,
) -> dict[str, Any]:
    source_system = _canonical_source(source)
    if source_system not in _EXPECTED_REVISIONS:
        raise ValueError("source must identify one of the frozen SQLite chatrecorder databases")
    if not database.is_file():
        raise ValueError(f"SQLite database does not exist: {database}")
    actual_sha256 = _sha256(database)
    if actual_sha256 != expected_sha256.lower():
        raise ValueError(
            f"SQLite SHA-256 mismatch: expected {expected_sha256.lower()}, got {actual_sha256}"
        )
    if output.exists():
        raise ValueError(f"refusing to overwrite existing export: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(output.name + ".partial")
    if partial.exists():
        raise ValueError(f"refusing to overwrite partial export: {partial}")
    profile = source_profile(source_system)
    row_count = 0
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            with closing(_connect_read_only(database)) as connection:
                _, text_column = _validate_source(connection, source_system=source_system)
                for row in _rows(connection, text_column=text_column):
                    handle.write(
                        json.dumps(
                            row,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                    )
                    handle.write("\n")
                    row_count += 1
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(partial, output)
    except Exception:
        try:
            partial.unlink()
        except FileNotFoundError:
            pass
        raise
    return {
        "source_system": source_system,
        "source_table": profile.source_table,
        "source_schema_version": profile.source_schema_version,
        "source_sha256": actual_sha256,
        "rows": row_count,
        "output": str(output),
        "source_writes": 0,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a frozen chatrecorder SQLite DB.")
    parser.add_argument("--source", required=True, choices=_SOURCE_ALIASES)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-sha256", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report = export_sqlite_snapshot(
        source=args.source,
        database=args.database,
        output=args.output,
        expected_sha256=args.expected_sha256,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
