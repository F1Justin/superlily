from superlily_core.history_import import dry_run_payloads


def _candidate(source_event_id: str) -> dict:
    return {
        "schema_version": "1.0",
        "source_event_id": source_event_id,
        "instance": {
            "instance_id": "lily-command",
            "platform": "qq",
            "adapter": "onebot_v11",
            "bot_id": "985393579",
            "role": "command",
        },
        "event_type": "message",
        "conversation": {"id": "123", "type": "group"},
        "sender": {"id": "789", "roles": []},
        "message": {"id": "456", "text": "hello", "segments": [], "attachments": []},
        "references": [{"type": "reply_to", "platform_message_id": "455"}],
        "occurred_at": "2026-06-19T12:00:00+00:00",
        "metadata": {"original_source": "lily"},
    }


def test_history_import_dry_run_summarizes_candidate_payloads() -> None:
    report = dry_run_payloads([_candidate("qq:group:123:message:456"), {"source_event_id": ""}])

    assert report["total"] == 2
    assert report["valid"] == 1
    assert report["invalid"] == 1
    assert report["references"] == 1
    assert report["reply_references"] == 1
    assert report["with_platform_message_id"] == 1
    assert report["with_text"] == 1
    assert report["by_original_source"] == {"lily": 1, "unknown": 1}
    assert report["writes"] == 0
    assert report["sample_errors"]
