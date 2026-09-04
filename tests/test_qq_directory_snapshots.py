import asyncio
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from superlily_contracts import QQDirectorySnapshotIn, qq_directory_snapshot_hash
from superlily_core.models import (
    ConversationNameObservation,
    IdentityNameObservation,
    QQDirectorySnapshot,
    QQFriendSnapshot,
    QQGroupMemberSnapshot,
)


ROOT = Path(__file__).parents[1]
DIRECTORY_PATHS = [
    ROOT / "bridges/lily_nonebot/lily_core_bridge/directory_snapshots.py",
    ROOT / "bridges/nekro/superlily_bridge/directory_snapshots.py",
]


def load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"directory_{path.parts[-3]}", path)
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


def payload(*, snapshot_id: str = "directory-snapshot-1", nickname: str = "Account A") -> dict:
    group = {
        "group_id": "10001",
        "group_name": "Group A",
        "group_remark": "Pinned A",
        "member_count": 1,
        "max_member_count": 500,
        "whole_group_ban": False,
    }
    members = [
        {
            "user_id": "12345678",
            "nickname": nickname,
            "card": "Card A",
            "role": "admin",
            "title": "Title A",
            "member_level": "42",
            "qq_level": 64,
            "joined_at": "2024-01-01T00:00:00+00:00",
            "last_sent_at": "2026-09-04T00:00:00+00:00",
            "muted_until": None,
            "is_robot": False,
        }
    ]
    source_apis = ["get_group_info_ex", "get_group_member_list"]
    snapshot_hash = qq_directory_snapshot_hash(
        snapshot_kind="group",
        group=group,
        members=members,
        friends=[],
        source_apis=source_apis,
        capture_status="complete",
        reason=None,
    )
    return {
        "schema_version": "1.0",
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "instance": instance(),
        "snapshot_kind": "group",
        "observed_at": "2026-09-04T01:00:00+00:00",
        "source_apis": source_apis,
        "capture_status": "complete",
        "reason": None,
        "group": group,
        "members": members,
        "friends": [],
    }


def test_bridge_directory_implementations_are_identical() -> None:
    assert DIRECTORY_PATHS[0].read_bytes() == DIRECTORY_PATHS[1].read_bytes()


@pytest.mark.parametrize("path", DIRECTORY_PATHS)
async def test_directory_api_timeout_cancels_a_stuck_request(path: Path) -> None:
    module = load_module(path)
    cancelled = asyncio.Event()
    release = asyncio.Event()

    async def stuck_request():
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            module.await_qq_api(stuck_request(), timeout_seconds=0.01),
            timeout=0.1,
        )
    await asyncio.wait_for(cancelled.wait(), timeout=0.1)
    release.set()
    await asyncio.sleep(0)


@pytest.mark.parametrize("path", DIRECTORY_PATHS)
def test_napcat_directory_normalization_keeps_useful_fields_without_contact_secrets(path: Path) -> None:
    module = load_module(path)
    assert module.directory_entry({"group_id": 10001, "group_name": "Group A"}) == {
        "group_id": 10001,
        "group_name": "Group A",
    }
    observed_at = datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc).isoformat()
    friend_payload, _ = module.friend_directory_snapshot(
        instance=instance(),
        raw_categories=[
            {
                "categoryId": 2,
                "categoryName": "Friends",
                "buddyList": [
                    {
                        "user_id": 12345678,
                        "nickname": "Account A",
                        "remark": "Remark A",
                        "phone_num": "18800000000",
                        "email": "private@example.test",
                        "birthday_year": 1990,
                    }
                ],
            }
        ],
        observed_at=observed_at,
        source_apis=["get_friends_with_category"],
    )
    validated = QQDirectorySnapshotIn.model_validate(friend_payload)
    assert validated.friends[0].model_dump() == {
        "user_id": "12345678",
        "nickname": "Account A",
        "remark": "Remark A",
        "category_id": "2",
        "category_name": "Friends",
    }
    assert "phone" not in str(friend_payload)
    assert "email" not in str(friend_payload)
    assert "birthday" not in str(friend_payload)


async def test_group_directory_snapshot_is_queryable_idempotent_and_feeds_name_history(client, app) -> None:
    headers = {
        "Authorization": "Bearer lily-secret",
        "Idempotency-Key": "directory-snapshot-1",
    }
    first = await client.post("/v1/qq-directory/snapshots", json=payload(), headers=headers)
    second = await client.post("/v1/qq-directory/snapshots", json=payload(), headers=headers)
    assert first.status_code == 201, first.text
    assert second.status_code == 200, second.text
    assert second.json()["duplicate"] is True

    async with app.state.database.sessions() as session:
        snapshots = (await session.scalars(select(QQDirectorySnapshot))).all()
        members = (await session.scalars(select(QQGroupMemberSnapshot))).all()
        identity = (await session.scalars(select(IdentityNameObservation))).all()
        conversations = (await session.scalars(select(ConversationNameObservation))).all()
    assert len(snapshots) == 1
    assert snapshots[0].group_remark == "Pinned A"
    assert members[0].role == "admin"
    assert members[0].title == "Title A"
    assert {(row.name_kind, row.name_value) for row in identity} == {
        ("account_name", "Account A"),
        ("conversation_display_name", "Card A"),
    }
    assert [(row.conversation_id, row.name_value) for row in conversations] == [("10001", "Group A")]


async def test_friend_snapshot_and_snapshot_id_content_conflict(client, app) -> None:
    group_response = await client.post(
        "/v1/qq-directory/snapshots",
        json=payload(),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "directory-group"},
    )
    assert group_response.status_code == 201
    conflict = await client.post(
        "/v1/qq-directory/snapshots",
        json=payload(nickname="Account B"),
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "directory-conflict"},
    )
    assert conflict.status_code == 409

    friends = [
        {
            "user_id": "87654321",
            "nickname": "Friend A",
            "remark": "Remark A",
            "category_id": "2",
            "category_name": "Friends",
        }
    ]
    friend_payload = {
        "schema_version": "1.0",
        "snapshot_id": "friend-snapshot-1",
        "instance": instance(),
        "snapshot_kind": "friends",
        "observed_at": "2026-09-04T02:00:00+00:00",
        "source_apis": ["get_friends_with_category"],
        "capture_status": "complete",
        "reason": None,
        "group": None,
        "members": [],
        "friends": friends,
    }
    friend_payload["snapshot_hash"] = qq_directory_snapshot_hash(
        snapshot_kind="friends",
        group=None,
        members=[],
        friends=friends,
        source_apis=friend_payload["source_apis"],
        capture_status="complete",
        reason=None,
    )
    response = await client.post(
        "/v1/qq-directory/snapshots",
        json=friend_payload,
        headers={"Authorization": "Bearer lily-secret", "Idempotency-Key": "directory-friends"},
    )
    assert response.status_code == 201, response.text
    async with app.state.database.sessions() as session:
        rows = (await session.scalars(select(QQFriendSnapshot))).all()
    assert [(row.user_id, row.remark, row.category_name) for row in rows] == [
        ("87654321", "Remark A", "Friends")
    ]
