from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import pytest
import superlily_core.history_import as history_import
from superlily_core.history_import import dry_run_legacy_rows, dry_run_payloads, main

# Fixed source identifiers and boundaries from docs/HISTORY_UNIFICATION.md.
LILY_CUTOVER = "2026-06-19T11:45:17.171050+00:00"
NEKRO_CUTOVER = "2026-06-19T11:49:44.696404+00:00"
LILY_SOURCE_SYSTEM = "lily.nonebot.chatrecorder.v2"
LILY_SOURCE_TABLE = "nonebot_plugin_chatrecorder_messagerecord_v2"
NEKRO_SOURCE_SYSTEM = "nekro.chat_message"
NEKRO_SOURCE_TABLE = "chat_message"

# The H2 limited rejection-code registry (mirrors history_import's finite set;
# dry-run output must never invent free-form codes outside it).
KNOWN_REJECTION_CODES = {
    "duplicate_source_identity",
    "invalid_create_time",
    "invalid_row",
    "invalid_send_timestamp",
    "invalid_sender_identity",
    "invalid_time",
    "missing_chat_key",
    "missing_bot_id",
    "missing_conversation_id",
    "missing_id",
    "missing_private_peer_id",
    "missing_scene_type",
    "missing_send_timestamp",
    "missing_sender_id",
    "missing_session_persist_id",
    "unknown_chat_type",
    "unknown_message_type",
}

# Nekro `send_timestamp` epoch seconds used by the mapping tests.
NEKRO_ELIGIBLE_EPOCH = "1781869783"  # last representable source second before cutover
NEKRO_BOUNDARY_EPOCH = "1781869784"  # source second containing the first Core observation
NEKRO_EPOCH_11_00 = "1781866800"
NEKRO_EPOCH_10_30 = "1781865000"
NEKRO_EPOCH_10_15 = "1781864100"


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


def _utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _lily_row(
    record_id: str,
    *,
    time_value: str,
    type_: str = "message",
    session_key: str = "session-1",
    scene_type: int = 1,
) -> dict:
    """A flattened nonebot_plugin_chatrecorder_messagerecord_v2 export row."""
    return {
        "id": record_id,
        "session_persist_id": session_key,
        "time": time_value,
        "type": type_,
        "message_id": f"message-{record_id}",
        "message": f"<message {record_id}>",
        "plain_text": f"hello {record_id}",
        "scene_type": scene_type,
        "bot_id": "985393579",
        "sender_id": "985393579" if type_ == "message_sent" else f"sender-{record_id}",
    }


def _nekro_row(
    record_id: str,
    *,
    send_timestamp: float | str | None,
    create_time: str = "2026-06-19 11:50:00.123456+00:00",
    chat_key: str = "chat-1",
    chat_type: str = "group",
    is_tome: bool = False,
) -> dict:
    """A flattened nekro_agent.chat_message export row."""
    row = {
        "id": record_id,
        "sender_id": f"sender-{record_id}",
        "sender_name": f"Sender {record_id}",
        "adapter_key": "onebot_v11",
        "message_id": f"message-{record_id}",
        "chat_key": chat_key,
        "chat_type": chat_type,
        "platform_userid": f"user-{record_id}",
        "content_text": f"hello {record_id}",
        "content_data": None,
        "raw_cq_code": None,
        "ext_data": None,
        "create_time": create_time,
        "update_time": create_time,
        "is_tome": is_tome,
    }
    if send_timestamp is not None:
        row["send_timestamp"] = send_timestamp
    return row


def _sqlite_row(
    record_id: str,
    *,
    type_: str = "message",
    detail_type: str = "group",
    bot_id: str | None = "985393579",
) -> dict:
    return {
        "id": record_id,
        "platform": "qq",
        "time": "2023-07-17 10:04:36.326594",
        "type": type_,
        "detail_type": detail_type,
        "message_id": f"sqlite-message-{record_id}",
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "plain_text": "hello",
        "user_id": bot_id if type_ == "message_sent" else f"sender-{record_id}",
        "group_id": "1080353942" if detail_type == "group" else None,
        "bot_type": "OneBot V11" if bot_id else None,
        "bot_id": bot_id,
        "guild_id": None,
        "channel_id": None,
    }


def _eligible_by_id(report: dict) -> dict[str, dict]:
    return {sample["source_record_id"]: sample for sample in report["sample_eligible"]}


def test_history_import_dry_run_summarizes_candidate_payloads() -> None:
    invalid = {"source_event_id": "SECRET-BODY-CONTENT"}
    report = dry_run_payloads([_candidate("qq:group:123:message:456"), invalid])

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
    assert "SECRET-BODY-CONTENT" not in json.dumps(report["sample_errors"])


def test_history_import_legacy_lily_naive_time_is_utc_and_cutover_is_strict() -> None:
    rows = [
        # One microsecond before the boundary, naive wall time.
        _lily_row("lily-1", time_value="2026-06-19 11:45:17.171049", scene_type=1),
        # Naive wall time equal to the boundary instant *only* under UTC; under
        # Asia/Shanghai it would be 03:45 UTC and eligible.
        _lily_row("lily-2", time_value="2026-06-19 11:45:17.171050", scene_type=1),
        _lily_row("lily-3", time_value="2026-06-19 11:45:17.171051", type_="message_sent"),
        _lily_row("lily-4", time_value="2026-06-19 11:00:00", type_="message_sent", scene_type=0),
    ]
    report = dry_run_legacy_rows("lily", rows, LILY_CUTOVER)

    assert report["source_system"] == LILY_SOURCE_SYSTEM
    assert report["source_table"] == LILY_SOURCE_TABLE
    assert report["manifest_schema_version"] == "history-dry-run-v1"
    assert _utc(report["cutover_boundary"]) == _utc(LILY_CUTOVER)
    assert report["total"] == 4
    assert report["eligible"] == 2
    assert report["excluded_at_or_after_cutover"] == 2
    assert report["rejected"] == 0
    assert report["duplicates"] == 0
    assert report["writes"] == 0
    assert report["by_month"] == {"2026-06": 2}
    assert report["by_direction"] == {"inbound": 1, "outbound": 1}
    assert report["by_bot_id"] == {"985393579": 2}
    assert report["by_conversation_type"] == {"group": 1, "private": 1}


    assert report["by_source_conversation_key"] == {"session-1": 2}
    assert report["eligible_empty_text"] == 0
    assert _utc(report["eligible_occurred_at_min"]) == _utc("2026-06-19T11:00:00+00:00")
    assert _utc(report["eligible_occurred_at_max"]) == _utc(
        "2026-06-19T11:45:17.171049+00:00"
    )
    assert report["by_rejection_code"] == {}
    assert report["eligible"] + report["excluded_at_or_after_cutover"] + report["rejected"] == report["total"]

    samples = _eligible_by_id(report)
    required_sample_keys = {
        "source_record_id",
        "occurred_at",
        "source_persisted_at",
        "direction",
        "source_conversation_key",
        "source_conversation_type",
        "conversation_type",
        "sender_id",
    }
    assert required_sample_keys <= samples["lily-1"].keys()
    for body_key in ("message", "plain_text", "content_text", "segments", "content_data"):
        assert body_key not in samples["lily-1"]

    assert _utc(samples["lily-1"]["occurred_at"]) == _utc("2026-06-19T11:45:17.171049+00:00")
    assert samples["lily-1"]["direction"] == "inbound"
    assert samples["lily-1"]["source_conversation_key"] == "session-1"
    assert str(samples["lily-1"]["source_conversation_type"]) == "1"
    assert samples["lily-1"]["conversation_type"] == "group"

    assert "lily-2" not in samples
    assert "lily-3" not in samples

    assert _utc(samples["lily-4"]["occurred_at"]) == _utc("2026-06-19T11:00:00+00:00")
    assert samples["lily-4"]["direction"] == "outbound"
    assert str(samples["lily-4"]["source_conversation_type"]) == "0"
    assert samples["lily-4"]["conversation_type"] == "private"


def test_history_import_sqlite_maps_group_rows_and_allows_unknown_inbound_bot() -> None:
    rows = [
        _sqlite_row("1"),
        _sqlite_row("2", type_="message_sent"),
        _sqlite_row("3", bot_id=None),
    ]
    report = dry_run_legacy_rows(
        "sqlite-data2",
        rows,
        "2024-08-28T13:25:30+00:00",
        source_snapshot_id="sqlite-data2-fixture",
        source_schema_version="chatrecorder-sqlite-9bca28bcb998",
        mapping_version="history-map-v1",
    )

    assert report["eligible"] == 3
    assert report["rejected"] == 0
    assert report["by_month"] == {"2023-07": 3}
    assert report["by_direction"] == {"inbound": 2, "outbound": 1}
    assert report["by_bot_id"] == {"985393579": 2}
    assert report["source_system"] == "lily.nonebot.chatrecorder.sqlite.data2"


def test_history_import_sqlite_rejects_unattributable_private_outbound() -> None:
    report = dry_run_legacy_rows(
        "sqlite-data3",
        [_sqlite_row("1", type_="message_sent", detail_type="private")],
        "2024-08-28T13:25:30+00:00",
    )

    assert report["eligible"] == 0
    assert report["rejected"] == 1
    assert report["by_rejection_code"] == {"missing_private_peer_id": 1}


def test_history_import_legacy_lily_rejects_unknown_type_and_duplicate_identity() -> None:
    first = _lily_row("lily-10", time_value="2026-06-19 10:00:00")
    rows = [
        first,
        _lily_row("lily-11", time_value="2026-06-19 10:01:00", type_="notice"),
        dict(first),  # same (source_system, source_table, source_record_id) triple
    ]
    report = dry_run_legacy_rows("lily", rows, LILY_CUTOVER)

    assert report["total"] == 3
    assert report["eligible"] == 0
    assert report["excluded_at_or_after_cutover"] == 0
    assert report["rejected"] == 3
    assert report["duplicates"] == 1
    assert report["by_direction"] == {}
    assert report["by_rejection_code"] == {
        "unknown_message_type": 1,
        "duplicate_source_identity": 2,
    }
    assert set(report["by_rejection_code"]) <= KNOWN_REJECTION_CODES
    assert report["writes"] == 0
    assert {entry["code"] for entry in report["sample_rejections"]} == {
        "unknown_message_type",
        "duplicate_source_identity",
    }


def test_history_import_legacy_nekro_maps_epoch_and_preserves_raw_chat_type() -> None:
    first = _nekro_row(
        "nekro-1",
        send_timestamp=NEKRO_ELIGIBLE_EPOCH,
        create_time="2026-06-19 11:50:00.123456+00:00",
        chat_type="group",
    )
    rows = [
        first,
        _nekro_row("nekro-2", send_timestamp=NEKRO_BOUNDARY_EPOCH),
        # create_time is before the boundary, but send_timestamp is missing:
        # must be rejected, never silently converted to an eligible row.
        _nekro_row("nekro-3", send_timestamp=None, create_time="2026-06-19 10:00:00+00:00"),
        _nekro_row("nekro-4", send_timestamp=NEKRO_EPOCH_11_00, chat_type="ChatType.PRIVATE"),
        # is_tome does not prove an outbound message; direction stays unknown.
        _nekro_row("nekro-5", send_timestamp=NEKRO_EPOCH_10_30, chat_type="private", is_tome=True),
        _nekro_row("nekro-6", send_timestamp=NEKRO_EPOCH_10_15, chat_type="ChatType.GROUP"),
        dict(first),
    ]
    report = dry_run_legacy_rows("nekro", rows, NEKRO_CUTOVER)

    assert report["source_system"] == NEKRO_SOURCE_SYSTEM
    assert report["source_table"] == NEKRO_SOURCE_TABLE
    assert _utc(report["cutover_boundary"]) == _utc(NEKRO_CUTOVER)
    assert _utc(report["source_cutover_boundary"]) == _utc("2026-06-19T11:49:44+00:00")
    assert report["total"] == 7
    assert report["eligible"] == 3
    assert report["excluded_at_or_after_cutover"] == 1
    assert report["rejected"] == 3
    assert report["duplicates"] == 1
    assert report["writes"] == 0
    assert report["by_month"] == {"2026-06": 3}
    assert report["by_direction"] == {"unknown": 3}
    assert report["by_bot_id"] == {}
    assert report["by_adapter_key"] == {"onebot_v11": 3}
    assert report["by_conversation_type"] == {"group": 1, "private": 2}
    assert report["by_source_conversation_key"] == {"chat-1": 3}
    assert report["eligible_empty_text"] == 0
    assert report["by_rejection_code"] == {
        "missing_send_timestamp": 1,
        "duplicate_source_identity": 2,
    }
    assert set(report["by_rejection_code"]) <= KNOWN_REJECTION_CODES
    assert report["eligible"] + report["excluded_at_or_after_cutover"] + report["rejected"] == report["total"]

    samples = _eligible_by_id(report)
    # send_timestamp epoch -> occurred_at; create_time -> source_persisted_at;
    # the two must stay distinct.
    # Raw chat_type values are preserved; normalization happens at the mapping layer.
    assert samples["nekro-4"]["source_conversation_type"] == "ChatType.PRIVATE"
    assert samples["nekro-4"]["conversation_type"] == "private"
    assert samples["nekro-6"]["source_conversation_type"] == "ChatType.GROUP"
    assert samples["nekro-6"]["conversation_type"] == "group"

    assert samples["nekro-5"]["direction"] == "unknown"
    assert samples["nekro-5"]["conversation_type"] == "private"

    assert "nekro-1" not in samples  # entire duplicate identity group is rejected
    assert "nekro-2" not in samples  # boundary second is excluded
    assert "nekro-3" not in samples  # rejected, not excluded and not eligible


def test_history_import_legacy_nekro_missing_send_timestamp_never_falls_back() -> None:
    report = dry_run_legacy_rows(
        "nekro",
        [
            _nekro_row(
                "nekro-99",
                send_timestamp=None,
                create_time="2026-06-19 10:00:00+00:00",
            )
        ],
        NEKRO_CUTOVER,
    )

    assert report["total"] == 1
    assert report["eligible"] == 0
    assert report["excluded_at_or_after_cutover"] == 0
    assert report["rejected"] == 1
    assert report["duplicates"] == 0
    assert report["by_rejection_code"] == {"missing_send_timestamp": 1}
    assert report["writes"] == 0
    assert report["sample_rejections"][0]["code"] == "missing_send_timestamp"


def test_history_import_legacy_manifest_hash_is_stable_and_order_independent() -> None:
    rows = [
        _lily_row("lily-20", time_value="2026-06-19 09:00:00"),
        _lily_row("lily-21", time_value="2026-06-19 09:01:00", type_="message_sent"),
        _lily_row("lily-22", time_value="2026-06-19 09:02:00"),
    ]
    first = dry_run_legacy_rows("lily", rows, LILY_CUTOVER)
    rerun = dry_run_legacy_rows("lily", rows, LILY_CUTOVER)
    reordered = dry_run_legacy_rows("lily", list(reversed(rows)), LILY_CUTOVER)

    assert re.fullmatch(r"[0-9a-f]{64}", first["manifest_sha256"])
    assert first["manifest_sha256"] == rerun["manifest_sha256"]
    assert first["manifest_sha256"] == reordered["manifest_sha256"]
    assert first["sample_eligible"] == reordered["sample_eligible"]
    assert first["sample_rejections"] == reordered["sample_rejections"]

    changed = [dict(rows[0]), dict(rows[1]), dict(rows[2])]
    changed[0]["plain_text"] = "changed content"
    content_changed = dry_run_legacy_rows("lily", changed, LILY_CUTOVER)
    assert content_changed["manifest_sha256"] != first["manifest_sha256"]


def test_history_import_legacy_rejects_entire_conflicting_identity_group_order_independently() -> None:
    inbound = _lily_row("same-id", time_value="2026-06-19 09:00:00")
    outbound = _lily_row(
        "same-id",
        time_value="2026-06-19 09:01:00",
        type_="message_sent",
    )

    first = dry_run_legacy_rows("lily", [inbound, outbound], LILY_CUTOVER)
    reversed_report = dry_run_legacy_rows("lily", [outbound, inbound], LILY_CUTOVER)

    for report in (first, reversed_report):
        assert report["eligible"] == 0
        assert report["excluded_at_or_after_cutover"] == 0
        assert report["rejected"] == 2
        assert report["duplicates"] == 1
        assert report["by_rejection_code"] == {"duplicate_source_identity": 2}
    assert first == reversed_report


def test_history_import_legacy_rejects_non_frozen_cutover() -> None:
    with pytest.raises(ValueError, match="cutover boundary"):
        dry_run_legacy_rows(
            "lily",
            [_lily_row("lily-30", time_value="2026-06-19 09:00:00")],
            "2026-06-19T19:45:17.171050+00:00",
        )


def test_history_import_legacy_rejects_invalid_cutover_as_configuration_error() -> None:
    with pytest.raises(ValueError, match="cutover_boundary must be an ISO-8601"):
        dry_run_legacy_rows("lily", [], "not-a-date")


@pytest.mark.parametrize(
    "time_value",
    ["2026-06-19", "2026-06-19 11:00:00+08:00"],
)
def test_history_import_legacy_lily_rejects_non_naive_utc_export(time_value: str) -> None:
    report = dry_run_legacy_rows(
        "lily",
        [_lily_row("lily-bad-time", time_value=time_value)],
        LILY_CUTOVER,
    )

    assert report["eligible"] == 0
    assert report["rejected"] == 1
    assert report["by_rejection_code"] == {"invalid_time": 1}


def test_history_import_legacy_nekro_uses_source_second_boundary() -> None:
    report = dry_run_legacy_rows(
        "nekro",
        [
            _nekro_row("before", send_timestamp="1781869783"),
            _nekro_row("equal", send_timestamp="1781869784"),
            _nekro_row("sentinel", send_timestamp="0"),
        ],
        NEKRO_CUTOVER,
    )

    assert report["eligible"] == 1
    assert report["excluded_at_or_after_cutover"] == 1
    assert report["rejected"] == 1
    assert report["by_rejection_code"] == {"invalid_send_timestamp": 1}
    assert _eligible_by_id(report)["before"]["occurred_at"].endswith("11:49:43+00:00")


def test_history_import_legacy_nekro_rejects_float_epoch_and_missing_create_time() -> None:
    float_row = _nekro_row("float", send_timestamp=1781869783.0)
    missing_create = _nekro_row("missing-create", send_timestamp="1781869783")
    missing_create["create_time"] = None

    report = dry_run_legacy_rows("nekro", [float_row, missing_create], NEKRO_CUTOVER)

    assert report["eligible"] == 0
    assert report["rejected"] == 2
    assert report["by_rejection_code"] == {
        "invalid_create_time": 1,
        "invalid_send_timestamp": 1,
    }


def test_history_import_legacy_lily_rejects_outbound_sender_not_matching_bot() -> None:
    row = _lily_row(
        "bad-outbound-sender",
        time_value="2026-06-19 09:00:00",
        type_="message_sent",
    )
    row["sender_id"] = "someone-else"

    report = dry_run_legacy_rows("lily", [row], LILY_CUTOVER)

    assert report["eligible"] == 0
    assert report["by_rejection_code"] == {"invalid_sender_identity": 1}


def test_history_import_legacy_uses_and_removes_disk_identity_ledger(
    tmp_path, monkeypatch
) -> None:
    observed_database = False

    def rows():
        nonlocal observed_database
        ledger_paths = list(tmp_path.glob("superlily-history-identities-*/seen-source-identities.sqlite3"))
        observed_database = any(path.is_file() for path in ledger_paths)
        yield _lily_row("lily-disk-ledger", time_value="2026-06-19 09:00:00")

    monkeypatch.setattr(history_import.tempfile, "tempdir", str(tmp_path))
    report = dry_run_legacy_rows("lily", rows(), LILY_CUTOVER)

    assert report["eligible"] == 1
    assert observed_database is True
    assert list(tmp_path.iterdir()) == []


def test_history_import_cli_top_level_help_lists_both_modes(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "eventin" in output
    assert "legacy" in output


def test_history_import_cli_legacy_mode_writes_zero_and_prints_manifest(tmp_path, capsys) -> None:
    path = tmp_path / "nekro-export.jsonl"
    rows = [
        _nekro_row("nekro-1", send_timestamp=NEKRO_ELIGIBLE_EPOCH),
        _nekro_row("nekro-1", send_timestamp=NEKRO_ELIGIBLE_EPOCH),
    ]
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    rc = main(
        [
            "legacy",
            "--source",
            "nekro",
            "--cutover",
            NEKRO_CUTOVER,
            "--snapshot-id",
            "nekro-fixture-snapshot",
            "--source-schema-version",
            "nekro-chat-message-v1",
            "--mapping-version",
            "history-map-v1",
            "--jsonl",
            str(path),
        ]
    )

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["source_system"] == NEKRO_SOURCE_SYSTEM
    assert report["source_table"] == NEKRO_SOURCE_TABLE
    assert report["total"] == 2
    assert report["eligible"] == 0
    assert report["rejected"] == 2
    assert report["duplicates"] == 1
    assert report["writes"] == 0
    assert re.fullmatch(r"[0-9a-f]{64}", report["manifest_sha256"])


def test_history_import_cli_legacy_reports_bad_jsonl_with_path_and_line(tmp_path) -> None:
    path = tmp_path / "broken-export.jsonl"
    path.write_text(
        json.dumps(_nekro_row("nekro-1", send_timestamp=NEKRO_ELIGIBLE_EPOCH))
        + "\n{this is not json\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=re.escape(f"{path}:2")):
        main(
            [
                "legacy",
                "--source",
                "nekro",
                "--cutover",
                NEKRO_CUTOVER,
                "--snapshot-id",
                "nekro-fixture-snapshot",
                "--source-schema-version",
                "nekro-chat-message-v1",
                "--mapping-version",
                "history-map-v1",
                "--jsonl",
                str(path),
            ]
        )
