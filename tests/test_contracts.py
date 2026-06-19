from datetime import datetime

import pytest
from pydantic import ValidationError

from superlily_contracts import EventIn, SanitizationPolicy, sanitize_payload


def test_sanitizer_redacts_secrets_and_url_queries() -> None:
    result = sanitize_payload(
        {
            "access_token": "top-secret",
            "nested": {"password": "also-secret"},
            "image_url": "https://example.test/image?a=credential&b=2",
            "file": "base64://very-long-secret-binary",
        },
        SanitizationPolicy(enabled=True),
    )
    assert result == {
        "access_token": "[REDACTED]",
        "nested": {"password": "[REDACTED]"},
        "image_url": "https://example.test/image",
        "file": "[BINARY_DATA]",
    }


def test_raw_payload_is_disabled_by_default() -> None:
    assert sanitize_payload({"anything": "value"}, SanitizationPolicy()) is None


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
