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


def test_sqlite_alembic_upgrade_reaches_attempt_lease_head_and_round_trips(
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
        observation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(event_observations)").fetchall()
        }
        collection_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('conversation_capture_profiles', 'platform_action_observations', "
                "'ingress_receipts', 'collector_watermarks')"
            ).fetchall()
        }
        invocation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_invocations)").fetchall()
        }
        transition_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'tool_invocation_transitions'"
            ).fetchall()
        }
        attempt_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_attempts)").fetchall()
        }
        attempt_event_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'tool_attempt_events'"
            ).fetchall()
        }

    assert version == ("0015_tool_attempts",)
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
        "tool_invocation_transitions",
        "tool_invocations",
        "tool_attempt_events",
        "tool_attempts",
    }
    assert descriptor_count == (0,)
    assert {
        "request_hash",
        "descriptor_snapshot_json",
        "input_hash",
        "principal_hash",
        "capability_hash",
        "policy_hash",
        "selected_provider_id",
        "execution_mode",
        "state",
        "transition_sequence",
        "deadline_at",
        "terminal_at",
    }.issubset(invocation_columns)
    assert transition_triggers == {
        "tool_invocation_transitions_no_update",
        "tool_invocation_transitions_no_delete",
    }
    assert {
        "attempt_number",
        "provider_id",
        "inventory_hash",
        "implementation_hash",
        "fencing_token",
        "lease_secret_hash",
        "state",
        "lease_expires_at",
        "last_heartbeat_at",
        "budget_hash",
        "permissions_hash",
        "usage_hash",
        "output_hash",
        "event_sequence",
    }.issubset(attempt_columns)
    assert attempt_event_triggers == {
        "tool_attempt_events_no_update",
        "tool_attempt_events_no_delete",
    }
    assert collection_tables == {
        "collector_watermarks",
        "conversation_capture_profiles",
        "ingress_receipts",
        "platform_action_observations",
    }
    assert {
        "capture_profile",
        "capture_policy_version",
        "capture_status",
        "sanitizer_version",
        "collector_sanitizer_version",
        "original_payload_sha256",
        "original_payload_size_bytes",
        "omitted_fields_json",
        "platform_extra_json",
        "capture_reason",
    }.issubset(observation_columns)

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0014_tool_invocations"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tool_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tool_%'"
            ).fetchall()
        }
    assert version == ("0014_tool_invocations",)
    assert "tool_attempts" not in tool_tables
    assert "tool_attempt_events" not in tool_tables
    assert "tool_invocations" in tool_tables

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0013_collection_reliability"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tool_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tool_%'"
            ).fetchall()
        }
        collection_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('conversation_capture_profiles', 'platform_action_observations', "
            "'ingress_receipts', 'collector_watermarks')"
        ).fetchall()
    assert version == ("0013_collection_reliability",)
    assert len(tool_tables) == 8
    assert "tool_invocations" not in tool_tables
    assert "tool_invocation_transitions" not in tool_tables
    assert len(collection_tables) == 4

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0012_tool_registry"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        observation_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(event_observations)").fetchall()
        }
        collection_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
            "('conversation_capture_profiles', 'platform_action_observations', "
            "'ingress_receipts', 'collector_watermarks')"
        ).fetchall()
        tool_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'tool_%'"
        ).fetchall()
    assert version == ("0012_tool_registry",)
    assert collection_tables == []
    assert "capture_profile" not in observation_columns
    assert len(tool_tables) == 8

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
    assert version == ("0015_tool_attempts",)
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
