import hashlib
import json
from datetime import datetime
from typing import Any


def _normalized(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.endswith("+00:00"):
        return f"{value[:-6]}Z"
    if isinstance(value, dict):
        return {key: _normalized(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def qq_directory_snapshot_hash(
    *,
    snapshot_kind: str,
    group: dict[str, Any] | None,
    members: list[dict[str, Any]],
    friends: list[dict[str, Any]],
    source_apis: list[str],
    capture_status: str,
    reason: str | None,
) -> str:
    value = {
        "snapshot_kind": snapshot_kind,
        "group": group,
        "members": sorted(members, key=lambda item: str(item.get("user_id", ""))),
        "friends": sorted(friends, key=lambda item: str(item.get("user_id", ""))),
        "source_apis": sorted(source_apis),
        "capture_status": capture_status,
        "reason": reason,
    }
    encoded = json.dumps(
        _normalized(value),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
