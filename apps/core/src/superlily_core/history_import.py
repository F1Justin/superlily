"""Write-free validators for historical import candidates.

The legacy mode is the H2 dry-run gate.  It consumes rows exported from a
read-only source snapshot, applies the frozen source-specific time and identity
contracts, and emits a content-free manifest.  It never connects to or writes
the archive schema.

The EventIn mode is retained only for compatibility with the older Phase 2a
candidate lint.  It is not an archive importer.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import sqlite3
import tempfile
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from superlily_contracts import EventIn


LILY_SOURCE_SYSTEM = "lily.nonebot.chatrecorder.v2"
LILY_SOURCE_TABLE = "nonebot_plugin_chatrecorder_messagerecord_v2"
LILY_CUTOVER_BOUNDARY = datetime(2026, 6, 19, 11, 45, 17, 171050, tzinfo=timezone.utc)
NEKRO_SOURCE_SYSTEM = "nekro.chat_message"
NEKRO_SOURCE_TABLE = "chat_message"
NEKRO_CUTOVER_BOUNDARY = datetime(2026, 6, 19, 11, 49, 44, 696404, tzinfo=timezone.utc)
NEKRO_SOURCE_CUTOVER_BOUNDARY = datetime(2026, 6, 19, 11, 49, 44, tzinfo=timezone.utc)
MANIFEST_SCHEMA_VERSION = "history-dry-run-v1"

_SOURCE_ALIASES = {
    "lily": LILY_SOURCE_SYSTEM,
    LILY_SOURCE_SYSTEM: LILY_SOURCE_SYSTEM,
    "nekro": NEKRO_SOURCE_SYSTEM,
    NEKRO_SOURCE_SYSTEM: NEKRO_SOURCE_SYSTEM,
}
_LIMITED_REJECTION_CODES = {
    "duplicate_source_identity",
    "invalid_create_time",
    "invalid_row",
    "invalid_send_timestamp",
    "invalid_time",
    "invalid_sender_identity",
    "missing_chat_key",
    "missing_bot_id",
    "missing_id",
    "missing_scene_type",
    "missing_send_timestamp",
    "missing_sender_id",
    "missing_session_persist_id",
    "unknown_chat_type",
    "unknown_message_type",
}


class _RejectedRow(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        if code not in _LIMITED_REJECTION_CODES:
            raise ValueError(f"unregistered rejection code: {code}")
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _source_label(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        label = metadata.get("original_source") or metadata.get("source")
        if label:
            return str(label)
    return "unknown"


def dry_run_payloads(payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Retain the pre-H2 EventIn candidate lint without granting write authority."""

    total = 0
    valid = 0
    invalid = 0
    references = 0
    reply_references = 0
    with_platform_message_id = 0
    with_text = 0
    by_original_source: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []

    for index, payload in enumerate(payloads, start=1):
        total += 1
        by_original_source[_source_label(payload)] += 1
        try:
            event = EventIn.model_validate(payload)
        except ValidationError as exc:
            invalid += 1
            if len(errors) < 20:
                redacted = [
                    {key: error[key] for key in ("type", "loc") if key in error}
                    for error in exc.errors(include_url=False)
                ]
                errors.append({"index": index, "error": redacted})
            continue

        valid += 1
        references += len(event.references)
        reply_references += sum(1 for reference in event.references if reference.type == "reply_to")
        if event.message and event.message.id:
            with_platform_message_id += 1
        if event.message and event.message.text:
            with_text += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "references": references,
        "reply_references": reply_references,
        "with_platform_message_id": with_platform_message_id,
        "with_text": with_text,
        "by_original_source": dict(sorted(by_original_source.items())),
        "sample_errors": errors,
        "writes": 0,
    }


def _canonical_source(source: str) -> str:
    try:
        return _SOURCE_ALIASES[source]
    except KeyError as exc:
        raise ValueError(f"unsupported legacy source: {source}") from exc


def _parse_aware_datetime(value: Any, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise _RejectedRow(f"invalid_{field}", f"{field} is not ISO-8601") from exc
    else:
        raise _RejectedRow(f"invalid_{field}", f"{field} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None:
        raise _RejectedRow(f"invalid_{field}", f"{field} must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_cutover_boundary(value: Any) -> datetime:
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            raise TypeError
    except (TypeError, ValueError) as exc:
        raise ValueError("cutover_boundary must be an ISO-8601 datetime with a UTC offset") from exc
    if parsed.tzinfo is None:
        raise ValueError("cutover_boundary must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _parse_lily_time(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        stripped = value.strip()
        if "T" not in stripped and " " not in stripped:
            raise _RejectedRow("invalid_time", "time must include a clock time")
        try:
            parsed = datetime.fromisoformat(stripped)
        except ValueError as exc:
            raise _RejectedRow("invalid_time", "time is not ISO-8601") from exc
    else:
        raise _RejectedRow("invalid_time", "time must be a datetime or ISO-8601 string")
    # chatrecorder 0.7.0 stores UTC in a timestamp-without-time-zone column.
    if parsed.tzinfo is not None:
        raise _RejectedRow("invalid_time", "Lily time must be exported without a UTC offset")
    return parsed.replace(tzinfo=timezone.utc)


def _parse_nekro_epoch(value: Any) -> datetime:
    if value is None or value == "":
        raise _RejectedRow("missing_send_timestamp", "send_timestamp is required")
    if isinstance(value, (bool, datetime, float)):
        raise _RejectedRow(
            "invalid_send_timestamp",
            "send_timestamp must be integer epoch seconds or an exact decimal string",
        )
    try:
        epoch = Decimal(str(value))
        micros = epoch * Decimal(1_000_000)
        if not micros.is_finite() or micros != micros.to_integral_value():
            raise InvalidOperation
        occurred_at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            microseconds=int(micros)
        )
        if occurred_at < datetime(2024, 1, 1, tzinfo=timezone.utc):
            raise InvalidOperation
        return occurred_at
    except (InvalidOperation, TypeError, ValueError, OverflowError) as exc:
        raise _RejectedRow("invalid_send_timestamp", "send_timestamp must be epoch seconds") from exc


def _conversation_type(raw: Any) -> str:
    normalized = str(raw or "").strip().lower()
    if normalized in {"group", "chattype.group"}:
        return "group"
    if normalized in {"private", "chattype.private"}:
        return "private"
    raise _RejectedRow("unknown_chat_type", f"unsupported chat_type: {raw!r}")


def _source_record_id(row: Mapping[str, Any]) -> str:
    value = row.get("id")
    if value is None or str(value).strip() == "":
        raise _RejectedRow("missing_id", "source primary key id is required")
    return str(value).strip()


def _normalize_lily(row: Mapping[str, Any]) -> dict[str, Any]:
    source_record_id = _source_record_id(row)
    session_key = row.get("source_conversation_key", row.get("session_persist_id"))
    if session_key is None or str(session_key).strip() == "":
        raise _RejectedRow("missing_session_persist_id", "session_persist_id is required")
    message_type = str(row.get("type") or "")
    directions = {"message": "inbound", "message_sent": "outbound"}
    if message_type not in directions:
        raise _RejectedRow("unknown_message_type", f"unsupported message type: {message_type!r}")
    occurred_at = _parse_lily_time(row.get("time"))
    raw_scene_type = row.get("source_conversation_type", row.get("scene_type"))
    if raw_scene_type is None or str(raw_scene_type).strip() == "":
        raise _RejectedRow("missing_scene_type", "scene_type from the session join is required")
    scene = str(raw_scene_type).strip().lower()
    if scene in {"0", "private"}:
        conversation_type = "private"
    elif scene in {"1", "group"}:
        conversation_type = "group"
    else:
        raise _RejectedRow("unknown_chat_type", f"unsupported scene_type: {raw_scene_type!r}")
    bot_id = row.get("bot_id")
    if bot_id is None or str(bot_id).strip() == "":
        raise _RejectedRow("missing_bot_id", "bot_id from the session join is required")
    sender_id = row.get("sender_id")
    if sender_id is None or str(sender_id).strip() == "":
        raise _RejectedRow("missing_sender_id", "sender_id from the session join is required")
    if message_type == "message_sent" and str(sender_id).strip() != str(bot_id).strip():
        raise _RejectedRow(
            "invalid_sender_identity",
            "outbound Lily sender_id must equal the session bot_id",
        )
    return {
        "source_record_id": source_record_id,
        "occurred_at": occurred_at,
        "source_persisted_at": None,
        "direction": directions[message_type],
        "source_conversation_key": str(session_key).strip(),
        "source_conversation_type": None if raw_scene_type is None else str(raw_scene_type),
        "conversation_type": conversation_type,
        "bot_id": str(bot_id).strip(),
        "sender_id": str(sender_id).strip(),
        "text_is_empty": str(row.get("plain_text") or "").strip() == "",
    }


def _normalize_nekro(row: Mapping[str, Any]) -> dict[str, Any]:
    source_record_id = _source_record_id(row)
    chat_key = row.get("chat_key")
    if chat_key is None or str(chat_key).strip() == "":
        raise _RejectedRow("missing_chat_key", "chat_key is required")
    sender_id = row.get("sender_id")
    if sender_id is None or str(sender_id).strip() == "":
        raise _RejectedRow("missing_sender_id", "sender_id is required")
    occurred_at = _parse_nekro_epoch(row.get("send_timestamp"))
    source_persisted_at = _parse_aware_datetime(row.get("create_time"), field="create_time")
    raw_chat_type = row.get("chat_type")
    adapter_key = row.get("adapter_key")
    if adapter_key is None or str(adapter_key).strip() == "":
        raise _RejectedRow("invalid_row", "adapter_key is required")
    return {
        "source_record_id": source_record_id,
        "occurred_at": occurred_at,
        "source_persisted_at": source_persisted_at,
        "direction": "unknown",
        "source_conversation_key": str(chat_key).strip(),
        "source_conversation_type": None if raw_chat_type is None else str(raw_chat_type),
        "conversation_type": _conversation_type(raw_chat_type),
        "sender_id": str(sender_id).strip(),
        "adapter_key": str(adapter_key).strip(),
        "text_is_empty": str(row.get("content_text") or "").strip() == "",
    }


def _manifest_item(source_system: str, source_table: str, item: Mapping[str, Any]) -> dict[str, Any]:
    persisted = item.get("source_persisted_at")
    return {
        "source_system": source_system,
        "source_table": source_table,
        "source_record_id": item["source_record_id"],
        "occurred_at": item["occurred_at"].isoformat(),
        "source_persisted_at": None if persisted is None else persisted.isoformat(),
        "direction": item["direction"],
        "source_conversation_key": item["source_conversation_key"],
        "source_conversation_type": item["source_conversation_type"],
        "conversation_type": item["conversation_type"],
        "bot_id": item.get("bot_id"),
        "sender_id": item["sender_id"],
        "adapter_key": item.get("adapter_key"),
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _row_digest(row: Any) -> int:
    payload = json.dumps(
        _json_safe(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest(), "big")


def _consider_sample(
    samples: list[tuple[int, str, dict[str, Any]]],
    *,
    digest: int,
    item: dict[str, Any],
    limit: int,
) -> None:
    """Keep the lexically stable lowest-digest samples in bounded memory."""

    if limit == 0:
        return
    canonical = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    samples.append((digest, canonical, item))
    samples.sort(key=lambda entry: (entry[0], entry[1]))
    del samples[limit:]


def _manifest_hash(
    *,
    source_system: str,
    source_table: str,
    cutover_boundary: datetime,
    source_cutover_boundary: datetime,
    source_snapshot_id: str | None,
    source_schema_version: str | None,
    mapping_version: str | None,
    row_count: int,
    digest_sum: int,
    digest_xor: int,
) -> str:
    """Return an order-independent, constant-memory digest of every source row."""

    payload = {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "source_system": source_system,
        "source_table": source_table,
        "cutover_boundary": cutover_boundary.isoformat(),
        "source_cutover_boundary": source_cutover_boundary.isoformat(),
        "source_snapshot_id": source_snapshot_id,
        "source_schema_version": source_schema_version,
        "mapping_version": mapping_version,
        "row_count": row_count,
        "row_digest_sum": f"{digest_sum:064x}",
        "row_digest_xor": f"{digest_xor:064x}",
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def dry_run_legacy_rows(
    source_system: str,
    rows: Iterable[Mapping[str, Any]],
    cutover_boundary: str | datetime,
    *,
    source_snapshot_id: str | None = None,
    source_schema_version: str | None = None,
    mapping_version: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any]:
    """Validate exported legacy rows and return a deterministic, write-free manifest."""

    canonical_source = _canonical_source(source_system)
    source_table = LILY_SOURCE_TABLE if canonical_source == LILY_SOURCE_SYSTEM else NEKRO_SOURCE_TABLE
    boundary = _parse_cutover_boundary(cutover_boundary)
    expected_boundary = (
        LILY_CUTOVER_BOUNDARY
        if canonical_source == LILY_SOURCE_SYSTEM
        else NEKRO_CUTOVER_BOUNDARY
    )
    if boundary != expected_boundary:
        raise ValueError(
            f"cutover boundary for {canonical_source} must be {expected_boundary.isoformat()}"
        )
    normalize = _normalize_lily if canonical_source == LILY_SOURCE_SYSTEM else _normalize_nekro
    source_boundary = (
        boundary if canonical_source == LILY_SOURCE_SYSTEM else NEKRO_SOURCE_CUTOVER_BOUNDARY
    )

    total = 0
    excluded = 0
    duplicates = 0
    rejection_counts: Counter[str] = Counter()
    by_month: Counter[str] = Counter()
    by_conversation_type: Counter[str] = Counter()
    by_source_conversation_key: Counter[str] = Counter()
    by_direction: Counter[str] = Counter()
    by_bot_id: Counter[str] = Counter()
    by_adapter_key: Counter[str] = Counter()
    eligible_empty_text = 0
    eligible_occurred_at_min: datetime | None = None
    eligible_occurred_at_max: datetime | None = None
    if sample_limit < 0:
        raise ValueError("sample_limit must be non-negative")
    sample_eligible: list[tuple[int, str, dict[str, Any]]] = []
    sample_rejections: list[tuple[int, str, dict[str, Any]]] = []
    digest_sum = 0
    digest_xor = 0
    digest_modulus = 1 << 256

    with tempfile.TemporaryDirectory(prefix="superlily-history-identities-") as temp_dir:
        identity_path = Path(temp_dir) / "seen-source-identities.sqlite3"
        with closing(sqlite3.connect(identity_path)) as identity_db:
            identity_db.execute("PRAGMA journal_mode = OFF")
            identity_db.execute("PRAGMA synchronous = OFF")
            identity_db.execute(
                """
                CREATE TABLE candidates (
                    source_record_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    source_persisted_at TEXT,
                    direction TEXT NOT NULL,
                    source_conversation_key TEXT NOT NULL,
                    source_conversation_type TEXT,
                    conversation_type TEXT NOT NULL,
                    bot_id TEXT,
                    sender_id TEXT NOT NULL,
                    adapter_key TEXT,
                    text_is_empty INTEGER NOT NULL,
                    sample_digest TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 1
                )
                """
            )

            for raw_row in rows:
                total += 1
                digest = _row_digest(raw_row)
                digest_sum = (digest_sum + digest) % digest_modulus
                digest_xor ^= digest
                if not isinstance(raw_row, Mapping):
                    rejection = _RejectedRow("invalid_row", "row must be a JSON object")
                    normalized = None
                else:
                    try:
                        normalized = normalize(raw_row)
                    except _RejectedRow as exc:
                        rejection = exc
                        normalized = None

                if normalized is None:
                    rejection_counts[rejection.code] += 1
                    rejection_item = {
                        "source_record_id": (
                            str(raw_row.get("id")).strip()
                            if isinstance(raw_row, Mapping) and raw_row.get("id") is not None
                            else None
                        ),
                        "code": rejection.code,
                        "detail": rejection.detail,
                    }
                    _consider_sample(
                        sample_rejections,
                        digest=digest,
                        item=rejection_item,
                        limit=sample_limit,
                    )
                    continue

                digest_hex = f"{digest:064x}"
                try:
                    identity_db.execute(
                        """
                        INSERT INTO candidates (
                            source_record_id, occurred_at, source_persisted_at, direction,
                            source_conversation_key, source_conversation_type,
                            conversation_type, bot_id, sender_id, adapter_key, text_is_empty,
                            sample_digest
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            normalized["source_record_id"],
                            normalized["occurred_at"].isoformat(),
                            (
                                None
                                if normalized["source_persisted_at"] is None
                                else normalized["source_persisted_at"].isoformat()
                            ),
                            normalized["direction"],
                            normalized["source_conversation_key"],
                            normalized["source_conversation_type"],
                            normalized["conversation_type"],
                            normalized.get("bot_id"),
                            normalized["sender_id"],
                            normalized.get("adapter_key"),
                            int(normalized["text_is_empty"]),
                            digest_hex,
                        ),
                    )
                except sqlite3.IntegrityError:
                    identity_db.execute(
                        """
                        UPDATE candidates
                        SET occurrence_count = occurrence_count + 1,
                            sample_digest = MIN(sample_digest, ?)
                        WHERE source_record_id = ?
                        """,
                        (digest_hex, normalized["source_record_id"]),
                    )

            duplicate_groups = identity_db.execute(
                """
                SELECT source_record_id, occurrence_count, sample_digest
                FROM candidates
                WHERE occurrence_count > 1
                ORDER BY source_record_id
                """
            ).fetchall()
            for source_record_id, occurrence_count, sample_digest in duplicate_groups:
                duplicates += int(occurrence_count) - 1
                rejection_counts["duplicate_source_identity"] += int(occurrence_count)
                _consider_sample(
                    sample_rejections,
                    digest=int(str(sample_digest), 16),
                    item={
                        "source_record_id": str(source_record_id),
                        "code": "duplicate_source_identity",
                        "detail": (
                            f"source identity occurs {int(occurrence_count)} times; "
                            "the entire identity group is rejected"
                        ),
                    },
                    limit=sample_limit,
                )

            unique_candidates = identity_db.execute(
                """
                SELECT source_record_id, occurred_at, source_persisted_at, direction,
                       source_conversation_key, source_conversation_type,
                       conversation_type, bot_id, sender_id, text_is_empty,
                       adapter_key, sample_digest
                FROM candidates
                WHERE occurrence_count = 1
                """
            )
            for candidate in unique_candidates:
                (
                    source_record_id,
                    occurred_at_raw,
                    persisted_at_raw,
                    direction,
                    source_conversation_key,
                    source_conversation_type,
                    conversation_type,
                    bot_id,
                    sender_id,
                    text_is_empty,
                    adapter_key,
                    sample_digest,
                ) = candidate
                normalized = {
                    "source_record_id": str(source_record_id),
                    "occurred_at": datetime.fromisoformat(str(occurred_at_raw)),
                    "source_persisted_at": (
                        None
                        if persisted_at_raw is None
                        else datetime.fromisoformat(str(persisted_at_raw))
                    ),
                    "direction": str(direction),
                    "source_conversation_key": str(source_conversation_key),
                    "source_conversation_type": source_conversation_type,
                    "conversation_type": str(conversation_type),
                    "bot_id": bot_id,
                    "sender_id": str(sender_id),
                    "adapter_key": adapter_key,
                    "text_is_empty": bool(text_is_empty),
                }

                if normalized["occurred_at"] >= source_boundary:
                    excluded += 1
                    continue

                item = _manifest_item(canonical_source, source_table, normalized)
                by_month[normalized["occurred_at"].strftime("%Y-%m")] += 1
                by_conversation_type[normalized["conversation_type"] or "unknown"] += 1
                by_source_conversation_key[normalized["source_conversation_key"]] += 1
                by_direction[normalized["direction"]] += 1
                if normalized.get("bot_id") is not None:
                    by_bot_id[normalized["bot_id"]] += 1
                if normalized.get("adapter_key") is not None:
                    by_adapter_key[normalized["adapter_key"]] += 1
                eligible_empty_text += int(normalized["text_is_empty"])
                if (
                    eligible_occurred_at_min is None
                    or normalized["occurred_at"] < eligible_occurred_at_min
                ):
                    eligible_occurred_at_min = normalized["occurred_at"]
                if (
                    eligible_occurred_at_max is None
                    or normalized["occurred_at"] > eligible_occurred_at_max
                ):
                    eligible_occurred_at_max = normalized["occurred_at"]
                _consider_sample(
                    sample_eligible,
                    digest=int(str(sample_digest), 16),
                    item=item,
                    limit=sample_limit,
                )

    return {
        "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "source_system": canonical_source,
        "source_table": source_table,
        "source_snapshot_id": source_snapshot_id,
        "source_schema_version": source_schema_version,
        "mapping_version": mapping_version,
        "cutover_boundary": boundary.isoformat(),
        "source_cutover_boundary": source_boundary.isoformat(),
        "total": total,
        "eligible": sum(by_month.values()),
        "excluded_at_or_after_cutover": excluded,
        "rejected": sum(rejection_counts.values()),
        "duplicates": duplicates,
        "by_month": dict(sorted(by_month.items())),
        "by_conversation_type": dict(sorted(by_conversation_type.items())),
        "by_source_conversation_key": dict(sorted(by_source_conversation_key.items())),
        "by_direction": dict(sorted(by_direction.items())),
        "by_bot_id": dict(sorted(by_bot_id.items())),
        "by_adapter_key": dict(sorted(by_adapter_key.items())),
        "eligible_empty_text": eligible_empty_text,
        "eligible_occurred_at_min": (
            None if eligible_occurred_at_min is None else eligible_occurred_at_min.isoformat()
        ),
        "eligible_occurred_at_max": (
            None if eligible_occurred_at_max is None else eligible_occurred_at_max.isoformat()
        ),
        "by_rejection_code": dict(sorted(rejection_counts.items())),
        "manifest_sha256": _manifest_hash(
            source_system=canonical_source,
            source_table=source_table,
            cutover_boundary=boundary,
            source_cutover_boundary=source_boundary,
            source_snapshot_id=source_snapshot_id,
            source_schema_version=source_schema_version,
            mapping_version=mapping_version,
            row_count=total,
            digest_sum=digest_sum,
            digest_xor=digest_xor,
        ),
        "sample_eligible": [item for _, _, item in sample_eligible],
        "sample_rejections": [item for _, _, item in sample_rejections],
        "writes": 0,
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if str(path) == "-":
        import sys

        handle = sys.stdin
        close_handle = False
    else:
        handle = path.open("r", encoding="utf-8")
        close_handle = True
    try:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number} is not valid JSON") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            yield value
    finally:
        if close_handle:
            handle.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Write-free historical import validation.")
    subparsers = parser.add_subparsers(dest="mode")
    event_parser = subparsers.add_parser("eventin", help="lint legacy EventIn-shaped candidates")
    event_parser.add_argument("jsonl", type=Path)
    legacy_parser = subparsers.add_parser("legacy", help="build an H2 legacy-row dry-run manifest")
    legacy_parser.add_argument("--source", required=True, choices=("lily", "nekro"))
    legacy_parser.add_argument("--cutover", required=True)
    legacy_parser.add_argument("--jsonl", required=True, type=Path)
    legacy_parser.add_argument("--snapshot-id", required=True)
    legacy_parser.add_argument("--source-schema-version", required=True)
    legacy_parser.add_argument("--mapping-version", required=True)
    legacy_parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Preserve the historic ``python -m ... candidates.jsonl`` invocation.
    if argv is None:
        import sys

        args_list = list(sys.argv[1:])
    else:
        args_list = list(argv)
    if args_list and args_list[0] not in {"eventin", "legacy", "-h", "--help"}:
        args_list.insert(0, "eventin")
    parser = _build_parser()
    args = parser.parse_args(args_list)
    if args.mode == "legacy":
        report = dry_run_legacy_rows(
            args.source,
            iter_jsonl(args.jsonl),
            args.cutover,
            source_snapshot_id=args.snapshot_id,
            source_schema_version=args.source_schema_version,
            mapping_version=args.mapping_version,
        )
    elif args.mode == "eventin":
        report = dry_run_payloads(iter_jsonl(args.jsonl))
    else:
        parser.error("a mode is required")
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = getattr(args, "output", None)
    if output is None:
        print(rendered, end="")
    else:
        output.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
