from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from superlily_contracts import ToolRegistryContractError, load_tool_rollout_plan


ROLLOUT_DIRECTORY = Path(__file__).parents[1] / "registry/rollouts"


def _plan(**changes) -> dict:
    now = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    value = {
        "schema_version": "1.0",
        "plan_id": "status-inspect-first-canary",
        "version": "1.0.0",
        "mode": "canary",
        "starts_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "max_invocations": 1,
        "rollback_mode": "ledger_only",
        "reason": "One bounded admin API status canary",
        "items": [
            {
                "item_id": "status-inspect-admin",
                "tool_id": "status.inspect",
                "descriptor_version": "1.0.2",
                "descriptor_hash": "a" * 64,
                "canonical_conversation": "qq:group:1080353942",
                "caller": "admin_api",
                "provider_id": "provider-status-primary",
                "expected_descriptor_resource_version": 2,
                "expected_provider_resource_version": 1,
            }
        ],
    }
    value.update(changes)
    return value


def _source(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def test_rollout_plan_is_strict_and_canonically_content_addressed() -> None:
    first = load_tool_rollout_plan(_source(_plan()))
    reordered = _plan()
    reordered["items"][0] = dict(reversed(list(reordered["items"][0].items())))
    second = load_tool_rollout_plan(
        json.dumps(reordered, ensure_ascii=False, indent=2, sort_keys=True).encode()
    )

    assert first.plan.mode == "canary"
    assert first.plan.rollback_mode == "ledger_only"
    assert first.authority.sha256 == second.authority.sha256
    assert first.authority.canonical_bytes == second.authority.canonical_bytes


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(mode="enforce"),
        lambda value: value.update(rollback_mode="canary"),
        lambda value: value.update(max_invocations=0),
        lambda value: value.update(expires_at="2026-07-21T00:00:00+00:00"),
        lambda value: value.update(starts_at="2026-07-19T00:00:00"),
        lambda value: value.update(extra=True),
        lambda value: value["items"][0].update(tool_id="status.*"),
        lambda value: value["items"][0].update(canonical_conversation="*"),
        lambda value: value["items"][0].update(provider_id="*"),
        lambda value: value["items"][0].update(caller="schedule"),
    ],
)
def test_rollout_plan_rejects_unbounded_or_implicit_authority(mutate) -> None:
    value = _plan()
    mutate(value)
    with pytest.raises(ToolRegistryContractError):
        load_tool_rollout_plan(_source(value))


def test_rollout_plan_rejects_duplicate_execution_target_with_other_provider() -> None:
    value = _plan()
    duplicate = {**value["items"][0], "item_id": "status-inspect-admin-second"}
    duplicate["provider_id"] = "provider-status-secondary"
    value["items"].append(duplicate)

    with pytest.raises(ToolRegistryContractError):
        load_tool_rollout_plan(_source(value))


@pytest.mark.parametrize(
    "path",
    sorted(ROLLOUT_DIRECTORY.glob("status-inspect-*.json")),
    ids=lambda path: path.name,
)
def test_committed_status_rollout_authorities_are_strict_and_single_call(path: Path) -> None:
    loaded = load_tool_rollout_plan(path.read_bytes())
    assert loaded.plan.mode == "canary"
    assert loaded.plan.max_invocations == 1
    assert loaded.plan.rollback_mode == "ledger_only"
    assert len(loaded.plan.items) == 1
    item = loaded.plan.items[0]
    assert item.tool_id == "status.inspect"
    assert item.descriptor_version == "1.0.2"
    assert item.descriptor_hash == (
        "0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd"
    )
    assert item.canonical_conversation == "qq:group:1080353942"
    assert item.caller == "admin_api"
    assert item.provider_id == "provider-status-primary"
