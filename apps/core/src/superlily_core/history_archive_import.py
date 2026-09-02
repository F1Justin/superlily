"""Checkpointed PostgreSQL writer for frozen legacy-history JSONL snapshots."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

import asyncpg
from sqlalchemy.engine import make_url

from .history_import import (
    LEGACY_SOURCE_CHOICES,
    _canonical_source,
    _RejectedRow,
    dry_run_legacy_rows,
    iter_jsonl,
    normalize_legacy_row,
    source_profile,
)

_MAPPING_VERSION = "history-map-v1"
_STAGE_COLUMNS = (
    "source_system",
    "source_table",
    "source_record_id",
    "legacy_message_id",
    "occurred_at",
    "source_persisted_at",
    "mapping_id",
    "mapping_version",
    "bot_id",
    "source_conversation_key",
    "source_conversation_type",
    "platform",
    "conversation_type",
    "conversation_id",
    "sender_id",
    "sender_name",
    "direction",
    "platform_message_id",
    "content_text",
    "segments_json",
    "reply_hint_json",
    "raw_fields_json",
    "raw_storage_ref",
    "parse_warning",
    "payload_sha256",
    "mapping_metadata_json",
)
_NEKRO_CHAT_KEY = re.compile(r"^onebot_v11-(group|private)_(.+)$")


@dataclass(frozen=True, slots=True)
class ImportSelection:
    scope: str
    conversation_key: str | None = None
    month: str | None = None
    max_rows: int | None = None

    @property
    def key(self) -> str:
        if self.scope == "sample":
            return f"sample:{self.conversation_key}:{self.max_rows}"
        if self.scope == "month":
            return f"month:{self.month}"
        return "full"

    def includes(self, item: Mapping[str, Any], selected_count: int) -> bool:
        if self.scope == "full":
            return True
        if self.scope == "month":
            return item["occurred_at"].strftime("%Y-%m") == self.month
        return item["source_conversation_key"] == self.conversation_key and selected_count < int(self.max_rows or 0)


@dataclass(frozen=True, slots=True)
class ArchiveRecord:
    source_system: str
    source_table: str
    source_record_id: str
    legacy_message_id: str
    occurred_at: datetime
    source_persisted_at: datetime | None
    mapping_id: str
    mapping_version: str
    bot_id: str | None
    source_conversation_key: str
    source_conversation_type: str | None
    platform: str
    conversation_type: str
    conversation_id: str
    sender_id: str
    sender_name: str | None
    direction: str
    platform_message_id: str | None
    content_text: str
    segments_json: str
    reply_hint_json: str
    raw_fields_json: str
    raw_storage_ref: str
    parse_warning: str | None
    payload_sha256: str
    mapping_metadata_json: str

    def stage_tuple(self) -> tuple[Any, ...]:
        return tuple(getattr(self, column) for column in _STAGE_COLUMNS)


def _json_canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise RuntimeError("database JSON value is not an object")
    return dict(value)


def _sanitize(value: Any, warnings: list[str], path: str = "$") -> Any:
    if isinstance(value, str):
        if "\x00" in value:
            warnings.append(f"nul_replaced:{path}")
            return value.replace("\x00", "\ufffd")
        return value
    if isinstance(value, list):
        return [_sanitize(item, warnings, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        return {str(key): _sanitize(item, warnings, f"{path}.{key}") for key, item in value.items()}
    return value


def _parse_json_field(
    value: Any,
    *,
    expected: type[list[Any]] | type[dict[str, Any]],
    field: str,
    warnings: list[str],
) -> list[Any] | dict[str, Any]:
    if value is None or value == "":
        return expected()
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            warnings.append(f"invalid_json:{field}")
            return expected()
    if not isinstance(parsed, expected):
        warnings.append(f"unexpected_json_type:{field}")
        return expected()
    return _sanitize(parsed, warnings, f"$.{field}")


def _clean_scalar(value: Any, warnings: list[str], field: str) -> str | None:
    if value is None:
        return None
    result = str(value)
    if "\x00" in result:
        warnings.append(f"nul_replaced:{field}")
        result = result.replace("\x00", "\ufffd")
    return result


def _reply_hint_lily(segments: list[Any]) -> dict[str, Any]:
    for segment in segments:
        if not isinstance(segment, Mapping) or str(segment.get("type", "")).lower() != "reply":
            continue
        data = segment.get("data")
        if isinstance(data, Mapping):
            target = data.get("id") or data.get("message_id")
            if target is not None and str(target).strip():
                return {
                    "scope": "source_conversation_and_bot",
                    "target_platform_message_id": str(target).strip(),
                }
    return {}


def _reply_hint_nekro(ext_data: Mapping[str, Any]) -> dict[str, Any]:
    target = ext_data.get("ref_msg_id")
    if target is None or not str(target).strip():
        return {}
    return {
        "scope": "source_chat_key",
        "target_platform_message_id": str(target).strip(),
        "target_source_conversation_key": str(ext_data.get("ref_chat_key") or "").strip() or None,
        "target_sender_id": str(ext_data.get("ref_sender_id") or "").strip() or None,
    }


def _deterministic_id(kind: str, *parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "superlily:archive:" + kind + ":" + "|".join(parts)))


def _conversation_target(
    source_system: str,
    row: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> tuple[str, str]:
    profile = source_profile(source_system)
    if profile.normalizer == "lily":
        value = row.get("conversation_id", row.get("scene_id"))
        if value is None or not str(value).strip():
            raise _RejectedRow("invalid_row", "scene_id/conversation_id is required")
        return str(value).strip(), "session_scene_join"
    if profile.normalizer == "nekro":
        chat_key = str(row.get("chat_key") or "").strip()
        match = _NEKRO_CHAT_KEY.fullmatch(chat_key)
        if not match:
            raise _RejectedRow(
                "invalid_row",
                "chat_key does not match onebot_v11 conversation grammar",
            )
        return match.group(2), "chat_key_grammar"
    return str(normalized["conversation_id"]), "sqlite_direct_ids"


def build_archive_record(
    source_system: str,
    row: Mapping[str, Any],
    *,
    source_snapshot_id: str,
    mapping_version: str = _MAPPING_VERSION,
) -> ArchiveRecord:
    source = _canonical_source(source_system)
    profile = source_profile(source)
    normalized = normalize_legacy_row(source, row)
    warnings: list[str] = []
    conversation_id, mapping_reason = _conversation_target(source, row, normalized)
    source_table = profile.source_table

    if profile.normalizer == "lily":
        segments = _parse_json_field(row.get("message"), expected=list, field="message", warnings=warnings)
        reply_hint = _reply_hint_lily(segments)
        content_text = _clean_scalar(row.get("plain_text"), warnings, "plain_text") or ""
        raw_fields = {
            "type": row.get("type"),
            "session_persist_id": row.get("session_persist_id"),
            "scene_id": row.get("scene_id", row.get("conversation_id")),
            "scene_type": row.get("scene_type"),
        }
        mapping_metadata = {
            "bot_id": normalized.get("bot_id"),
            "reason": mapping_reason,
        }
    elif profile.normalizer == "nekro":
        segments = _parse_json_field(
            row.get("content_data"),
            expected=list,
            field="content_data",
            warnings=warnings,
        )
        ext_data = _parse_json_field(row.get("ext_data"), expected=dict, field="ext_data", warnings=warnings)
        reply_hint = _reply_hint_nekro(ext_data)
        content_text = _clean_scalar(row.get("content_text"), warnings, "content_text") or ""
        raw_fields = {
            "adapter_key": row.get("adapter_key"),
            "platform_userid": row.get("platform_userid"),
            "sender_nickname": row.get("sender_nickname"),
            "is_tome": row.get("is_tome"),
            "raw_cq_code": row.get("raw_cq_code"),
            "ext_data": ext_data,
            "update_time": row.get("update_time"),
        }
        mapping_metadata = {
            "adapter_key": normalized.get("adapter_key"),
            "reason": mapping_reason,
        }
    else:
        segments = _parse_json_field(
            row.get("message"),
            expected=list,
            field="message",
            warnings=warnings,
        )
        reply_hint = _reply_hint_lily(segments)
        content_text = _clean_scalar(row.get("plain_text"), warnings, "plain_text") or ""
        raw_fields = {
            "type": row.get("type"),
            "detail_type": row.get("detail_type"),
            "group_id": row.get("group_id"),
            "bot_type": row.get("bot_type"),
            "guild_id": row.get("guild_id"),
            "channel_id": row.get("channel_id"),
        }
        mapping_metadata = {
            "bot_id": normalized.get("bot_id"),
            "reason": mapping_reason,
            "source_schema_version": profile.source_schema_version,
        }

    source_record_id = normalized["source_record_id"]
    legacy_message_id = _deterministic_id("message", source, source_table, source_record_id)
    mapping_id = _deterministic_id("mapping", source, normalized["source_conversation_key"], mapping_version)
    sanitized_raw = _sanitize(raw_fields, warnings, "$.raw_fields")
    parse_warning = ";".join(sorted(set(warnings)))[:2048] or None
    payload = {
        "source_system": source,
        "source_table": source_table,
        "source_record_id": source_record_id,
        "legacy_message_id": legacy_message_id,
        "occurred_at": normalized["occurred_at"].isoformat(),
        "source_persisted_at": (
            None if normalized["source_persisted_at"] is None else normalized["source_persisted_at"].isoformat()
        ),
        "bot_id": normalized.get("bot_id") or _clean_scalar(row.get("bot_id"), warnings, "bot_id"),
        "source_conversation_key": normalized["source_conversation_key"],
        "source_conversation_type": normalized["source_conversation_type"],
        "platform": "qq",
        "conversation_type": normalized["conversation_type"],
        "conversation_id": conversation_id,
        "sender_id": normalized["sender_id"],
        "sender_name": _clean_scalar(row.get("sender_name"), warnings, "sender_name"),
        "direction": normalized["direction"],
        "platform_message_id": _clean_scalar(row.get("message_id"), warnings, "message_id"),
        "content_text": content_text,
        "segments": segments,
        "reply_hint": reply_hint,
        "raw_fields": sanitized_raw,
        "raw_storage_ref": f"snapshot://{source_snapshot_id}/{source_table}/{source_record_id}",
        "parse_warning": parse_warning,
    }
    payload_sha256 = hashlib.sha256(_json_canonical(payload).encode("utf-8")).hexdigest()
    return ArchiveRecord(
        source_system=source,
        source_table=source_table,
        source_record_id=source_record_id,
        legacy_message_id=legacy_message_id,
        occurred_at=normalized["occurred_at"],
        source_persisted_at=normalized["source_persisted_at"],
        mapping_id=mapping_id,
        mapping_version=mapping_version,
        bot_id=payload["bot_id"],
        source_conversation_key=normalized["source_conversation_key"],
        source_conversation_type=normalized["source_conversation_type"],
        platform="qq",
        conversation_type=normalized["conversation_type"],
        conversation_id=conversation_id,
        sender_id=normalized["sender_id"],
        sender_name=payload["sender_name"],
        direction=normalized["direction"],
        platform_message_id=payload["platform_message_id"],
        content_text=content_text,
        segments_json=_json_canonical(segments),
        reply_hint_json=_json_canonical(reply_hint),
        raw_fields_json=_json_canonical(sanitized_raw),
        raw_storage_ref=payload["raw_storage_ref"],
        parse_warning=parse_warning,
        payload_sha256=payload_sha256,
        mapping_metadata_json=_json_canonical(mapping_metadata),
    )


def verify_manifest(source: str, jsonl_path: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    canonical_source = _canonical_source(source)
    profile = source_profile(canonical_source)
    expected_schema = profile.source_schema_version
    if manifest.get("source_system") != canonical_source:
        raise ValueError("manifest source_system does not match --source")
    if manifest.get("source_schema_version") != expected_schema:
        raise ValueError(f"manifest source_schema_version must be {expected_schema}")
    if manifest.get("mapping_version") != _MAPPING_VERSION:
        raise ValueError(f"manifest mapping_version must be {_MAPPING_VERSION}")
    computed = dry_run_legacy_rows(
        canonical_source,
        iter_jsonl(jsonl_path),
        profile.cutover_boundary,
        source_snapshot_id=str(manifest.get("source_snapshot_id") or ""),
        source_schema_version=expected_schema,
        mapping_version=_MAPPING_VERSION,
    )
    compared = (
        "manifest_schema_version",
        "source_system",
        "source_table",
        "source_snapshot_id",
        "source_schema_version",
        "mapping_version",
        "cutover_boundary",
        "source_cutover_boundary",
        "total",
        "eligible",
        "excluded_at_or_after_cutover",
        "rejected",
        "duplicates",
        "manifest_sha256",
    )
    mismatches = [field for field in compared if computed.get(field) != manifest.get(field)]
    if mismatches:
        raise ValueError("manifest mismatch: " + ", ".join(mismatches))
    return computed


def _asyncpg_dsn(database_url: str) -> str:
    parsed = make_url(database_url)
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("archive target must use a postgresql+asyncpg URL")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False)


async def _ensure_target(conn: asyncpg.Connection) -> None:
    database = await conn.fetchval("SELECT current_database()")
    if not database:
        raise RuntimeError("target database could not be identified")
    revision = await conn.fetchval("SELECT version_num FROM alembic_version")
    if revision not in {
        "0026_history_timeline_export",
        "0027_name_observation_history",
        "0028_sqlite_chatrecorder_archive",
    }:
        raise RuntimeError(
            "archive target must be at a supported history revision, "
            f"got {revision}"
        )
    required = await conn.fetchval(
        """
        SELECT count(*) = 4
        FROM information_schema.tables
        WHERE table_schema = 'archive'
          AND table_name IN (
              'import_batches', 'conversation_mappings',
              'legacy_messages', 'source_message_identities'
          )
        """
    )
    if not required:
        raise RuntimeError("archive schema is incomplete")


async def _ensure_batch(
    conn: asyncpg.Connection,
    *,
    manifest: Mapping[str, Any],
) -> tuple[str, dict[str, Any]]:
    source = str(manifest["source_system"])
    snapshot = str(manifest["source_snapshot_id"])
    batch_id = _deterministic_id("batch", source, snapshot)
    await conn.execute(
        """
        INSERT INTO archive.import_batches (
            id, source_system, source_snapshot_id, source_schema_version,
            mapping_version, cutover_boundary, status, started_at,
            source_row_count, rejected_count, duplicate_count,
            manifest_hash, content_hash, checkpoint_json, error_summary_json
        ) VALUES (
            $1, $2, $3, $4, $5, $6, 'running', now(),
            $7, 0, $8, $9, $9, '{}'::jsonb, $10::jsonb
        )
        ON CONFLICT (source_system, source_snapshot_id) DO NOTHING
        """,
        batch_id,
        source,
        snapshot,
        manifest["source_schema_version"],
        manifest["mapping_version"],
        datetime.fromisoformat(str(manifest["cutover_boundary"])),
        int(manifest["total"]),
        int(manifest["duplicates"]),
        manifest["manifest_sha256"],
        _json_canonical(
            {
                "manifest_rejections": manifest.get("by_rejection_code", {}),
                "excluded_at_or_after_cutover": manifest.get("excluded_at_or_after_cutover", 0),
            }
        ),
    )
    row = await conn.fetchrow(
        "SELECT * FROM archive.import_batches WHERE source_system=$1 AND source_snapshot_id=$2",
        source,
        snapshot,
    )
    if row is None:
        raise RuntimeError("archive batch was not created")
    for field in ("source_schema_version", "mapping_version", "manifest_hash"):
        if str(row[field]) != str(manifest[field if field != "manifest_hash" else "manifest_sha256"]):
            raise RuntimeError(f"existing batch {field} does not match manifest")
    if int(row["source_row_count"]) != int(manifest["total"]):
        raise RuntimeError("existing batch source_row_count does not match manifest")
    checkpoint = _json_object(row["checkpoint_json"])
    return str(row["id"]), checkpoint


async def _create_stage(conn: asyncpg.Connection) -> None:
    await conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS history_import_stage (
            source_system text NOT NULL,
            source_table text NOT NULL,
            source_record_id text NOT NULL,
            legacy_message_id text NOT NULL,
            occurred_at timestamptz NOT NULL,
            source_persisted_at timestamptz,
            mapping_id text NOT NULL,
            mapping_version text NOT NULL,
            bot_id text,
            source_conversation_key text NOT NULL,
            source_conversation_type text,
            platform text NOT NULL,
            conversation_type text NOT NULL,
            conversation_id text NOT NULL,
            sender_id text NOT NULL,
            sender_name text,
            direction text NOT NULL,
            platform_message_id text,
            content_text text,
            segments_json text NOT NULL,
            reply_hint_json text NOT NULL,
            raw_fields_json text NOT NULL,
            raw_storage_ref text NOT NULL,
            parse_warning text,
            payload_sha256 text NOT NULL,
            mapping_metadata_json text NOT NULL
        ) ON COMMIT PRESERVE ROWS
        """
    )


async def _write_chunk(
    conn: asyncpg.Connection,
    *,
    batch_id: str,
    records: list[ArchiveRecord],
) -> tuple[int, int]:
    if not records:
        return 0, 0
    async with conn.transaction():
        await conn.execute("TRUNCATE history_import_stage")
        await conn.copy_records_to_table(
            "history_import_stage",
            records=[item.stage_tuple() for item in records],
            columns=_STAGE_COLUMNS,
        )
        conflict = await conn.fetchrow(
            """
            SELECT s.source_record_id, i.state, i.payload_sha256 AS existing_hash,
                   s.payload_sha256 AS incoming_hash
            FROM history_import_stage s
            JOIN archive.source_message_identities i
              USING (source_system, source_table, source_record_id)
            WHERE i.state <> 'imported'
               OR i.payload_sha256 IS DISTINCT FROM s.payload_sha256
            LIMIT 1
            """
        )
        if conflict:
            raise RuntimeError(
                f"source identity conflict for {conflict['source_record_id']}: state={conflict['state']}"
            )
        await conn.execute(
            """
            INSERT INTO archive.conversation_mappings (
                id, source_system, source_conversation_key,
                source_conversation_type, platform, conversation_type,
                conversation_id, mapping_version, mapping_status, active,
                mapping_reason, source_metadata_json
            )
            SELECT DISTINCT ON (source_system, source_conversation_key)
                mapping_id, source_system, source_conversation_key,
                source_conversation_type, platform, conversation_type,
                conversation_id, mapping_version, 'mapped', true,
                'frozen H2 source mapping', mapping_metadata_json::jsonb
            FROM history_import_stage
            ORDER BY source_system, source_conversation_key, source_record_id
            ON CONFLICT (source_system, source_conversation_key, mapping_version) DO NOTHING
            """
        )
        mapping_conflict = await conn.fetchrow(
            """
            SELECT s.source_conversation_key
            FROM history_import_stage s
            JOIN archive.conversation_mappings m
              ON m.source_system=s.source_system
             AND m.source_conversation_key=s.source_conversation_key
             AND m.mapping_version=s.mapping_version
            WHERE m.mapping_status <> 'mapped'
               OR m.platform IS DISTINCT FROM s.platform
               OR m.conversation_type IS DISTINCT FROM s.conversation_type
               OR m.conversation_id IS DISTINCT FROM s.conversation_id
            LIMIT 1
            """
        )
        if mapping_conflict:
            raise RuntimeError(f"conversation mapping conflict for {mapping_conflict['source_conversation_key']}")
        await conn.execute(
            """
            INSERT INTO archive.legacy_messages (
                id, source_system, source_table, source_record_id,
                import_batch_id, mapping_version, bot_id,
                source_conversation_key, source_conversation_type,
                platform, conversation_type, conversation_id,
                sender_id, sender_name, direction, occurred_at,
                source_persisted_at, platform_message_id, content_text,
                segments_json, reply_hint_json, raw_fields_json,
                raw_storage_ref, parse_warning
            )
            SELECT
                s.legacy_message_id, s.source_system, s.source_table,
                s.source_record_id, $1, s.mapping_version, s.bot_id,
                s.source_conversation_key, s.source_conversation_type,
                s.platform, s.conversation_type, s.conversation_id,
                s.sender_id, s.sender_name, s.direction, s.occurred_at,
                s.source_persisted_at, s.platform_message_id, s.content_text,
                s.segments_json::jsonb, s.reply_hint_json::jsonb,
                s.raw_fields_json::jsonb, s.raw_storage_ref, s.parse_warning
            FROM history_import_stage s
            LEFT JOIN archive.source_message_identities i
              USING (source_system, source_table, source_record_id)
            WHERE i.source_record_id IS NULL
            ON CONFLICT DO NOTHING
            """,
            batch_id,
        )
        inserted = await conn.fetch(
            """
            INSERT INTO archive.source_message_identities (
                source_system, source_table, source_record_id,
                import_batch_id, legacy_message_id, occurred_at,
                payload_sha256, state
            )
            SELECT s.source_system, s.source_table, s.source_record_id,
                   $1, s.legacy_message_id, s.occurred_at,
                   s.payload_sha256, 'imported'
            FROM history_import_stage s
            LEFT JOIN archive.source_message_identities i
              USING (source_system, source_table, source_record_id)
            WHERE i.source_record_id IS NULL
            ON CONFLICT DO NOTHING
            RETURNING source_record_id
            """,
            batch_id,
        )
        valid_count = await conn.fetchval(
            """
            SELECT count(*)
            FROM history_import_stage s
            JOIN archive.source_message_identities i
              USING (source_system, source_table, source_record_id)
            WHERE i.state='imported' AND i.payload_sha256=s.payload_sha256
            """
        )
        if int(valid_count) != len(records):
            raise RuntimeError("chunk did not finish with one matching imported identity per row")
        await conn.execute(
            """
            UPDATE archive.import_batches
            SET imported_count=imported_count+$2, updated_at=now()
            WHERE id=$1
            """,
            batch_id,
            len(inserted),
        )
        return len(inserted), len(records) - len(inserted)


async def _record_rejection(
    conn: asyncpg.Connection,
    *,
    batch_id: str,
    source_system: str,
    source_table: str,
    source_record_id: str | None,
    error_code: str,
    payload_sha256: str,
) -> bool:
    if not source_record_id:
        return False
    async with conn.transaction():
        result = await conn.execute(
            """
            INSERT INTO archive.source_message_identities (
                source_system, source_table, source_record_id,
                import_batch_id, payload_sha256, state, error_code
            ) VALUES ($1, $2, $3, $4, $5, 'rejected', $6)
            ON CONFLICT (source_system, source_table, source_record_id) DO NOTHING
            """,
            source_system,
            source_table,
            source_record_id,
            batch_id,
            payload_sha256,
            error_code,
        )
        inserted = result.endswith("1")
        if inserted:
            await conn.execute(
                """
                UPDATE archive.import_batches
                SET rejected_count=rejected_count+1, updated_at=now()
                WHERE id=$1
                """,
                batch_id,
            )
        return inserted


async def apply_archive_import(
    *,
    database_url: str,
    source: str,
    jsonl_path: Path,
    manifest: Mapping[str, Any],
    selection: ImportSelection,
    chunk_size: int = 5000,
) -> dict[str, Any]:
    if chunk_size < 1 or chunk_size > 50_000:
        raise ValueError("chunk_size must be between 1 and 50000")
    verified = verify_manifest(source, jsonl_path, manifest)
    if int(verified["duplicates"]) != 0:
        raise ValueError("archive apply requires a manifest with zero duplicate source identities")
    canonical_source = str(verified["source_system"])
    source_table = str(verified["source_table"])
    source_boundary = source_profile(canonical_source).source_cutover_boundary
    conn = await asyncpg.connect(_asyncpg_dsn(database_url), command_timeout=120)
    batch_id: str | None = None
    try:
        await _ensure_target(conn)
        batch_id, checkpoint = await _ensure_batch(conn, manifest=verified)
        scopes = dict(checkpoint.get("scopes") or {})
        prior = dict(scopes.get(selection.key) or {})
        if prior.get("status") == "completed":
            return {
                "batch_id": batch_id,
                "scope": selection.key,
                "status": "completed",
                "inserted": 0,
                "existing": int(prior.get("selected", 0)),
                "writes": 0,
            }
        await conn.execute(
            """
            UPDATE archive.import_batches
            SET status='running', finished_at=NULL, updated_at=now()
            WHERE id=$1
            """,
            batch_id,
        )
        await _create_stage(conn)
        last_line = int(prior.get("last_line", 0))
        selected_count = int(prior.get("selected", 0))
        inserted_count = int(prior.get("inserted", 0))
        existing_count = int(prior.get("existing", 0))
        rejected_unledgered = int(prior.get("rejected_unledgered", 0))
        chunk: list[ArchiveRecord] = []
        chunk_last_line = last_line

        async def flush() -> None:
            nonlocal inserted_count, existing_count, chunk
            inserted, existing = await _write_chunk(conn, batch_id=batch_id, records=chunk)
            inserted_count += inserted
            existing_count += existing
            chunk = []

        for line_number, row in enumerate(iter_jsonl(jsonl_path), start=1):
            if line_number <= last_line:
                continue
            if selection.scope == "sample" and selected_count >= int(selection.max_rows or 0):
                break
            chunk_last_line = line_number
            try:
                normalized = normalize_legacy_row(canonical_source, row)
                if normalized["occurred_at"] >= source_boundary:
                    continue
                if not selection.includes(normalized, selected_count):
                    continue
                record = build_archive_record(
                    canonical_source,
                    row,
                    source_snapshot_id=str(verified["source_snapshot_id"]),
                    mapping_version=str(verified["mapping_version"]),
                )
                chunk.append(record)
                selected_count += 1
            except _RejectedRow as exc:
                if selection.scope != "full":
                    continue
                source_record_id = str(row.get("id")).strip() if row.get("id") is not None else None
                digest = hashlib.sha256(_json_canonical(row).encode("utf-8")).hexdigest()
                ledgered = await _record_rejection(
                    conn,
                    batch_id=batch_id,
                    source_system=canonical_source,
                    source_table=source_table,
                    source_record_id=source_record_id,
                    error_code=exc.code,
                    payload_sha256=digest,
                )
                if not ledgered and not source_record_id:
                    rejected_unledgered += 1
            if len(chunk) >= chunk_size:
                await flush()
                scope_state = {
                    "status": "running",
                    "last_line": chunk_last_line,
                    "selected": selected_count,
                    "inserted": inserted_count,
                    "existing": existing_count,
                    "rejected_unledgered": rejected_unledgered,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                scopes[selection.key] = scope_state
                await _update_checkpoint(conn, batch_id, scopes, rejected_unledgered)

        await flush()
        scope_state = {
            "status": "completed",
            "last_line": chunk_last_line,
            "selected": selected_count,
            "inserted": inserted_count,
            "existing": existing_count,
            "rejected_unledgered": rejected_unledgered,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        scopes[selection.key] = scope_state
        await _update_checkpoint(
            conn,
            batch_id,
            scopes,
            rejected_unledgered,
            reconcile=True,
        )
        if selection.scope == "full":
            if selected_count != int(verified["eligible"]):
                raise RuntimeError(
                    "full scope selected count does not match manifest eligible count: "
                    f"{selected_count} != {verified['eligible']}"
                )
            final_counts = await conn.fetchrow(
                """
                SELECT imported_count, rejected_count
                FROM archive.import_batches
                WHERE id=$1
                """,
                batch_id,
            )
            if int(final_counts["imported_count"]) != int(verified["eligible"]):
                raise RuntimeError("full scope did not import every manifest-eligible identity")
            if int(final_counts["rejected_count"]) != int(verified["rejected"]):
                raise RuntimeError("full scope rejection count does not match manifest")
            await conn.execute(
                """
                UPDATE archive.import_batches
                SET status='completed', finished_at=now(), updated_at=now()
                WHERE id=$1
                """,
                batch_id,
            )
            await conn.execute(
                """
                ANALYZE archive.legacy_messages,
                        archive.source_message_identities,
                        archive.conversation_mappings,
                        archive.import_batches
                """
            )
        return {
            "batch_id": batch_id,
            "scope": selection.key,
            "status": "completed",
            "selected": selected_count,
            "inserted": inserted_count,
            "existing": existing_count,
            "rejected_unledgered": rejected_unledgered,
            "writes": inserted_count,
        }
    except Exception as exc:
        if batch_id is not None:
            try:
                await conn.execute(
                    """
                    UPDATE archive.import_batches
                    SET status='failed', finished_at=now(), updated_at=now(),
                        error_summary_json = error_summary_json || $2::jsonb
                    WHERE id=$1
                    """,
                    batch_id,
                    _json_canonical({"last_failure": type(exc).__name__}),
                )
            except Exception:
                pass
        raise
    finally:
        await conn.close()


async def _update_checkpoint(
    conn: asyncpg.Connection,
    batch_id: str,
    scopes: Mapping[str, Any],
    rejected_unledgered: int,
    *,
    reconcile: bool = False,
) -> None:
    await conn.execute(
        """
        UPDATE archive.import_batches
        SET checkpoint_json=$2::jsonb, updated_at=now()
        WHERE id=$1
        """,
        batch_id,
        _json_canonical({"scopes": scopes}),
    )
    if reconcile:
        await _reconcile_batch_counts(conn, batch_id, rejected_unledgered)


async def _reconcile_batch_counts(
    conn: asyncpg.Connection,
    batch_id: str,
    rejected_unledgered: int,
) -> None:
    await conn.execute(
        """
        UPDATE archive.import_batches AS batch
        SET imported_count=counts.imported,
            rejected_count=counts.rejected+$2,
            updated_at=now()
        FROM (
            SELECT count(*) FILTER (WHERE state='imported') AS imported,
                   count(*) FILTER (WHERE state='rejected') AS rejected
            FROM archive.source_message_identities
            WHERE import_batch_id=$1
        ) AS counts
        WHERE batch.id=$1
        """,
        batch_id,
        rejected_unledgered,
    )


def _selection_from_args(args: argparse.Namespace) -> ImportSelection:
    if args.scope == "sample":
        if not args.conversation_key or not args.max_rows:
            raise ValueError("sample scope requires --conversation-key and --max-rows")
        return ImportSelection("sample", conversation_key=args.conversation_key, max_rows=args.max_rows)
    if args.scope == "month":
        if not args.month or not re.fullmatch(r"\d{4}-\d{2}", args.month):
            raise ValueError("month scope requires --month YYYY-MM")
        return ImportSelection("month", month=args.month)
    return ImportSelection("full")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Checkpointed H2 archive writer.")
    parser.add_argument("--source", required=True, choices=LEGACY_SOURCE_CHOICES)
    parser.add_argument("--jsonl", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--scope", required=True, choices=("sample", "month", "full"))
    parser.add_argument("--conversation-key")
    parser.add_argument("--month")
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--chunk-size", type=int, default=5000)
    parser.add_argument("--write-archive", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.write_archive:
        raise SystemExit("refusing archive write without --write-archive")
    database_url = os.getenv("SUPERLILY_DATABASE_URL")
    if not database_url:
        raise SystemExit("SUPERLILY_DATABASE_URL is required")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    selection = _selection_from_args(args)
    report = asyncio.run(
        apply_archive_import(
            database_url=database_url,
            source=args.source,
            jsonl_path=args.jsonl,
            manifest=manifest,
            selection=selection,
            chunk_size=args.chunk_size,
        )
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
