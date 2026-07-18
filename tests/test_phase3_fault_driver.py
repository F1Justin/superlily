from argparse import Namespace
import json
import os
from pathlib import Path

import pytest

import superlily_core.phase3_status_fault_driver as fault_driver
from superlily_core.phase3_status_fault_driver import DrillError, _config, _safe_summary


def args(**overrides) -> Namespace:
    values = {
        "provider_stopped_ack": True,
        "run_id": "fault-run-001",
        "expected_plan_id": "status-inspect-fault-test",
        "expected_plan_hash": "a" * 64,
        "wait_seconds": 8.0,
        "descriptor": Path("registry/descriptors/status.inspect/1.0.2.json"),
        "scenario": "invalid-output",
    }
    values.update(overrides)
    return Namespace(**values)


def test_fault_driver_config_requires_stop_ack_before_reading_credentials(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_ADMIN_TOKEN", "admin-private")
    monkeypatch.setenv("SUPERLILY_STATUS_PROVIDER_TOKEN", "provider-private")
    with pytest.raises(DrillError, match="Provider stop"):
        _config(args(provider_stopped_ack=False))
    assert "SUPERLILY_ADMIN_TOKEN" in os.environ
    assert "SUPERLILY_STATUS_PROVIDER_TOKEN" in os.environ


def test_fault_driver_config_pops_credentials_and_hides_them_from_repr(monkeypatch) -> None:
    monkeypatch.setenv("SUPERLILY_PHASE3_CORE_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SUPERLILY_ADMIN_TOKEN", "admin-private")
    monkeypatch.setenv("SUPERLILY_STATUS_PROVIDER_TOKEN", "provider-private")
    config = _config(args())
    assert config.admin_token == "admin-private"
    assert config.provider_token == "provider-private"
    assert "private" not in repr(config)
    assert "SUPERLILY_ADMIN_TOKEN" not in os.environ
    assert "SUPERLILY_STATUS_PROVIDER_TOKEN" not in os.environ


def test_fault_driver_rejects_noncanonical_plan_hash_before_reading_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("SUPERLILY_ADMIN_TOKEN", "admin-private")
    monkeypatch.setenv("SUPERLILY_STATUS_PROVIDER_TOKEN", "provider-private")
    with pytest.raises(DrillError, match="lowercase SHA-256"):
        _config(args(expected_plan_hash="A" * 64))
    assert "SUPERLILY_ADMIN_TOKEN" in os.environ
    assert "SUPERLILY_STATUS_PROVIDER_TOKEN" in os.environ


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8765",
        "http://lily-core:8000",
        "http://user:secret@127.0.0.1:8765",
        "http://127.0.0.1:8765?token=secret",
    ],
)
def test_fault_driver_rejects_non_loopback_or_credential_bearing_url(
    monkeypatch, url: str
) -> None:
    monkeypatch.setenv("SUPERLILY_PHASE3_CORE_URL", url)
    monkeypatch.setenv("SUPERLILY_ADMIN_TOKEN", "admin-private")
    monkeypatch.setenv("SUPERLILY_STATUS_PROVIDER_TOKEN", "provider-private")
    with pytest.raises(DrillError, match="loopback"):
        _config(args())


def test_fault_driver_summary_omits_secrets_and_payload_bodies() -> None:
    view = {
        "invocation_id": "inv-1",
        "state": "succeeded",
        "reason_code": "provider_completed",
        "deadline_at": "2026-07-19T00:00:05+00:00",
        "terminal_at": "2026-07-19T00:00:01+00:00",
        "request": {"input": {"secret": "request-private"}},
        "transitions": [
            {
                "sequence": 1,
                "event": "complete_success",
                "state": "succeeded",
                "reason_code": "provider_completed",
                "evidence": {"lease_secret": "lease-private"},
            }
        ],
        "attempts": [
            {
                "attempt_id": "attempt-1",
                "attempt_number": 1,
                "fencing_token": 1,
                "state": "succeeded",
                "error_code": None,
                "output_hash": "a" * 64,
                "output": {"secret": "output-private"},
            }
        ],
    }
    summary = _safe_summary(
        view,
        scenario="retry-fence-success",
        plan_id="plan-1",
        plan_hash="b" * 64,
    )
    rendered = json.dumps(summary, sort_keys=True)
    assert "private" not in rendered
    assert summary["attempts"][0]["output_hash"] == "a" * 64


def test_fault_driver_redacts_arbitrary_validation_error(monkeypatch, capsys) -> None:
    async def fail_validation(_config) -> dict:
        raise ValueError("provider-private")

    monkeypatch.setattr(fault_driver, "_main", fail_validation)
    monkeypatch.setenv("SUPERLILY_ADMIN_TOKEN", "admin-private")
    monkeypatch.setenv("SUPERLILY_STATUS_PROVIDER_TOKEN", "provider-private")
    outcome = fault_driver.main(
        [
            "invalid-output",
            "--expected-plan-id",
            "status-inspect-fault-test",
            "--expected-plan-hash",
            "a" * 64,
            "--run-id",
            "fault-run-001",
            "--provider-stopped-ack",
        ]
    )
    output = capsys.readouterr().out
    assert outcome == 2
    assert "private" not in output
    assert "local bounded validation failed" in output
