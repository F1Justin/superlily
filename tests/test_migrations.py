from __future__ import annotations

import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory


def test_revision_identifiers_fit_alembic_version_column() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    revisions = list(scripts.walk_revisions())
    assert revisions
    assert all(len(revision.revision) <= 32 for revision in revisions)


def test_sqlite_alembic_upgrade_reaches_tool_registry_head_and_round_trips(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.sqlite"
    env = {
        **os.environ,
        "SUPERLILY_DATABASE_URL": f"sqlite+aiosqlite:///{database_path}",
    }

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("uq_event_claim_enforced_allow_owner",),
        ).fetchone()
        claim_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(event_claims)").fetchall()
        }
        tool_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tool_%'"
            ).fetchall()
        }
        descriptor_count = connection.execute("SELECT COUNT(*) FROM tool_descriptors").fetchone()

    assert version == ("0012_tool_registry",)
    assert index_sql is not None
    assert "acknowledged_at" in claim_columns
    normalized_sql = " ".join(index_sql[0].lower().split())
    assert "unique index" in normalized_sql
    assert "where enforced = 1 and action = 'allow'" in normalized_sql
    assert tool_tables == {
        "tool_descriptor_lifecycle_events",
        "tool_descriptors",
        "tool_provider_credentials",
        "tool_provider_heartbeats",
        "tool_provider_inventory_entries",
        "tool_provider_inventory_snapshots",
        "tool_provider_lifecycle_events",
        "tool_providers",
    }
    assert descriptor_count == (0,)
    assert not any("invocation" in table or "attempt" in table or "lease" in table for table in tool_tables)

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0011_claim_ack"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tool_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tool_%'"
        ).fetchall()
    assert version == ("0011_claim_ack",)
    assert tool_tables == []

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        descriptor_count = connection.execute("SELECT COUNT(*) FROM tool_descriptors").fetchone()
    assert version == ("0012_tool_registry",)
    assert descriptor_count == (0,)

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "base"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        application_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name != 'alembic_version'"
        ).fetchall()
    assert application_tables == []
