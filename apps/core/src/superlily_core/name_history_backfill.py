from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from .settings import DEFAULT_DATABASE_URL


HEAD_REVISION = "0028_sqlite_chatrecorder_archive"


def _uuid(*parts: object) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            ":".join(("superlily-name-backfill", *(str(p) for p in parts))),
        )
    )


def _async_url(value: str) -> str:
    if value.startswith("postgresql://"):
        return "postgresql+asyncpg://" + value.removeprefix("postgresql://")
    return value


async def _require_head(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != HEAD_REVISION:
        raise RuntimeError(f"name-history target must be at {HEAD_REVISION}, got {revision}")


async def _run_sql_scope(
    engine: AsyncEngine,
    *,
    scope: str,
    snapshot_id: str,
    statements: tuple[str, ...],
    parameters: dict[str, Any],
) -> dict[str, int | str]:
    batch_id = _uuid("batch", scope, snapshot_id)
    async with engine.begin() as connection:
        existing = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT status, selected_count, written_count, existing_count
                    FROM name_observation_backfill_batches
                    WHERE source_system = 'superlily.name_history_backfill'
                      AND source_scope = :scope
                      AND source_snapshot_id = :snapshot_id
                    """
                    ),
                    {"scope": scope, "snapshot_id": snapshot_id},
                )
            )
            .mappings()
            .first()
        )
        if existing is not None and existing["status"] == "completed":
            return {"scope": scope, **dict(existing)}
        await connection.execute(
            text(
                """
                INSERT INTO name_observation_backfill_batches (
                    id, source_system, source_scope, source_snapshot_id, status,
                    cursor_json, selected_count, written_count, existing_count,
                    started_at, updated_at
                ) VALUES (
                    :id, 'superlily.name_history_backfill', :scope, :snapshot_id,
                    'running', '{}'::jsonb, 0, 0, 0, now(), now()
                )
                ON CONFLICT (source_system, source_scope, source_snapshot_id)
                DO UPDATE SET status = 'running', error_summary = NULL, updated_at = now()
                """
            ),
            {"id": batch_id, "scope": scope, "snapshot_id": snapshot_id},
        )

    written = 0
    try:
        for statement in statements:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), parameters)
                written += int(result.scalar_one())
                await connection.execute(
                    text(
                        """
                        UPDATE name_observation_backfill_batches
                        SET written_count = :written, selected_count = :written,
                            updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    {"id": batch_id, "written": written},
                )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE name_observation_backfill_batches
                    SET status = 'completed', finished_at = now(), updated_at = now(),
                        cursor_json = jsonb_build_object('complete', true)
                    WHERE id = :id
                    """
                ),
                {"id": batch_id},
            )
    except Exception as exc:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE name_observation_backfill_batches
                    SET status = 'failed', finished_at = now(), updated_at = now(),
                        error_summary = :error
                    WHERE id = :id
                    """
                ),
                {"id": batch_id, "error": type(exc).__name__},
            )
        raise
    return {"scope": scope, "status": "completed", "written_count": written}


def _nekro_conversation(chat_key: str) -> tuple[str, str] | None:
    value = chat_key.removeprefix("onebot_v11-")
    for kind in ("group", "private"):
        for separator in ("_", "-"):
            prefix = f"{kind}{separator}"
            if value.startswith(prefix) and value[len(prefix) :]:
                return kind, value[len(prefix) :]
    return None


async def _backfill_nekro_live(
    target: AsyncEngine,
    source_url: str,
    *,
    snapshot_id: str,
    cutover: datetime,
    chunk_size: int,
) -> dict[str, int | str]:
    scope = "nekro_live_post_cutover"
    batch_id = _uuid("batch", scope, snapshot_id)
    cursor_timestamp = float(cutover.timestamp())
    cursor_id = ""
    selected = 0
    written = 0
    source = create_async_engine(_async_url(source_url), pool_pre_ping=True)
    try:
        async with target.begin() as connection:
            batch = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT status, cursor_json, selected_count, written_count
                        FROM name_observation_backfill_batches
                        WHERE source_system = 'superlily.name_history_backfill'
                          AND source_scope = :scope
                          AND source_snapshot_id = :snapshot_id
                        """
                        ),
                        {"scope": scope, "snapshot_id": snapshot_id},
                    )
                )
                .mappings()
                .first()
            )
            if batch is not None and batch["status"] == "completed":
                return {"scope": scope, **dict(batch)}
            if batch is not None:
                cursor = batch["cursor_json"] or {}
                cursor_timestamp = float(cursor.get("send_timestamp", cursor_timestamp))
                cursor_id = str(cursor.get("id", ""))
                selected = int(batch["selected_count"])
                written = int(batch["written_count"])
            await connection.execute(
                text(
                    """
                    INSERT INTO name_observation_backfill_batches (
                        id, source_system, source_scope, source_snapshot_id, status,
                        cursor_json, selected_count, written_count, existing_count,
                        started_at, updated_at
                    ) VALUES (
                        :id, 'superlily.name_history_backfill', :scope, :snapshot_id,
                        'running', CAST(:cursor AS jsonb), :selected, :written, 0, now(), now()
                    )
                    ON CONFLICT (source_system, source_scope, source_snapshot_id)
                    DO UPDATE SET status = 'running', error_summary = NULL, updated_at = now()
                    """
                ),
                {
                    "id": batch_id,
                    "scope": scope,
                    "snapshot_id": snapshot_id,
                    "cursor": json.dumps({"send_timestamp": cursor_timestamp, "id": cursor_id}),
                    "selected": selected,
                    "written": written,
                },
            )

        account_latest: dict[str, str] = {}
        display_latest: dict[tuple[str, str, str], str] = {}
        async with target.connect() as connection:
            latest_rows = (
                await connection.execute(
                    text(
                        """
                        SELECT DISTINCT ON (
                            user_id,
                            name_kind,
                            CASE WHEN name_kind = 'account_name' THEN ''
                                 ELSE COALESCE(conversation_type, '') END,
                            CASE WHEN name_kind = 'account_name' THEN ''
                                 ELSE COALESCE(conversation_id, '') END
                        ) user_id, name_kind, conversation_type, conversation_id, name_value
                        FROM identity_name_observations
                        WHERE platform = 'qq'
                        ORDER BY user_id, name_kind,
                                 CASE WHEN name_kind = 'account_name' THEN ''
                                      ELSE COALESCE(conversation_type, '') END,
                                 CASE WHEN name_kind = 'account_name' THEN ''
                                      ELSE COALESCE(conversation_id, '') END,
                                 observed_at DESC, recorded_at DESC, id DESC
                        """
                    )
                )
            ).mappings()
            for row in latest_rows:
                if row["name_kind"] == "account_name":
                    account_latest[str(row["user_id"])] = str(row["name_value"])
                elif row["name_kind"] == "conversation_display_name":
                    display_latest[
                        (
                            str(row["user_id"]),
                            str(row["conversation_type"]),
                            str(row["conversation_id"]),
                        )
                    ] = str(row["name_value"])

        while True:
            async with source.connect() as connection:
                rows = (
                    (
                        await connection.execute(
                            text(
                                """
                            SELECT id::text AS source_record_id, sender_id::text,
                                   platform_userid::text, sender_name, sender_nickname,
                                   chat_key, send_timestamp
                            FROM chat_message
                            WHERE send_timestamp IS NOT NULL
                              AND send_timestamp >= :cutover
                              AND (
                                  send_timestamp > :cursor_timestamp
                                  OR (
                                      send_timestamp = :cursor_timestamp
                                      AND id::text > :cursor_id
                                  )
                              )
                            ORDER BY send_timestamp, id::text
                            LIMIT :limit
                            """
                            ),
                            {
                                "cutover": cutover.timestamp(),
                                "cursor_timestamp": cursor_timestamp,
                                "cursor_id": cursor_id,
                                "limit": chunk_size,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
            if not rows:
                break

            inserts: list[dict[str, Any]] = []
            for row in rows:
                selected += 1
                source_record_id = str(row["source_record_id"])
                user_id = str(row["platform_userid"] or row["sender_id"] or "").strip()
                parsed = _nekro_conversation(str(row["chat_key"] or ""))
                if not user_id or parsed is None:
                    continue
                conversation_type, conversation_id = parsed
                observed_at = datetime.fromtimestamp(float(row["send_timestamp"]), tz=timezone.utc)
                account_name = str(row["sender_name"] or "").strip()
                display_name = str(row["sender_nickname"] or row["sender_name"] or "").strip()
                candidates = (
                    ("account_name", account_name),
                    ("conversation_display_name", display_name),
                )
                for name_kind, name_value in candidates:
                    if not name_value:
                        continue
                    if name_kind == "account_name":
                        if account_latest.get(user_id) == name_value:
                            continue
                        account_latest[user_id] = name_value
                    else:
                        display_key = (user_id, conversation_type, conversation_id)
                        if display_latest.get(display_key) == name_value:
                            continue
                        display_latest[display_key] = name_value
                    inserts.append(
                        {
                            "id": _uuid("nekro-live", source_record_id, name_kind),
                            "user_id": user_id,
                            "conversation_type": conversation_type,
                            "conversation_id": conversation_id,
                            "name_kind": name_kind,
                            "name_value": name_value,
                            "observed_at": observed_at,
                            "source_record_id": source_record_id,
                            "provenance": json.dumps(
                                {
                                    "source_snapshot_id": snapshot_id,
                                    "chat_key": str(row["chat_key"]),
                                },
                                separators=(",", ":"),
                            ),
                        }
                    )

            cursor_timestamp = float(rows[-1]["send_timestamp"])
            cursor_id = str(rows[-1]["source_record_id"])
            async with target.begin() as connection:
                if inserts:
                    await connection.execute(
                        text(
                            """
                            INSERT INTO identity_name_observations (
                                id, platform, user_id, conversation_type,
                                conversation_id, name_kind, name_value, observed_at,
                                instance_id, source_system, source_record_type,
                                source_record_id, observation_method, provenance_json
                            ) VALUES (
                                :id, 'qq', :user_id, :conversation_type,
                                :conversation_id, :name_kind, :name_value, :observed_at,
                                'nekro-agent', 'nekro.chat_message.live', 'chat_message',
                                :source_record_id, 'source_message', CAST(:provenance AS jsonb)
                            )
                            ON CONFLICT (
                                source_system, source_record_type, source_record_id, name_kind
                            ) DO NOTHING
                            """
                        ),
                        inserts,
                    )
                    written += len(inserts)
                await connection.execute(
                    text(
                        """
                        UPDATE name_observation_backfill_batches
                        SET cursor_json = CAST(:cursor AS jsonb), selected_count = :selected,
                            written_count = :written, updated_at = now()
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": batch_id,
                        "cursor": json.dumps({"send_timestamp": cursor_timestamp, "id": cursor_id}),
                        "selected": selected,
                        "written": written,
                    },
                )

        async with target.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE name_observation_backfill_batches
                    SET status = 'completed', finished_at = now(), updated_at = now()
                    WHERE id = :id
                    """
                ),
                {"id": batch_id},
            )
        return {
            "scope": scope,
            "status": "completed",
            "selected_count": selected,
            "written_count": written,
        }
    except Exception as exc:
        async with target.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE name_observation_backfill_batches
                    SET status = 'failed', finished_at = now(), updated_at = now(),
                        error_summary = :error
                    WHERE id = :id
                    """
                ),
                {"id": batch_id, "error": type(exc).__name__},
            )
        raise
    finally:
        await source.dispose()


_CORE_IDENTITY_SQL = """
WITH candidates AS (
    SELECT observation.id, source.platform, observation.sender_id AS user_id,
           source.conversation_type, source.conversation_id,
           btrim(observation.sender_name) AS name_value, source.occurred_at,
           observation.instance_id, observation.reported_source_event_id,
           lag(btrim(observation.sender_name)) OVER (
               PARTITION BY observation.instance_id, observation.sender_id,
                            source.conversation_type, source.conversation_id
               ORDER BY source.occurred_at, observation.id
           ) AS previous_name
    FROM event_observations AS observation
    JOIN source_events AS source ON source.id = observation.source_event_id
    WHERE observation.sender_id IS NOT NULL
      AND NULLIF(btrim(observation.sender_name), '') IS NOT NULL
      AND source.occurred_at < :cutover
), inserted AS (
    INSERT INTO identity_name_observations (
        id, platform, user_id, conversation_type, conversation_id, name_kind,
        name_value, observed_at, instance_id, source_system, source_record_type,
        source_record_id, observation_method, provenance_json
    )
    SELECT md5('core-effective:' || id), platform, user_id, conversation_type,
           conversation_id, 'effective_display_name', name_value, occurred_at,
           instance_id, 'superlily_core', 'event_observation', id, 'event',
           jsonb_build_object(
               'reported_source_event_id', reported_source_event_id,
               'backfill_semantics', 'legacy_effective_display'
           )
    FROM candidates
    WHERE previous_name IS DISTINCT FROM name_value
    ON CONFLICT (source_system, source_record_type, source_record_id, name_kind)
    DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""


_CORE_CONVERSATION_SQL = """
WITH candidates AS (
    SELECT observation.id, source.platform, source.conversation_type,
           source.conversation_id, btrim(observation.conversation_name) AS name_value,
           source.occurred_at, observation.instance_id,
           observation.reported_source_event_id,
           lag(btrim(observation.conversation_name)) OVER (
               PARTITION BY observation.instance_id, source.platform,
                            source.conversation_type, source.conversation_id
               ORDER BY source.occurred_at, observation.id
           ) AS previous_name
    FROM event_observations AS observation
    JOIN source_events AS source ON source.id = observation.source_event_id
    WHERE NULLIF(btrim(observation.conversation_name), '') IS NOT NULL
      AND source.occurred_at < :cutover
), inserted AS (
    INSERT INTO conversation_name_observations (
        id, platform, conversation_type, conversation_id, name_value, observed_at,
        instance_id, source_system, source_record_type, source_record_id,
        observation_method, provenance_json
    )
    SELECT md5('core-conversation:' || id), platform, conversation_type,
           conversation_id, name_value, occurred_at, instance_id,
           'superlily_core', 'event_observation', id, 'event',
           jsonb_build_object('reported_source_event_id', reported_source_event_id)
    FROM candidates
    WHERE previous_name IS DISTINCT FROM name_value
    ON CONFLICT (source_system, source_record_type, source_record_id)
    DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""


_NEKRO_ARCHIVE_SQL = """
WITH source_rows AS (
    SELECT id, source_record_id, platform, sender_id AS user_id,
           conversation_type, conversation_id, occurred_at, bot_id,
           NULLIF(btrim(sender_name), '') AS account_name,
           COALESCE(
               NULLIF(btrim(raw_fields_json ->> 'sender_nickname'), ''),
               NULLIF(btrim(sender_name), '')
           ) AS display_name
    FROM archive.legacy_messages
    WHERE source_system = 'nekro.chat_message'
      AND sender_id IS NOT NULL
), names AS (
    SELECT *, 'account_name'::text AS name_kind, account_name AS name_value,
           lag(account_name) OVER (
               PARTITION BY user_id ORDER BY occurred_at, id
           ) AS previous_name
    FROM source_rows
    UNION ALL
    SELECT *, 'conversation_display_name'::text, display_name,
           lag(display_name) OVER (
               PARTITION BY user_id, conversation_type, conversation_id
               ORDER BY occurred_at, id
           )
    FROM source_rows
), inserted AS (
    INSERT INTO identity_name_observations (
        id, platform, user_id, conversation_type, conversation_id, name_kind,
        name_value, observed_at, instance_id, source_system, source_record_type,
        source_record_id, observation_method, provenance_json
    )
    SELECT md5('archive-nekro:' || source_record_id || ':' || name_kind),
           platform, user_id, conversation_type, conversation_id, name_kind,
           name_value, occurred_at, NULL, 'nekro.chat_message', 'chat_message',
           source_record_id, 'legacy_message',
           jsonb_build_object('archive_legacy_message_id', id, 'bot_id', bot_id)
    FROM names
    WHERE name_value IS NOT NULL AND previous_name IS DISTINCT FROM name_value
    ON CONFLICT (source_system, source_record_type, source_record_id, name_kind)
    DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""


_LILY_ARCHIVE_SQL = """
WITH snapshots AS (
    SELECT DISTINCT ON (
        sender_id, conversation_type, conversation_id, btrim(sender_name)
    )
        id, source_record_id, platform, sender_id AS user_id,
        conversation_type, conversation_id, btrim(sender_name) AS name_value,
        imported_at, occurred_at, bot_id
    FROM archive.legacy_messages
    WHERE source_system = 'lily.nonebot.chatrecorder.v2'
      AND sender_id IS NOT NULL
      AND NULLIF(btrim(sender_name), '') IS NOT NULL
    ORDER BY sender_id, conversation_type, conversation_id,
             btrim(sender_name), imported_at, id
), inserted AS (
    INSERT INTO identity_name_observations (
        id, platform, user_id, conversation_type, conversation_id, name_kind,
        name_value, observed_at, instance_id, source_system, source_record_type,
        source_record_id, observation_method, provenance_json
    )
    SELECT md5('archive-lily-snapshot:' || source_record_id), platform, user_id,
           conversation_type, conversation_id, 'effective_display_name',
           name_value, imported_at, NULL, 'lily.nonebot.chatrecorder.v2',
           'chatrecorder_join_snapshot', source_record_id, 'legacy_join_snapshot',
           jsonb_build_object(
               'archive_legacy_message_id', id,
               'source_message_occurred_at', occurred_at,
               'bot_id', bot_id,
               'temporal_claim', 'observed_at_import_not_message_time'
           )
    FROM snapshots
    ON CONFLICT (source_system, source_record_type, source_record_id, name_kind)
    DO NOTHING
    RETURNING 1
)
SELECT count(*) FROM inserted
"""


async def run(args: argparse.Namespace) -> list[dict[str, int | str]]:
    engine = create_async_engine(_async_url(args.database_url), pool_pre_ping=True)
    try:
        await _require_head(engine)
        cutover = datetime.fromisoformat(args.cutover.replace("Z", "+00:00"))
        if cutover.tzinfo is None:
            raise ValueError("--cutover must include a timezone")
        parameters = {"cutover": cutover.astimezone(timezone.utc)}
        results = [
            await _run_sql_scope(
                engine,
                scope="core_event_observations",
                snapshot_id=args.snapshot_id,
                statements=(_CORE_IDENTITY_SQL, _CORE_CONVERSATION_SQL),
                parameters=parameters,
            ),
            await _run_sql_scope(
                engine,
                scope="nekro_archive",
                snapshot_id=args.snapshot_id,
                statements=(_NEKRO_ARCHIVE_SQL,),
                parameters=parameters,
            ),
            await _run_sql_scope(
                engine,
                scope="lily_archive_snapshots",
                snapshot_id=args.snapshot_id,
                statements=(_LILY_ARCHIVE_SQL,),
                parameters=parameters,
            ),
        ]
        if args.nekro_source_url:
            nekro_cutover_value = args.nekro_source_cutover or args.cutover
            nekro_cutover = datetime.fromisoformat(nekro_cutover_value.replace("Z", "+00:00"))
            if nekro_cutover.tzinfo is None:
                raise ValueError("--nekro-source-cutover must include a timezone")
            results.append(
                await _backfill_nekro_live(
                    engine,
                    args.nekro_source_url,
                    snapshot_id=args.snapshot_id,
                    cutover=nekro_cutover.astimezone(timezone.utc),
                    chunk_size=args.chunk_size,
                )
            )
        return results
    finally:
        await engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Checkpointed, idempotent name-history backfill for Core and H2 archive rows."
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("SUPERLILY_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--snapshot-id", required=True)
    parser.add_argument(
        "--nekro-source-url",
        default=os.getenv("SUPERLILY_NEKRO_DATABASE_URL"),
        help="Optional read-only Nekro database URL for exact post-cutover account/card history.",
    )
    parser.add_argument(
        "--nekro-source-cutover",
        help="Timezone-aware H2 boundary for the optional live Nekro source; defaults to --cutover.",
    )
    parser.add_argument("--chunk-size", type=int, default=10_000)
    parser.add_argument(
        "--cutover",
        required=True,
        help="Only Core events before this timezone-aware timestamp are treated as legacy names.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
