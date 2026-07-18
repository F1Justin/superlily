"""Crash-safe local ingress spool used before Core event submission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any
from uuid import uuid4


class SpoolError(RuntimeError):
    pass


class SpoolConflict(SpoolError):
    pass


class SpoolFull(SpoolError):
    pass


class ReceiptMismatch(SpoolError):
    pass


@dataclass(frozen=True, slots=True)
class SpoolRecord:
    sequence: int
    endpoint: str
    idempotency_key: str
    payload: dict[str, Any]
    record_sha256: str
    captured_at: str
    attempts: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat()


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class DurableIngressSpool:
    """Small SQLite outbox with FULL synchronous commits and exact receipts."""

    SCHEMA_VERSION = "1"

    def __init__(
        self,
        path: str,
        *,
        quota_bytes: int = 268_435_456,
        retention_seconds: int = 86_400,
        max_record_bytes: int = 1_048_576,
    ) -> None:
        self.path = Path(path).expanduser()
        self.quota_bytes = max(1_048_576, quota_bytes)
        self.retention_seconds = max(0, retention_seconds)
        self.max_record_bytes = max(65_536, max_record_bytes)
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._spool_id: str | None = None
        self._last_compact = 0.0

    @property
    def spool_id(self) -> str:
        if self._spool_id is None:
            raise SpoolError("durable spool is not open")
        return self._spool_id

    def open(self) -> None:
        with self._lock:
            if self._connection is not None:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
            try:
                self._open_database()
            except sqlite3.DatabaseError as exc:
                self._close_connection()
                quarantined = self._quarantine_database_files()
                self._open_database()
                self._set_meta("quarantined_files", str(len(quarantined)))
                self._set_meta("last_error", f"database_recovered:{type(exc).__name__}")

    def _open_database(self) -> None:
        connection = sqlite3.connect(
            self.path,
            timeout=2.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=2000")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA foreign_keys=ON")
            quick_check = connection.execute("PRAGMA quick_check").fetchone()
            if quick_check is None or quick_check[0] != "ok":
                raise sqlite3.DatabaseError("spool quick_check failed")
        except Exception:
            connection.close()
            raise
        self._connection = connection
        self._create_schema()
        self._spool_id = self._get_meta("spool_id")
        if not self._spool_id:
            self._spool_id = f"spool-{uuid4().hex}"
            self._set_meta("spool_id", self._spool_id)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def _create_schema(self) -> None:
        connection = self._require_connection()
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS spool_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS spool_records (
                sequence INTEGER PRIMARY KEY,
                endpoint TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                record_sha256 TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_error TEXT,
                receipt_id TEXT,
                committed_at TEXT,
                CHECK (state IN ('pending', 'committed', 'quarantined'))
            );
            CREATE TABLE IF NOT EXISTS spool_identities (
                idempotency_key TEXT PRIMARY KEY,
                sequence INTEGER NOT NULL UNIQUE,
                record_sha256 TEXT NOT NULL,
                captured_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_spool_records_delivery
                ON spool_records(state, next_attempt_at, sequence);
            INSERT OR IGNORE INTO spool_identities(
                idempotency_key, sequence, record_sha256, captured_at
            )
            SELECT idempotency_key, sequence, record_sha256, captured_at
            FROM spool_records;
            """
        )
        if self._get_meta("schema_version") is None:
            self._set_meta("schema_version", self.SCHEMA_VERSION)
        if self._get_meta("next_sequence") is None:
            next_sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM spool_records"
            ).fetchone()[0]
            self._set_meta("next_sequence", str(next_sequence))
        for key in (
            "capture_failures",
            "quota_rejections",
            "replay_successes",
            "replay_failures",
            "quarantined_files",
        ):
            if self._get_meta(key) is None:
                self._set_meta(key, "0")

    def _quarantine_database_files(self) -> list[Path]:
        suffix = f".quarantine-{_utc_now().strftime('%Y%m%dT%H%M%SZ')}-{uuid4().hex[:8]}"
        quarantined: list[Path] = []
        for source in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if not source.exists():
                continue
            target = Path(f"{source}{suffix}")
            source.replace(target)
            quarantined.append(target)
        return quarantined

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                try:
                    self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except sqlite3.DatabaseError:
                    pass
            self._close_connection()

    def _close_connection(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._spool_id = None

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise SpoolError("durable spool is not open")
        return self._connection

    def _get_meta(self, key: str) -> str | None:
        row = self._require_connection().execute(
            "SELECT value FROM spool_meta WHERE key = ?", (key,)
        ).fetchone()
        return str(row[0]) if row is not None else None

    def _set_meta(self, key: str, value: str) -> None:
        self._require_connection().execute(
            "INSERT INTO spool_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def _meta_int(self, key: str) -> int:
        try:
            return int(self._get_meta(key) or "0")
        except ValueError:
            return 0

    def _increment_meta(self, key: str, amount: int = 1) -> None:
        self._set_meta(key, str(self._meta_int(key) + amount))

    def _live_bytes(self) -> int:
        connection = self._require_connection()
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        free_pages = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        live = max(0, page_count - free_pages) * page_size
        wal_path = Path(f"{self.path}-wal")
        if wal_path.exists():
            live += wal_path.stat().st_size
        return live

    def append_event(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> SpoolRecord:
        if not idempotency_key:
            raise SpoolError("durable event requires an idempotency key")
        with self._lock:
            connection = self._require_connection()
            base_payload = dict(payload)
            base_payload.pop("ingress", None)
            material = {
                "endpoint": "/v1/events",
                "idempotency_key": idempotency_key,
                "payload": base_payload,
            }
            record_sha256 = hashlib.sha256(_canonical_bytes(material)).hexdigest()
            existing = connection.execute(
                "SELECT * FROM spool_records WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if existing["record_sha256"] != record_sha256:
                    self._increment_meta("capture_failures")
                    self._set_meta("last_error", "idempotency_key_payload_conflict")
                    raise SpoolConflict(
                        "idempotency key is already bound to another durable record"
                    )
                record = self._row_to_record(existing)
                payload.clear()
                payload.update(record.payload)
                return record

            identity = connection.execute(
                "SELECT * FROM spool_identities WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if identity is not None:
                if identity["record_sha256"] != record_sha256:
                    self._increment_meta("capture_failures")
                    self._set_meta("last_error", "idempotency_key_payload_conflict")
                    raise SpoolConflict(
                        "idempotency key is already bound to another durable record"
                    )
                full_payload = dict(base_payload)
                full_payload["ingress"] = {
                    "schema_version": "1.0",
                    "spool_id": self.spool_id,
                    "sequence": int(identity["sequence"]),
                    "record_sha256": record_sha256,
                    "captured_at": str(identity["captured_at"]),
                }
                payload.clear()
                payload.update(full_payload)
                return SpoolRecord(
                    sequence=int(identity["sequence"]),
                    endpoint="/v1/events",
                    idempotency_key=idempotency_key,
                    payload=full_payload,
                    record_sha256=record_sha256,
                    captured_at=str(identity["captured_at"]),
                    attempts=0,
                )

            estimated_size = len(_canonical_bytes(base_payload)) + 8_192
            if estimated_size > self.max_record_bytes:
                self._increment_meta("capture_failures")
                self._set_meta("last_error", "record_too_large")
                raise SpoolFull("durable record exceeds max_record_bytes")
            self.compact()
            if self._live_bytes() + estimated_size > self.quota_bytes:
                self._increment_meta("capture_failures")
                self._increment_meta("quota_rejections")
                self._set_meta("last_error", "spool_quota_exceeded")
                raise SpoolFull("durable spool quota exceeded")

            captured_at = _utc_iso()
            connection.execute("BEGIN IMMEDIATE")
            try:
                sequence = int(self._get_meta("next_sequence") or "1")
                full_payload = dict(base_payload)
                full_payload["ingress"] = {
                    "schema_version": "1.0",
                    "spool_id": self.spool_id,
                    "sequence": sequence,
                    "record_sha256": record_sha256,
                    "captured_at": captured_at,
                }
                payload_json = _canonical_bytes(full_payload).decode("utf-8")
                if len(payload_json.encode("utf-8")) > self.max_record_bytes:
                    raise SpoolFull("durable record exceeds max_record_bytes")
                connection.execute(
                    "INSERT INTO spool_identities("
                    "idempotency_key, sequence, record_sha256, captured_at"
                    ") VALUES (?, ?, ?, ?)",
                    (idempotency_key, sequence, record_sha256, captured_at),
                )
                connection.execute(
                    "INSERT INTO spool_records("
                    "sequence, endpoint, idempotency_key, payload_json, record_sha256, captured_at"
                    ") VALUES (?, '/v1/events', ?, ?, ?, ?)",
                    (
                        sequence,
                        idempotency_key,
                        payload_json,
                        record_sha256,
                        captured_at,
                    ),
                )
                self._set_meta("next_sequence", str(sequence + 1))
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
            payload.clear()
            payload.update(full_payload)
            return SpoolRecord(
                sequence=sequence,
                endpoint="/v1/events",
                idempotency_key=idempotency_key,
                payload=full_payload,
                record_sha256=record_sha256,
                captured_at=captured_at,
                attempts=0,
            )

    def _row_to_record(self, row: sqlite3.Row) -> SpoolRecord:
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise SpoolError("durable record payload is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SpoolError("durable record payload is not an object")
        return SpoolRecord(
            sequence=int(row["sequence"]),
            endpoint=str(row["endpoint"]),
            idempotency_key=str(row["idempotency_key"]),
            payload=payload,
            record_sha256=str(row["record_sha256"]),
            captured_at=str(row["captured_at"]),
            attempts=int(row["attempts"]),
        )

    def next_pending(self, now_monotonic: float | None = None) -> SpoolRecord | None:
        del now_monotonic  # Wall-clock retry timestamps survive process restart.
        with self._lock:
            connection = self._require_connection()
            while True:
                row = connection.execute(
                    "SELECT * FROM spool_records WHERE state = 'pending' "
                    "ORDER BY sequence LIMIT 1"
                ).fetchone()
                if row is None:
                    self.compact()
                    return None
                if float(row["next_attempt_at"]) > time.time():
                    return None
                try:
                    record = self._row_to_record(row)
                    self._verify_record(record)
                    return record
                except SpoolError as exc:
                    self.quarantine(int(row["sequence"]), f"checksum:{exc}")

    def _verify_record(self, record: SpoolRecord) -> None:
        ingress = record.payload.get("ingress")
        if not isinstance(ingress, dict):
            raise SpoolError("missing ingress envelope")
        if (
            ingress.get("spool_id") != self.spool_id
            or ingress.get("sequence") != record.sequence
            or ingress.get("record_sha256") != record.record_sha256
            or ingress.get("captured_at") != record.captured_at
        ):
            raise SpoolError("ingress envelope mismatch")
        base_payload = dict(record.payload)
        base_payload.pop("ingress", None)
        material = {
            "endpoint": record.endpoint,
            "idempotency_key": record.idempotency_key,
            "payload": base_payload,
        }
        actual_hash = hashlib.sha256(_canonical_bytes(material)).hexdigest()
        if actual_hash != record.record_sha256:
            raise SpoolError("record checksum mismatch")

    def acknowledge(self, record: SpoolRecord, receipt: dict[str, Any]) -> None:
        expected = (self.spool_id, record.sequence, record.record_sha256)
        actual = (
            receipt.get("spool_id"),
            receipt.get("sequence"),
            receipt.get("record_sha256"),
        )
        if actual != expected or receipt.get("outcome") not in {"committed", "duplicate"}:
            raise ReceiptMismatch("Core receipt does not match durable spool record")
        receipt_id = receipt.get("receipt_id")
        if not isinstance(receipt_id, str) or not receipt_id:
            raise ReceiptMismatch("Core receipt has no receipt_id")
        with self._lock:
            cursor = self._require_connection().execute(
                "UPDATE spool_records SET state = 'committed', receipt_id = ?, "
                "committed_at = ?, last_error = NULL WHERE sequence = ? AND state = 'pending'",
                (receipt_id, _utc_iso(), record.sequence),
            )
            if cursor.rowcount == 0:
                state_row = self._require_connection().execute(
                    "SELECT state FROM spool_records WHERE sequence = ?", (record.sequence,)
                ).fetchone()
                if state_row is not None and state_row[0] == "committed":
                    return
                identity = self._require_connection().execute(
                    "SELECT sequence FROM spool_identities "
                    "WHERE idempotency_key = ? AND sequence = ? AND record_sha256 = ?",
                    (record.idempotency_key, record.sequence, record.record_sha256),
                ).fetchone()
                if state_row is None and identity is not None:
                    return
                raise ReceiptMismatch("durable record is not pending")
            self._increment_meta("replay_successes")
            self._set_meta("last_error", "")
            self.compact()

    def retry(self, record: SpoolRecord, error: str, *, base_seconds: float = 0.5) -> None:
        with self._lock:
            attempts = record.attempts + 1
            delay = min(60.0, max(0.1, base_seconds) * (2 ** min(attempts - 1, 10)))
            bounded_error = error[:4_096]
            cursor = self._require_connection().execute(
                "UPDATE spool_records SET attempts = ?, next_attempt_at = ?, "
                "last_attempt_at = ?, last_error = ? "
                "WHERE sequence = ? AND state = 'pending'",
                (attempts, time.time() + delay, _utc_iso(), bounded_error, record.sequence),
            )
            if cursor.rowcount == 0:
                return
            self._increment_meta("replay_failures")
            self._set_meta("last_error", bounded_error)

    def quarantine(self, sequence: int, error: str) -> None:
        with self._lock:
            bounded_error = error[:4_096]
            cursor = self._require_connection().execute(
                "UPDATE spool_records SET state = 'quarantined', last_attempt_at = ?, "
                "last_error = ? WHERE sequence = ? AND state = 'pending'",
                (_utc_iso(), bounded_error, sequence),
            )
            if cursor.rowcount == 0:
                return
            self._increment_meta("replay_failures")
            self._set_meta("last_error", bounded_error)

    def next_retry_delay(self) -> float:
        with self._lock:
            row = self._require_connection().execute(
                "SELECT MIN(next_attempt_at) FROM spool_records WHERE state = 'pending'"
            ).fetchone()
            if row is None or row[0] is None:
                return 5.0
            return max(0.05, min(5.0, float(row[0]) - time.time()))

    def compact(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_compact < 60.0:
            return
        connection = self._require_connection()
        cutoff = _utc_now().timestamp() - self.retention_seconds
        connection.execute(
            "DELETE FROM spool_records WHERE state = 'committed' "
            "AND CAST(strftime('%s', committed_at) AS INTEGER) < ?",
            (int(cutoff),),
        )
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        self._last_compact = now

    def status(self) -> dict[str, Any]:
        with self._lock:
            connection = self._require_connection()
            counts = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM spool_records GROUP BY state"
                ).fetchall()
            }
            pending_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(LENGTH(payload_json)), 0) FROM spool_records "
                    "WHERE state = 'pending'"
                ).fetchone()[0]
            )
            oldest = connection.execute(
                "SELECT MIN(captured_at) FROM spool_records WHERE state = 'pending'"
            ).fetchone()[0]
            oldest_seconds: float | None = None
            if oldest:
                try:
                    captured = datetime.fromisoformat(str(oldest))
                    if captured.tzinfo is None:
                        captured = captured.replace(tzinfo=timezone.utc)
                    oldest_seconds = max(0.0, (_utc_now() - captured).total_seconds())
                except ValueError:
                    oldest_seconds = None
            highest_sequence = max(0, int(self._get_meta("next_sequence") or "1") - 1)
            live_bytes = self._live_bytes()
            pending = counts.get("pending", 0)
            quarantined = counts.get("quarantined", 0)
            quota_rejections = self._meta_int("quota_rejections")
            if quarantined or self._meta_int("quarantined_files"):
                state = "quarantined"
            elif quota_rejections or live_bytes >= int(self.quota_bytes * 0.9):
                state = "quota_pressure"
            elif pending:
                state = "pending"
            else:
                state = "healthy"
            last_error = self._get_meta("last_error") or None
            return {
                "schema_version": "1.0",
                "enabled": True,
                "state": state,
                "durability_mode": "sqlite_full",
                "spool_id": self.spool_id,
                "pending_records": pending,
                "pending_bytes": pending_bytes,
                "committed_records": counts.get("committed", 0),
                "quarantined_records": quarantined,
                "quarantined_files": self._meta_int("quarantined_files"),
                "oldest_pending_seconds": oldest_seconds,
                "live_bytes": live_bytes,
                "quota_bytes": self.quota_bytes,
                "highest_sequence": highest_sequence,
                "replay_successes": self._meta_int("replay_successes"),
                "replay_failures": self._meta_int("replay_failures"),
                "capture_failures": self._meta_int("capture_failures"),
                "quota_rejections": quota_rejections,
                "last_error": last_error,
                "observed_at": _utc_iso(),
            }
