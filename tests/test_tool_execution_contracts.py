from datetime import datetime, timezone
import logging
import math

from pydantic import ValidationError
import pytest

from superlily_core.app import _EmptyLeaseAccessFilter
from superlily_contracts import (
    ToolExecutionCompleteIn,
    ToolExecutionFailIn,
    ToolExecutionHeartbeatIn,
    ToolExecutionStartIn,
    ToolLeaseOut,
    ToolLeaseRequestIn,
    ToolUsage,
    lease_secret_hash,
    canonicalize_json_value,
)


SECRET = "s" * 43


def test_access_log_filter_only_hides_successful_empty_lease_polls() -> None:
    access_filter = _EmptyLeaseAccessFilter()

    def record(message: str) -> logging.LogRecord:
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            message,
            (),
            None,
        )

    assert not access_filter.filter(
        record('client - "POST /v1/tool-executions/lease HTTP/1.1" 204')
    )
    assert access_filter.filter(
        record('client - "POST /v1/tool-executions/lease HTTP/1.1" 200')
    )
    assert access_filter.filter(record('client - "POST /v1/events HTTP/1.1" 204'))


def proof_payload() -> dict:
    return {
        "schema_version": "1.0",
        "attempt_id": "attempt-123",
        "fencing_token": 7,
        "lease_secret": SECRET,
    }


def test_execution_contracts_are_strict_and_bounded() -> None:
    assert ToolLeaseRequestIn(inventory_hash="a" * 64).schema_version == "1.0"
    assert ToolExecutionStartIn.model_validate(proof_payload()).fencing_token == 7
    heartbeat = ToolExecutionHeartbeatIn.model_validate(
        {
            **proof_payload(),
            "usage": {"wall_time_ms": 12},
            "provider_observed_at": "2026-07-19T00:00:00+00:00",
        }
    )
    assert heartbeat.provider_observed_at == datetime(2026, 7, 19, tzinfo=timezone.utc)
    assert heartbeat.usage.wall_time_ms == 12
    assert heartbeat.usage.output_bytes == 0


@pytest.mark.parametrize(
    "change",
    [
        {"lease_secret": "short"},
        {"lease_secret": "x" * 32 + "/"},
        {"fencing_token": 0},
        {"provider_id": "self-reported-provider"},
    ],
)
def test_execution_proof_rejects_weak_or_extra_authority(change: dict) -> None:
    with pytest.raises(ValidationError):
        ToolExecutionStartIn.model_validate({**proof_payload(), **change})


def test_complete_and_fail_bound_output_and_safe_error() -> None:
    complete = ToolExecutionCompleteIn.model_validate(
        {
            **proof_payload(),
            "provider_result_id": "result-1",
            "output": {"status": "ok"},
            "usage": ToolUsage(output_bytes=15).model_dump(),
        }
    )
    assert complete.output == {"status": "ok"}
    with pytest.raises(ValidationError):
        ToolExecutionCompleteIn.model_validate(
            {
                **proof_payload(),
                "provider_result_id": "result-2",
                "output": {"value": math.nan},
                "usage": ToolUsage().model_dump(),
            }
        )
    with pytest.raises(ValidationError):
        ToolExecutionFailIn.model_validate(
            {
                **proof_payload(),
                "provider_result_id": "result-3",
                "error_code": "made_up",
                "safe_detail": "bad",
            }
        )
    with pytest.raises(ValidationError):
        ToolExecutionFailIn.model_validate(
            {
                **proof_payload(),
                "provider_result_id": "result-4",
                "error_code": "execution_failed",
                "safe_detail": " path leaked ",
            }
        )
    with pytest.raises(ValidationError):
        ToolExecutionFailIn.model_validate(
            {
                **proof_payload(),
                "provider_result_id": "result-5",
                "error_code": "execution_failed",
                "safe_detail": "first line\nsecond line",
            }
        )


def test_lease_response_contains_exact_execution_boundary() -> None:
    input_value = {"scope": "provider_runtime"}
    deadline = datetime.now(timezone.utc)
    lease = ToolLeaseOut(
        invocation_id="invocation-1",
        attempt_id="attempt-1",
        attempt_number=1,
        fencing_token=1,
        lease_secret=SECRET,
        provider_id="provider-status-primary",
        inventory_hash="a" * 64,
        implementation_hash="b" * 64,
        tool_id="status.inspect",
        descriptor_version="1.0.0",
        descriptor_hash="c" * 64,
        input=input_value,
        input_hash=canonicalize_json_value(input_value).sha256,
        deadline_at=deadline,
        lease_expires_at=deadline,
        resource_budget={"output_bytes": 32_768},
        execution_permissions={
            "network": "deny",
            "filesystem": "deny",
            "subprocess": "deny",
            "secrets": [],
            "remote_fetch": "deny",
            "artifacts": [],
        },
    )
    assert lease.provider_id == "provider-status-primary"
    assert lease_secret_hash(SECRET) == lease_secret_hash(SECRET)
    assert lease_secret_hash(SECRET) != lease_secret_hash("t" * 43)


def test_lease_rejects_mismatched_input_hash_and_inverted_deadline() -> None:
    input_value = {"scope": "provider_runtime"}
    payload = {
        "invocation_id": "invocation-1",
        "attempt_id": "attempt-1",
        "attempt_number": 1,
        "fencing_token": 1,
        "lease_secret": SECRET,
        "provider_id": "provider-status-primary",
        "inventory_hash": "a" * 64,
        "implementation_hash": "b" * 64,
        "tool_id": "status.inspect",
        "descriptor_version": "1.0.0",
        "descriptor_hash": "c" * 64,
        "input": input_value,
        "input_hash": canonicalize_json_value(input_value).sha256,
        "deadline_at": datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc),
        "lease_expires_at": datetime(2026, 7, 18, 23, 59, tzinfo=timezone.utc),
        "resource_budget": {"output_bytes": 32_768},
        "execution_permissions": {
            "network": "deny",
            "filesystem": "deny",
            "subprocess": "deny",
            "secrets": [],
            "remote_fetch": "deny",
            "artifacts": [],
        },
    }
    with pytest.raises(ValidationError, match="input_hash"):
        ToolLeaseOut.model_validate({**payload, "input_hash": "d" * 64})
    with pytest.raises(ValidationError, match="lease expiry"):
        ToolLeaseOut.model_validate(
            {
                **payload,
                "lease_expires_at": datetime(2026, 7, 19, 0, 1, tzinfo=timezone.utc),
            }
        )
