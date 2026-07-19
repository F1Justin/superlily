from __future__ import annotations

import pytest
from pydantic import ValidationError

from superlily_contracts import ToolInvocationConfirmIn


def confirmation_payload() -> dict:
    return {
        "schema_version": "1.0",
        "confirmation_id": "confirmation-1",
        "request_hash": "a" * 64,
        "input_hash": "b" * 64,
        "principal_hash": "c" * 64,
        "decision": "approve",
        "reason": "用户明确确认执行",
    }


def test_confirmation_contract_binds_all_authority_hashes() -> None:
    confirmation = ToolInvocationConfirmIn.model_validate(confirmation_payload())
    assert confirmation.confirmation_id == "confirmation-1"
    assert confirmation.decision == "approve"


@pytest.mark.parametrize(
    "change",
    [
        {"request_hash": "A" * 64},
        {"input_hash": "b" * 63},
        {"principal_hash": "c" * 64 + "d"},
        {"decision": "maybe"},
        {"reason": " surrounding whitespace "},
        {"reason": "line one\nline two"},
        {"caller": "self-reported"},
    ],
)
def test_confirmation_contract_rejects_ambiguous_or_extra_authority(change: dict) -> None:
    with pytest.raises(ValidationError):
        ToolInvocationConfirmIn.model_validate({**confirmation_payload(), **change})
