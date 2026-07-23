from __future__ import annotations

from hashlib import sha256

import pytest

from superlily_contracts import (
    AdapterSimulationError,
    CapabilityPlanningError,
    RenderDocument,
    plan_render_delivery,
    render_document_plain_text,
    simulate_adapter_delivery,
    simulate_constrained_text_delivery,
)


def _document() -> RenderDocument:
    return RenderDocument(
        schema_version="1.2",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        title="**同一份结构化结果**",
        blocks=[
            {
                "kind": "text",
                "node_id": "answer",
                "text": r"结果为 $x^2+1$。",
            },
            {
                "kind": "alternative",
                "node_id": "presentation",
                "preferred_option_id": "visual",
                "options": [
                    {
                        "option_id": "visual",
                        "label": "视觉版",
                        "requires": ["send_image"],
                        "blocks": [
                            {
                                "kind": "notice",
                                "node_id": "visual-result",
                                "severity": "info",
                                "text": "适合图片发送。",
                            }
                        ],
                    },
                    {
                        "option_id": "plain",
                        "label": "文本版",
                        "requires": ["send_text"],
                        "blocks": [
                            {
                                "kind": "text",
                                "node_id": "plain-result",
                                "text": "适合纯文本发送。",
                            }
                        ],
                    },
                ],
            },
        ],
    )


def test_planner_is_deterministic_and_explains_every_alternative() -> None:
    profile = {
        "profile": "onebot_v11.qq.v1",
        "supported": ["send_text", "send_image", "send_text"],
        "limits": {},
    }
    first = plan_render_delivery(_document(), profile)
    second = plan_render_delivery(
        _document(),
        {
            "limits": {},
            "supported": ["send_image", "send_text"],
            "profile": "onebot_v11.qq.v1",
        },
    )

    assert first.decision_hash == second.decision_hash
    assert first.capability_hash == second.capability_hash
    assert first.selected_family == "image"
    assert first.selected_alternatives[0].selected_option_id == "visual"
    assert first.rejected_alternatives[0].option_id == "plain"
    assert first.rejected_alternatives[0].reason == "lower_priority"
    assert first.degradation_reasons == ()
    assert first.ordered_payloads[0].source == "render_artifact"


def test_constrained_simulator_selects_text_and_keeps_auditable_semantics() -> None:
    decision, receipt = simulate_constrained_text_delivery(_document())

    assert decision.selected_family == "text"
    assert decision.selected_alternatives[0].selected_option_id == "plain"
    assert decision.rejected_alternatives[0].option_id == "visual"
    assert decision.rejected_alternatives[0].missing_capabilities == ("send_image",)
    assert decision.degradation_reasons == (
        "alternative_fallback:presentation:visual->plain",
        "image_unsupported_fallback_to_text",
    )
    assert receipt.transcript == (
        {
            "position": 0,
            "operation": "send_text",
            "text": decision.fallback_text,
        },
    )
    assert receipt.semantic_text_sha256 == sha256(
        render_document_plain_text(decision.resolved_document).encode("utf-8")
    ).hexdigest()

    replay_decision, replay = simulate_constrained_text_delivery(_document())
    assert replay_decision.decision_hash == decision.decision_hash
    assert replay.transcript_hash == receipt.transcript_hash


def test_qq_and_constrained_profiles_consume_the_same_document_without_platform_send() -> None:
    document = RenderDocument(
        schema_version="1.2",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        blocks=[
            {
                "kind": "text",
                "node_id": "same-result",
                "text": r"工具结果：$42$。",
            }
        ],
    )
    qq_decision, qq_receipt = simulate_adapter_delivery(
        document,
        {
            "profile": "onebot_v11.qq.v1",
            "supported": ["send_image", "send_text"],
            "limits": {"max_payloads": 1},
        },
        rendered_artifact_sha256="a" * 64,
    )
    text_decision, text_receipt = simulate_constrained_text_delivery(document)

    assert qq_decision.resolved_document_hash == text_decision.resolved_document_hash
    assert qq_receipt.semantic_text_sha256 == text_receipt.semantic_text_sha256
    assert qq_receipt.transcript[0]["operation"] == "send_image"
    assert text_receipt.transcript[0]["operation"] == "send_text"


def test_planner_rejects_unrenderable_profiles_and_unmatched_alternatives() -> None:
    plain_document = RenderDocument(
        schema_version="1.2",
        instance_id="nekro-agent",
        conversation_key="onebot_v11-group_1080353942",
        blocks=[{"kind": "text", "node_id": "result", "text": "结果"}],
    )
    with pytest.raises(
        CapabilityPlanningError, match="cannot deliver the rendered document"
    ):
        plan_render_delivery(
            plain_document,
            {"profile": "none.v1", "supported": [], "limits": {}},
        )
    with pytest.raises(
        CapabilityPlanningError, match="no document alternative matches"
    ):
        plan_render_delivery(
            _document(),
            {
                "profile": "audio_only.v1",
                "supported": ["send_audio"],
                "limits": {},
            },
        )


def test_simulator_enforces_adapter_text_limit_without_truncation() -> None:
    with pytest.raises(AdapterSimulationError, match="character limit"):
        simulate_adapter_delivery(
            _document(),
            {
                "profile": "tiny_text.v1",
                "supported": ["send_text"],
                "limits": {"max_payloads": 1, "max_text_chars": 8},
            },
        )
