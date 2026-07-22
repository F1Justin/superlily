from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine


def test_revision_identifiers_fit_alembic_version_column() -> None:
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))

    revisions = list(scripts.walk_revisions())
    assert revisions
    assert all(len(revision.revision) <= 32 for revision in revisions)


def test_sqlite_alembic_upgrade_reaches_control_plane_head_and_round_trips(
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
        descriptor_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_descriptors)").fetchall()
        }
        descriptor_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('tool_descriptors', 'tool_descriptor_lifecycle_events')"
            ).fetchall()
        }
        provider_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_providers)").fetchall()
        }
        provider_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('tool_providers', 'tool_provider_lifecycle_events')"
            ).fetchall()
        }
        control_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'control_plane_%'"
            ).fetchall()
        }
        control_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name LIKE 'control_plane_%'"
            ).fetchall()
        }
        rollout_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name LIKE 'tool_rollout_%'"
            ).fetchall()
        }
        confirmation_artifact_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('tool_confirmations', 'tool_confirmation_events', "
                "'tool_artifacts', 'tool_artifact_events')"
            ).fetchall()
        }
        render_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN "
                "('render_documents', 'render_attempts', 'render_artifacts', "
                "'render_delivery_plans', 'render_delivery_intents', "
                "'render_delivery_attempts')"
            ).fetchall()
        }
        render_artifact_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(render_artifacts)").fetchall()
        }
        render_delivery_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(render_delivery_attempts)"
            ).fetchall()
        }
        render_delivery_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name = 'render_delivery_attempts'"
            ).fetchall()
        }

    assert version == ("0018_render_attempt_delivery",)
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
        "tool_rollout_plans",
        "tool_rollout_plan_items",
        "tool_rollout_plan_lifecycle_events",
        "tool_rollout_plan_counters",
        "tool_confirmations",
        "tool_confirmation_events",
        "tool_artifacts",
        "tool_artifact_events",
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
        "rollout_plan_id",
        "rollout_plan_item_id",
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
    assert "resource_version" in descriptor_columns
    assert descriptor_triggers == {
        "tool_descriptors_authority_no_update",
        "tool_descriptors_no_delete",
        "tool_descriptors_lifecycle_guard",
        "tool_descriptor_lifecycle_events_no_update",
        "tool_descriptor_lifecycle_events_no_delete",
    }
    assert "resource_version" in provider_columns
    assert provider_triggers == {
        "tool_providers_authority_no_update",
        "tool_providers_no_delete",
        "tool_providers_lifecycle_guard",
        "tool_provider_lifecycle_events_no_update",
        "tool_provider_lifecycle_events_no_delete",
    }
    assert control_tables == {
        "control_plane_sessions",
        "control_plane_login_attempts",
        "control_plane_mutations",
        "control_plane_audit_events",
        "control_plane_previews",
    }
    assert control_triggers == {
        "control_plane_login_attempts_no_update",
        "control_plane_login_attempts_no_delete",
        "control_plane_mutations_no_update",
        "control_plane_mutations_no_delete",
        "control_plane_audit_events_no_update",
        "control_plane_audit_events_no_delete",
        "control_plane_previews_no_update",
        "control_plane_previews_no_delete",
    }
    assert rollout_triggers == {
        "tool_rollout_plans_authority_no_update",
        "tool_rollout_plans_no_delete",
        "tool_rollout_plans_lifecycle_guard",
        "tool_rollout_plan_items_no_update",
        "tool_rollout_plan_items_no_delete",
        "tool_rollout_plan_lifecycle_events_no_update",
        "tool_rollout_plan_lifecycle_events_no_delete",
        "tool_rollout_plan_counters_update_guard",
        "tool_rollout_plan_counters_no_delete",
    }
    assert confirmation_artifact_triggers == {
        "tool_confirmations_authority_no_update",
        "tool_confirmations_no_delete",
        "tool_confirmations_state_guard",
        "tool_confirmation_events_no_update",
        "tool_confirmation_events_no_delete",
        "tool_artifacts_authority_no_update",
        "tool_artifacts_no_delete",
        "tool_artifacts_state_guard",
        "tool_artifact_events_no_update",
        "tool_artifact_events_no_delete",
    }
    assert render_tables == {
        "render_documents",
        "render_attempts",
        "render_artifacts",
        "render_delivery_plans",
        "render_delivery_intents",
        "render_delivery_attempts",
    }
    assert "attempt_id" in render_artifact_columns
    assert {"plan_id", "intent_id"}.issubset(render_delivery_columns)
    assert render_delivery_triggers == {
        "render_delivery_attempts_no_update",
        "render_delivery_attempts_no_delete",
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
        [sys.executable, "-m", "alembic", "check"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0015b_descriptor_mutations"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        provider_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_providers)").fetchall()
        }
        provider_triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name IN ('tool_providers', 'tool_provider_lifecycle_events')"
        ).fetchall()
        descriptor_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_descriptors)").fetchall()
        }
    assert version == ("0015b_descriptor_mutations",)
    assert "resource_version" not in provider_columns
    assert provider_triggers == []
    assert "resource_version" in descriptor_columns

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0015a_control_plane_auth"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        control_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'control_plane_%'"
            ).fetchall()
        }
        descriptor_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(tool_descriptors)").fetchall()
        }
        descriptor_triggers = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name IN ('tool_descriptors', 'tool_descriptor_lifecycle_events')"
        ).fetchall()
    assert version == ("0015a_control_plane_auth",)
    assert control_tables == {
        "control_plane_sessions",
        "control_plane_login_attempts",
        "control_plane_mutations",
        "control_plane_audit_events",
    }
    assert "resource_version" not in descriptor_columns
    assert descriptor_triggers == []

    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "0015_tool_attempts"],
        cwd=Path(__file__).parents[1],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(database_path) as connection:
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        control_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name LIKE 'control_plane_%'"
        ).fetchall()
        attempt_tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('tool_attempts', 'tool_attempt_events')"
        ).fetchall()
    assert version == ("0015_tool_attempts",)
    assert control_tables == []
    assert {row[0] for row in attempt_tables} == {"tool_attempts", "tool_attempt_events"}

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
    assert version == ("0018_render_attempt_delivery",)
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


def test_postgres_alembic_control_plane_round_trip_and_drift() -> None:
    database_url = os.getenv("SUPERLILY_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("SUPERLILY_TEST_DATABASE_URL is not configured")
    parsed = make_url(database_url)
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.host in {"127.0.0.1", "localhost"}
    assert parsed.database is not None and parsed.database.endswith("_test")
    env = {**os.environ, "SUPERLILY_DATABASE_URL": database_url}
    cwd = Path(__file__).parents[1]

    def alembic(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=cwd,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )

    async def snapshot() -> tuple[
        str | None,
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
        set[str],
    ]:
        engine = create_async_engine(database_url)
        try:
            async with engine.connect() as connection:
                version_table = await connection.scalar(
                    text(
                        "SELECT to_regclass(current_schema() || '.alembic_version') IS NOT NULL"
                    )
                )
                version = None
                if version_table:
                    version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                tables = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT table_name FROM information_schema.tables "
                                "WHERE table_schema = current_schema() "
                                "AND table_name LIKE 'control_plane_%'"
                            )
                        )
                    ).all()
                )
                triggers = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT trigger_name FROM information_schema.triggers "
                                "WHERE event_object_schema = current_schema() "
                                "AND event_object_table LIKE 'control_plane_%'"
                            )
                        )
                    ).all()
                )
                descriptor_triggers = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT trigger_name FROM information_schema.triggers "
                                "WHERE event_object_schema = current_schema() "
                                "AND event_object_table IN "
                                "('tool_descriptors', 'tool_descriptor_lifecycle_events')"
                            )
                        )
                    ).all()
                )
                descriptor_columns = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'tool_descriptors'"
                            )
                        )
                    ).all()
                )
                provider_triggers = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT trigger_name FROM information_schema.triggers "
                                "WHERE event_object_schema = current_schema() "
                                "AND event_object_table IN "
                                "('tool_providers', 'tool_provider_lifecycle_events')"
                            )
                        )
                    ).all()
                )
                provider_columns = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT column_name FROM information_schema.columns "
                                "WHERE table_schema = current_schema() "
                                "AND table_name = 'tool_providers'"
                            )
                        )
                    ).all()
                )
                functions = set(
                    (
                        await connection.scalars(
                            text(
                                "SELECT p.proname FROM pg_proc p "
                                "JOIN pg_namespace n ON n.oid = p.pronamespace "
                                "WHERE n.nspname = current_schema() "
                                "AND p.proname IN ('reject_control_plane_evidence_mutation', "
                                "'reject_descriptor_lifecycle_evidence_mutation', "
                                "'guard_tool_descriptor_authority_mutation', "
                                "'reject_provider_lifecycle_evidence_mutation', "
                                "'guard_tool_provider_authority_mutation', "
                                "'guard_tool_rollout_plan_authority_mutation', "
                                "'reject_rollout_plan_evidence_mutation', "
                                "'guard_tool_rollout_plan_counter_mutation', "
                                "'guard_tool_confirmation_mutation', "
                                "'guard_tool_artifact_mutation', "
                                "'reject_confirmation_artifact_event_mutation')"
                            )
                        )
                    ).all()
                )
                return (
                    version,
                    tables,
                    triggers,
                    descriptor_triggers,
                    descriptor_columns,
                    provider_triggers,
                    provider_columns,
                    functions,
                )
        finally:
            await engine.dispose()

    alembic("downgrade", "base")
    try:
        alembic("upgrade", "head")
        (
            version,
            tables,
            triggers,
            descriptor_triggers,
            descriptor_columns,
            provider_triggers,
            provider_columns,
            functions,
        ) = asyncio.run(snapshot())
        assert version == "0018_render_attempt_delivery"
        assert tables == {
            "control_plane_sessions",
            "control_plane_login_attempts",
            "control_plane_mutations",
            "control_plane_audit_events",
            "control_plane_previews",
        }
        assert triggers == {
            "control_plane_login_attempts_no_mutation",
            "control_plane_mutations_no_mutation",
            "control_plane_audit_events_no_mutation",
            "control_plane_previews_no_mutation",
        }
        assert descriptor_triggers == {
            "tool_descriptors_authority_guard",
            "tool_descriptor_lifecycle_events_no_mutation",
        }
        assert "resource_version" in descriptor_columns
        assert provider_triggers == {
            "tool_providers_authority_guard",
            "tool_provider_lifecycle_events_no_mutation",
        }
        assert "resource_version" in provider_columns
        assert functions == {
            "reject_control_plane_evidence_mutation",
            "reject_descriptor_lifecycle_evidence_mutation",
            "guard_tool_descriptor_authority_mutation",
            "reject_provider_lifecycle_evidence_mutation",
            "guard_tool_provider_authority_mutation",
            "guard_tool_rollout_plan_authority_mutation",
            "reject_rollout_plan_evidence_mutation",
            "guard_tool_rollout_plan_counter_mutation",
            "guard_tool_confirmation_mutation",
            "guard_tool_artifact_mutation",
            "reject_confirmation_artifact_event_mutation",
        }
        alembic("check")

        alembic("downgrade", "0015b_descriptor_mutations")
        (
            version,
            tables,
            triggers,
            descriptor_triggers,
            descriptor_columns,
            provider_triggers,
            provider_columns,
            functions,
        ) = asyncio.run(snapshot())
        assert version == "0015b_descriptor_mutations"
        assert provider_triggers == set()
        assert "resource_version" not in provider_columns
        assert "resource_version" in descriptor_columns
        assert functions == {
            "reject_control_plane_evidence_mutation",
            "reject_descriptor_lifecycle_evidence_mutation",
            "guard_tool_descriptor_authority_mutation",
        }

        alembic("downgrade", "0015a_control_plane_auth")
        (
            version,
            tables,
            triggers,
            descriptor_triggers,
            descriptor_columns,
            provider_triggers,
            provider_columns,
            functions,
        ) = asyncio.run(snapshot())
        assert version == "0015a_control_plane_auth"
        assert tables == {
            "control_plane_sessions",
            "control_plane_login_attempts",
            "control_plane_mutations",
            "control_plane_audit_events",
        }
        assert triggers == {
            "control_plane_login_attempts_no_mutation",
            "control_plane_mutations_no_mutation",
            "control_plane_audit_events_no_mutation",
        }
        assert descriptor_triggers == set()
        assert "resource_version" not in descriptor_columns
        assert provider_triggers == set()
        assert "resource_version" not in provider_columns
        assert functions == {"reject_control_plane_evidence_mutation"}

        alembic("downgrade", "0015_tool_attempts")
        (
            version,
            tables,
            triggers,
            descriptor_triggers,
            descriptor_columns,
            provider_triggers,
            provider_columns,
            functions,
        ) = asyncio.run(snapshot())
        assert version == "0015_tool_attempts"
        assert tables == set()
        assert triggers == set()
        assert descriptor_triggers == set()
        assert "resource_version" not in descriptor_columns
        assert provider_triggers == set()
        assert "resource_version" not in provider_columns
        assert functions == set()

        alembic("upgrade", "head")
        assert asyncio.run(snapshot())[0] == "0018_render_attempt_delivery"
    finally:
        alembic("downgrade", "base", check=False)
