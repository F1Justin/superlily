from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return {}


def _text(value: Any, limit: int = 512) -> str | None:
    if value is None or isinstance(value, (bool, dict, list, tuple, set, bytes, bytearray)):
        return None
    normalized = str(value).strip()
    return normalized[:limit] if normalized else None


def _integer(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= minimum else None


def _timestamp(value: Any) -> str | None:
    seconds = _integer(value, minimum=1)
    if seconds is None:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _state_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _snapshot_payload(
    *,
    instance: dict[str, Any],
    snapshot_kind: str,
    observed_at: str,
    source_apis: list[str],
    capture_status: str,
    reason: str | None,
    group: dict[str, Any] | None,
    members: list[dict[str, Any]],
    friends: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    state = {
        "snapshot_kind": snapshot_kind,
        "group": group,
        "members": sorted(members, key=lambda item: item["user_id"]),
        "friends": sorted(friends, key=lambda item: item["user_id"]),
        "source_apis": sorted(source_apis),
        "capture_status": capture_status,
        "reason": reason,
    }
    snapshot_hash = _state_hash(state)
    snapshot_id = _state_hash(
        {
            "instance_id": instance["instance_id"],
            "observed_at": observed_at,
            "snapshot_hash": snapshot_hash,
        }
    )
    return {
        "schema_version": "1.0",
        "snapshot_id": snapshot_id,
        "snapshot_hash": snapshot_hash,
        "instance": instance,
        "snapshot_kind": snapshot_kind,
        "observed_at": observed_at,
        "source_apis": sorted(source_apis),
        "capture_status": capture_status,
        "reason": reason,
        "group": group,
        "members": state["members"],
        "friends": state["friends"],
    }, snapshot_id


def group_directory_snapshot(
    *,
    instance: dict[str, Any],
    raw_group: Any,
    raw_members: list[Any],
    observed_at: str,
    source_apis: list[str],
    capture_status: str = "complete",
    reason: str | None = None,
) -> tuple[dict[str, Any], str]:
    item = _mapping(raw_group)
    group_id = _text(item.get("group_id") or item.get("groupCode") or item.get("id"), 256)
    if group_id is None:
        raise ValueError("group directory snapshot requires group_id")
    all_shut = item.get("group_all_shut", item.get("groupShutupExpireTime"))
    profile = {
        "group_id": group_id,
        "group_name": _text(item.get("group_name") or item.get("groupName") or item.get("name")),
        "group_remark": _text(item.get("group_remark") or item.get("remarkName")),
        "member_count": _integer(item.get("member_count", item.get("memberCount"))),
        "max_member_count": _integer(item.get("max_member_count", item.get("maxMember"))),
        "whole_group_ban": _boolean(all_shut),
    }
    members_by_id: dict[str, dict[str, Any]] = {}
    for raw_member in raw_members:
        member = _mapping(raw_member)
        user_id = _text(member.get("user_id") or member.get("uin"), 256)
        if user_id is None:
            continue
        raw_role = _text(member.get("role"), 16) or "unknown"
        role = raw_role if raw_role in {"owner", "admin", "member"} else "unknown"
        members_by_id[user_id] = {
            "user_id": user_id,
            "nickname": _text(member.get("nickname") or member.get("nick")),
            "card": _text(member.get("card") or member.get("cardName")),
            "role": role,
            "title": _text(member.get("title") or member.get("memberSpecialTitle")),
            "member_level": _text(member.get("level") or member.get("memberRealLevel")),
            "qq_level": _integer(member.get("qq_level", member.get("qqLevel"))),
            "joined_at": _timestamp(member.get("join_time", member.get("joinTime"))),
            "last_sent_at": _timestamp(member.get("last_sent_time", member.get("lastSpeakTime"))),
            "muted_until": _timestamp(member.get("shut_up_timestamp", member.get("shutUpTime"))),
            "is_robot": _boolean(member.get("is_robot", member.get("isRobot"))),
        }
    return _snapshot_payload(
        instance=instance,
        snapshot_kind="group",
        observed_at=observed_at,
        source_apis=source_apis,
        capture_status=capture_status,
        reason=reason,
        group=profile,
        members=list(members_by_id.values()),
        friends=[],
    )


def friend_directory_snapshot(
    *,
    instance: dict[str, Any],
    raw_categories: list[Any],
    observed_at: str,
    source_apis: list[str],
    capture_status: str = "complete",
    reason: str | None = None,
) -> tuple[dict[str, Any], str]:
    friends_by_id: dict[str, dict[str, Any]] = {}
    for raw_category in raw_categories:
        category = _mapping(raw_category)
        raw_friends = (
            category.get("buddyList")
            or category.get("buddy_list")
            or category.get("friends")
            or []
        )
        if not isinstance(raw_friends, list):
            continue
        category_id = _text(category.get("categoryId") or category.get("category_id") or category.get("id"), 256)
        category_name = _text(category.get("categoryName") or category.get("category_name") or category.get("name"))
        for raw_friend in raw_friends:
            friend = _mapping(raw_friend)
            user_id = _text(friend.get("user_id") or friend.get("uin"), 256)
            if user_id is None:
                continue
            friends_by_id[user_id] = {
                "user_id": user_id,
                "nickname": _text(friend.get("nickname") or friend.get("nick")),
                "remark": _text(friend.get("remark")),
                "category_id": category_id or _text(friend.get("category_id"), 256),
                "category_name": category_name,
            }
    return _snapshot_payload(
        instance=instance,
        snapshot_kind="friends",
        observed_at=observed_at,
        source_apis=source_apis,
        capture_status=capture_status,
        reason=reason,
        group=None,
        members=[],
        friends=list(friends_by_id.values()),
    )
