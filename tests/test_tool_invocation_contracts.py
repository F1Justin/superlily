from __future__ import annotations

from itertools import product
from typing import get_args

import pytest
from pydantic import ValidationError

from superlily_contracts import (
    TERMINAL_INVOCATION_STATES,
    InvocationState,
    InvocationTransitionEvent,
    ToolInvocationCreateIn,
    invocation_request_hash,
    legal_invocation_transitions,
    validate_invocation_transition,
)


def invocation_payload() -> dict:
    return {
        "schema_version": "1.0",
        "tool_id": "status.inspect",
        "descriptor_version": "1.0.0",
        "descriptor_hash": "a" * 64,
        "input": {"scope": "provider_runtime"},
        "principal": {
            "platform": "qq",
            "sender_id": "123456",
            "conversation_id": "group:1080353942",
            "conversation_type": "group",
            "platform_roles": ["member"],
            "source_event_id": "qq:message:123",
            "entry_id": "status-command-123",
        },
        "capabilities": [],
    }


def test_complete_invocation_transition_matrix_is_explicit() -> None:
    states = get_args(InvocationState)
    events = get_args(InvocationTransitionEvent)
    legal = legal_invocation_transitions()

    assert set(legal) == set(events)
    for event, previous_state, state in product(events, (None, *states), states):
        expected = (previous_state, state) in legal[event]
        if expected:
            validate_invocation_transition(previous_state, state, event)
        else:
            with pytest.raises(ValueError, match="illegal invocation transition"):
                validate_invocation_transition(previous_state, state, event)


def test_terminal_invocation_states_have_no_outbound_transition() -> None:
    for transitions in legal_invocation_transitions().values():
        assert all(previous not in TERMINAL_INVOCATION_STATES for previous, _ in transitions)


def test_invocation_request_hash_is_canonical_and_identity_bound() -> None:
    first = ToolInvocationCreateIn.model_validate(invocation_payload())
    reordered_payload = invocation_payload()
    reordered_payload["input"] = {"scope": "provider_runtime"}
    second = ToolInvocationCreateIn.model_validate(reordered_payload)

    first_hash = invocation_request_hash(
        first,
        caller="command",
        authenticated_subject="lily-command",
    )
    assert first_hash == invocation_request_hash(
        second,
        caller="command",
        authenticated_subject="lily-command",
    )
    assert first_hash != invocation_request_hash(
        second,
        caller="admin_api",
        authenticated_subject="core-admin",
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unexpected": True}),
        lambda payload: payload.update({"capabilities": ["image", "image"]}),
        lambda payload: payload.update({"capabilities": ["Host_Read"]}),
        lambda payload: payload.update({"input": {"value": float("nan")}}),
        lambda payload: payload["principal"].update({"platform_roles": ["member", "member"]}),
        lambda payload: payload["principal"].update({"sender_id": " user "}),
        lambda payload: payload["principal"].update({"conversation_id": "private:1080353942"}),
    ],
)
def test_invocation_contract_rejects_ambiguous_or_unbounded_values(mutation) -> None:
    payload = invocation_payload()
    mutation(payload)

    with pytest.raises(ValidationError):
        ToolInvocationCreateIn.model_validate(payload)
