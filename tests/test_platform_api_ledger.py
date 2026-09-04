import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import select

from superlily_contracts import EventIn
from superlily_core.models import PlatformAPICallRecord


ROOT = Path(__file__).parents[1]
AUDIT_PATHS = [
    ROOT / "bridges/lily_nonebot/lily_core_bridge/platform_api_audit.py",
    ROOT / "bridges/nekro/superlily_bridge/platform_api_audit.py",
]


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"api_audit_{path.parts[-3]}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def instance() -> dict:
    return {
        "instance_id": "lily-command",
        "platform": "qq",
        "adapter": "onebot_v11",
        "bot_id": "985393579",
        "role": "command",
    }


def test_bridge_api_audit_implementations_are_identical() -> None:
    assert AUDIT_PATHS[0].read_bytes() == AUDIT_PATHS[1].read_bytes()


@pytest.mark.parametrize("path", AUDIT_PATHS)
def test_side_effect_selection_excludes_reads_and_existing_message_send_ledger(path: Path) -> None:
    module = load_module(path)
    assert module.is_audited_side_effect("delete_msg")
    assert module.is_audited_side_effect("set_group_card")
    assert module.is_audited_side_effect("_set_model_show")
    assert module.is_audited_side_effect("upload_group_file")
    assert module.is_audited_side_effect("friend_poke")
    assert module.is_audited_side_effect("_send_group_notice")
    assert module.is_audited_side_effect("send_like")
    assert module.is_audited_side_effect(".handle_quick_operation")
    assert not module.is_audited_side_effect("get_group_member_list")
    assert not module.is_audited_side_effect("get_credentials")
    assert not module.is_audited_side_effect("send_group_msg")


@pytest.mark.parametrize("path", AUDIT_PATHS)
def test_api_audit_keeps_safe_parameters_and_omits_credentials_and_paths(path: Path) -> None:
    module = load_module(path)
    payload, _, _ = module.started_api_call(
        instance=instance(),
        api="upload_group_file",
        data={
            "group_id": 10001,
            "file": "/private/path/report.txt",
            "name": "report.txt",
            "access_token": "secret",
            "url": "https://user:pass@example.test/file?token=secret",
            "cookie": "secret-cookie",
        },
        trigger_source_event_id="qq:source:v2:" + "1" * 64,
        occurred_at="2026-09-04T03:00:00+00:00",
    )
    event = EventIn.model_validate(payload)
    assert event.platform_api_call is not None
    assert event.platform_api_call.safe_parameters == {
        "group_id": 10001,
        "name": "report.txt",
        "file_supplied": True,
        "url_supplied": True,
    }
    serialized = str(event.model_dump(mode="json"))
    assert "/private/path" not in serialized
    assert "secret-cookie" not in serialized
    assert "user:pass" not in serialized


async def test_started_and_completed_events_materialize_one_queryable_call(client, app) -> None:
    module = load_module(AUDIT_PATHS[0])
    started, started_key, context = module.started_api_call(
        instance=instance(),
        api="set_group_card",
        data={"group_id": 10001, "user_id": 12345678, "card": "New Card"},
        trigger_source_event_id=None,
        occurred_at="2026-09-04T03:00:00+00:00",
    )
    completed, completed_key = module.completed_api_call(
        context,
        exception=None,
        result={"retcode": 0},
        duration_ms=125,
        occurred_at="2026-09-04T03:00:00.125000+00:00",
    )
    headers = {"Authorization": "Bearer lily-secret"}
    first = await client.post(
        "/v1/events",
        json=started,
        headers={**headers, "Idempotency-Key": started_key},
    )
    second = await client.post(
        "/v1/events",
        json=completed,
        headers={**headers, "Idempotency-Key": completed_key},
    )
    replay = await client.post(
        "/v1/events",
        json=completed,
        headers={**headers, "Idempotency-Key": completed_key},
    )
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert replay.status_code == 200, replay.text

    async with app.state.database.sessions() as session:
        rows = (await session.scalars(select(PlatformAPICallRecord))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.api_name == "set_group_card"
    assert row.target_conversation_type == "group"
    assert row.target_conversation_id == "10001"
    assert row.safe_parameters_json == {"card": "New Card", "group_id": 10001, "user_id": 12345678}
    assert row.start_observed is True
    assert row.result_observed is True
    assert row.outcome == "succeeded"
    assert row.success is True
    assert row.return_code == 0
    assert row.duration_ms == 125


async def test_timeout_is_ambiguous_and_result_without_start_remains_explicit(client, app) -> None:
    module = load_module(AUDIT_PATHS[0])
    _, _, context = module.started_api_call(
        instance=instance(),
        api="delete_msg",
        data={"message_id": 456},
        trigger_source_event_id=None,
        occurred_at="2026-09-04T04:00:00+00:00",
    )
    completed, key = module.completed_api_call(
        context,
        exception=TimeoutError("request timed out"),
        result=None,
        duration_ms=10_000,
        occurred_at="2026-09-04T04:00:10+00:00",
    )
    response = await client.post(
        "/v1/events",
        json=completed,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    async with app.state.database.sessions() as session:
        row = await session.scalar(select(PlatformAPICallRecord))
    assert row is not None
    assert row.start_observed is False
    assert row.result_observed is True
    assert row.started_at is None
    assert row.outcome == "ambiguous"
    assert row.success is False
    assert row.safe_error_code == "platform_completion_unknown"
