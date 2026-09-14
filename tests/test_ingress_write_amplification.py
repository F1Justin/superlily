import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, func, select

from superlily_contracts import EventIn, HeartbeatIn
from superlily_core.models import (
    BotInstance, CollectorWatermark, EventObservation, HistoryDeliveryReceipt,
    IngressReceiptRecord, InstanceStatusTransition, SourceEvent,
)
from superlily_core.service import (
    _advance_collector_watermark, ensure_instance, ingest_event, ingest_heartbeat,
)
from test_api import event_payload


@contextmanager
def writes(app):
    recorded = []

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(("UPDATE ", "INSERT ")):
            recorded.append((statement, cursor.rowcount))

    engine = app.state.database.engine.sync_engine
    event.listen(engine, "after_cursor_execute", capture)
    try:
        yield recorded
    finally:
        event.remove(engine, "after_cursor_execute", capture)


def durable_payload(sequence, instance_id="lily-command"):
    payload = event_payload(
        instance_id, source_event_id=f"write-ablation-{instance_id}-{sequence}",
        message_id=f"write-ablation-{sequence}",
    )
    payload["ingress"] = {
        "spool_id": "write-ablation-spool", "sequence": sequence,
        "record_sha256": f"{sequence:064x}", "captured_at": payload["occurred_at"],
    }
    return EventIn.model_validate(payload)


async def test_identical_instance_upsert_does_not_update_row(app):
    payload = durable_payload(1)
    async with app.state.database.sessions() as session:
        original = await ensure_instance(session, payload.instance, app.state.settings)
        first_seen = original.first_seen_at
        await session.commit()
        with writes(app) as recorded:
            unchanged = await ensure_instance(session, payload.instance, app.state.settings)
            await session.commit()
        upserts = [(sql, count) for sql, count in recorded if sql.startswith("INSERT INTO bot_instances")]
        assert len(upserts) == 1
        assert upserts[0][1] == 0
        assert unchanged.first_seen_at == first_seen


@pytest.mark.parametrize("field,value", [
    ("platform", "other"), ("adapter", "other"), ("bot_id", "other"),
    ("role", "other"), ("display_name", "renamed"), ("version", "new-version"),
])
async def test_changed_instance_metadata_is_persisted(app, field, value):
    instance = durable_payload(1).instance
    async with app.state.database.sessions() as session:
        row = await ensure_instance(session, instance, app.state.settings)
        first_seen = row.first_seen_at
        await session.commit()
        with writes(app) as recorded:
            row = await ensure_instance(session, instance.model_copy(update={field: value}), app.state.settings)
            await session.commit()
        assert getattr(row, field) == value
        assert row.first_seen_at == first_seen
        assert [count for sql, count in recorded if sql.startswith("INSERT INTO bot_instances")] == [1]
        if field in {"display_name", "version"}:
            row = await ensure_instance(session, instance, app.state.settings)
            await session.commit()
            assert getattr(row, field) is None


async def test_identical_instance_upsert_preserves_pending_status(app):
    instance = durable_payload(1).instance
    async with app.state.database.sessions() as session:
        row = await ensure_instance(session, instance, app.state.settings)
        await session.commit()
        row.reported_status = "online"
        row.metadata_json = {"pending": True}
        refreshed = await ensure_instance(session, instance, app.state.settings)
        await session.commit()
        assert refreshed.reported_status == "online"
        assert refreshed.metadata_json == {"pending": True}


async def test_watermark_updates_once_and_replays_without_update(app):
    for sequence in (1, 3, 2):
        payload = durable_payload(sequence)
        async with app.state.database.sessions() as session:
            with writes(app) as recorded:
                observation, duplicate = await ingest_event(session, payload, f"write-test-{sequence}", app.state.settings)
            assert not duplicate
            assert len([sql for sql, _ in recorded if sql.startswith("UPDATE collector_watermarks")]) == 1
            receipt = await session.scalar(select(IngressReceiptRecord).where(IngressReceiptRecord.observation_id == observation.id))
            receipt_id = receipt.id
        # A new pool/session must see the commit before any replay acknowledgement.
        await app.state.database.dispose()
        async with app.state.database.sessions() as session:
            assert await session.get(IngressReceiptRecord, receipt_id) is not None
            with writes(app) as recorded:
                _, duplicate = await ingest_event(session, payload, f"write-test-{sequence}", app.state.settings)
            assert duplicate
            assert not [sql for sql, _ in recorded if sql.startswith("UPDATE collector_watermarks")]
            row = await session.get(CollectorWatermark, ("lily-command", "write-ablation-spool"))
            assert (row.highest_contiguous_sequence, row.highest_seen_sequence) == {1: (1, 1), 3: (1, 3), 2: (3, 3)}[sequence]


async def test_history_receipt_closes_gap_with_one_watermark_update(app):
    payload = durable_payload(2)
    async with app.state.database.sessions() as session:
        observation, _ = await ingest_event(session, payload, "write-history-seed", app.state.settings)
        now = datetime.now(timezone.utc)
        receipt = HistoryDeliveryReceipt(
            observation_id=observation.id, instance_id="lily-command",
            delivery_source_event_id="synthetic-history-delivery", spool_id="write-ablation-spool",
            collector_sequence=1, record_sha256="a" * 64, captured_at=now, committed_at=now,
        )
        session.add(receipt)
        await session.flush()
        with writes(app) as recorded:
            row = await _advance_collector_watermark(session, receipt)
            await session.commit()
        assert (row.highest_contiguous_sequence, row.highest_seen_sequence) == (2, 2)
        assert len([sql for sql, _ in recorded if sql.startswith("UPDATE collector_watermarks")]) == 1


async def test_failed_commit_leaves_no_receipt_and_retry_recovers(app, monkeypatch):
    payload = durable_payload(1)
    async with app.state.database.sessions() as session:
        async def fail_commit():
            raise RuntimeError("synthetic failure before durable commit")

        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="before durable commit"):
            await ingest_event(session, payload, "write-failed-commit", app.state.settings)
    async with app.state.database.sessions() as session:
        for model in (IngressReceiptRecord, CollectorWatermark, EventObservation, SourceEvent, BotInstance):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        _, duplicate = await ingest_event(session, payload, "write-failed-commit", app.state.settings)
        assert not duplicate
        row = await session.get(CollectorWatermark, ("lily-command", "write-ablation-spool"))
        assert row.highest_contiguous_sequence == 1


async def test_heartbeat_updates_state_once_and_preserves_stale_guard(app):
    instance = durable_payload(1).instance
    first = datetime.now(timezone.utc) - timedelta(seconds=5)
    payload = HeartbeatIn.model_validate({
        "schema_version": "1.0", "instance": instance.model_dump(),
        "occurred_at": first, "process_status": "running", "connection_status": "connected",
    })
    async with app.state.database.sessions() as session:
        await ingest_heartbeat(session, payload, app.state.settings)
        with writes(app) as recorded:
            row = await ingest_heartbeat(session, payload.model_copy(update={"occurred_at": first + timedelta(seconds=1)}), app.state.settings)
        assert [count for sql, count in recorded if sql.startswith("INSERT INTO bot_instances")] == [0]
        assert len([sql for sql, _ in recorded if sql.startswith("UPDATE bot_instances")]) == 1
        latest = row.last_heartbeat_at
        with writes(app) as recorded:
            row = await ingest_heartbeat(session, payload, app.state.settings)
        assert row.last_heartbeat_at == latest
        assert recorded == []
        assert await session.scalar(select(func.count()).select_from(InstanceStatusTransition)) == 1


async def test_concurrent_first_instance_registration_is_safe(app):
    if app.state.database.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL row conflict and transaction locking")
    instance = durable_payload(1).instance

    async def register():
        async with app.state.database.sessions() as session:
            await ensure_instance(session, instance, app.state.settings)
            await session.commit()

    await asyncio.wait_for(asyncio.gather(*(register() for _ in range(4))), 15)
    async with app.state.database.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(BotInstance)) == 1


async def test_concurrent_spool_gap_delivery_is_safe(app):
    if app.state.database.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL collector advisory transaction lock")

    async def deliver(sequence):
        async with app.state.database.sessions() as session:
            return await ingest_event(session, durable_payload(sequence), f"concurrent-write-{sequence}", app.state.settings)

    await asyncio.wait_for(asyncio.gather(*(deliver(sequence) for sequence in (3, 1, 2))), 15)
    async with app.state.database.sessions() as session:
        row = await session.get(CollectorWatermark, ("lily-command", "write-ablation-spool"))
        assert (row.highest_contiguous_sequence, row.highest_seen_sequence) == (3, 3)
        assert await session.scalar(select(func.count()).select_from(IngressReceiptRecord)) == 3
