import json
from datetime import datetime

import pytest
from pydantic import ValidationError

from superlily_contracts import (
    EventIn,
    EventReference,
    ResponseIn,
    PlatformCapabilities,
    RuntimePlugin,
    SanitizationPolicy,
    sanitize_payload,
)


def test_sanitizer_redacts_secrets_and_url_queries() -> None:
    result = sanitize_payload(
        {
            "access_token": "top-secret",
            "core_token": "another-secret",
            "database_url": "postgresql://user:password@database/core",
            "nested": {"password": "also-secret"},
            "image_url": "https://user:password@example.test/image?a=credential&b=2",
            "jumpUrl": "mqqapi://user:password@qzoneschema/feed?token=secret#fragment",
            "file": "base64://very-long-secret-binary",
        },
        SanitizationPolicy(enabled=True),
    )
    assert result == {
        "access_token": "[REDACTED]",
        "core_token": "[REDACTED]",
        "database_url": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
        "image_url": "https://example.test/image",
        "jumpUrl": "mqqapi://qzoneschema/feed",
        "file": "[BINARY_DATA]",
    }


def test_sanitizer_strips_userinfo_query_and_fragment_from_custom_uri_fields() -> None:
    result = sanitize_payload(
        {
            "file": "mqqapi://user:password@qzoneschema/feed?token=secret#fragment",
            "platform_id": "napcat-resource://user:password@resource/opaque?id=secret#fragment",
            "callback_uri": "custom-scheme:/callback/path?ticket=secret#fragment",
        },
        SanitizationPolicy(enabled=True),
    )

    assert result == {
        "file": "mqqapi://qzoneschema/feed",
        "platform_id": "napcat-resource://resource/opaque",
        "callback_uri": "custom-scheme:/callback/path",
    }


def test_raw_payload_is_disabled_by_default() -> None:
    assert sanitize_payload({"anything": "value"}, SanitizationPolicy()) is None


def test_wire_models_replace_postgres_incompatible_nul_recursively() -> None:
    event = EventIn.model_validate(
        {
            "source_event_id": "qq:group:1:message:2",
            "instance": {
                "instance_id": "lily-command",
                "platform": "qq",
                "adapter": "onebot_v11",
                "bot_id": "1",
                "role": "command",
            },
            "event_type": "message",
            "conversation": {"id": "1", "type": "group", "name": "a\x00b"},
            "message": {
                "id": "2",
                "text": "前\x00后",
                "segments": [{"type": "text", "data": {"text": "前\x00后"}}],
            },
            "metadata": {"nul\x00key": "nul\x00value"},
            "occurred_at": "2026-06-19T12:00:00+00:00",
        }
    )

    assert event.conversation.name == "a\ufffdb"
    assert event.message is not None
    assert event.message.text == "前\ufffd后"
    assert event.message.segments[0]["data"]["text"] == "前\ufffd后"
    assert event.metadata == {"nul\ufffdkey": "nul\ufffdvalue"}

    response = ResponseIn.model_validate(
        {
            "source_response_id": "response-1",
            "instance": event.instance.model_dump(),
            "response_type": "message",
            "conversation": {"id": "1", "type": "group"},
            "text": "答\x00案",
            "segments": [],
            "attachments": [],
            "success": False,
            "error": "错\x00误",
            "occurred_at": "2026-06-19T12:00:00+00:00",
        }
    )
    assert response.text == "答\ufffd案"
    assert response.error == "错\ufffd误"


def test_sanitizer_recurses_into_json_encoded_segment_data() -> None:
    result = sanitize_payload(
        {
            "data": json.dumps(
                {
                    "image_url": "https://user:password@example.test/image?token=secret",
                    "authorization": "Bearer secret",
                    "label": "keep",
                }
            ),
            "text": '{"authorization":"ordinary user text"}',
        },
        SanitizationPolicy(enabled=True),
    )

    assert json.loads(result["data"]) == {
        "image_url": "https://example.test/image",
        "authorization": "[REDACTED]",
        "label": "keep",
    }
    assert result["text"] == '{"authorization":"ordinary user text"}'

    truncated = sanitize_payload(
        {"data": json.dumps({"label": "x" * 200})},
        SanitizationPolicy(enabled=True, max_string=40),
    )
    assert json.loads(truncated["data"])["_truncated"] is True


def test_event_rejects_naive_timestamp() -> None:
    with pytest.raises(ValidationError):
        EventIn.model_validate(
            {
                "source_event_id": "qq:group:1:message:2",
                "instance": {
                    "instance_id": "lily-command",
                    "platform": "qq",
                    "adapter": "onebot_v11",
                    "bot_id": "1",
                    "role": "command",
                },
                "event_type": "message",
                "conversation": {"id": "1", "type": "group"},
                "occurred_at": datetime(2026, 6, 19, 12, 0, 0),
            }
        )


def test_event_accepts_structured_references() -> None:
    event = EventIn.model_validate(
        {
            "source_event_id": "qq:group:1:message:2",
            "instance": {
                "instance_id": "lily-command",
                "platform": "qq",
                "adapter": "onebot_v11",
                "bot_id": "1",
                "role": "command",
            },
            "event_type": "message",
            "conversation": {"id": "1", "type": "group"},
            "references": [
                {
                    "type": "reply_to",
                    "platform_message_id": "1",
                    "conversation_id": "1",
                    "conversation_type": "group",
                }
            ],
            "occurred_at": "2026-06-19T12:00:00+00:00",
        }
    )

    assert event.references == [
        EventReference(
            type="reply_to",
            platform_message_id="1",
            conversation_id="1",
            conversation_type="group",
        )
    ]


def test_event_reference_requires_a_target_hint() -> None:
    with pytest.raises(ValidationError):
        EventReference(type="reply_to")


def test_runtime_plugin_rejects_impossible_classification_counts() -> None:
    with pytest.raises(ValidationError):
        RuntimePlugin(
            plugin_id="demo",
            module_name="plugins.demo",
            matcher_count=1,
            classified_matcher_count=2,
        )


def test_platform_capabilities_are_canonical_and_conservative() -> None:
    capabilities = PlatformCapabilities(
        profile="onebot_v11.qq.v1",
        supported=["send_text", "mention", "reply"],
        limits={"text_chars": 4_096},
    )
    assert capabilities.supported == ["mention", "reply", "send_text"]

    with pytest.raises(ValidationError, match="unique"):
        PlatformCapabilities(
            profile="onebot_v11.qq.v1",
            supported=["send_text", "send_text"],
        )
    with pytest.raises(ValidationError, match="non-negative"):
        PlatformCapabilities(
            profile="onebot_v11.qq.v1",
            limits={"text_chars": -1},
        )
