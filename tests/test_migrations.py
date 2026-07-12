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


def test_sqlite_alembic_upgrade_reaches_head_with_partial_claim_owner_index(
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

    assert version == ("0010_claim_owner_index",)
    assert index_sql is not None
    normalized_sql = " ".join(index_sql[0].lower().split())
    assert "unique index" in normalized_sql
    assert "where enforced = 1 and action = 'allow'" in normalized_sql

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
