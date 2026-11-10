"""Record and emit alerts for increases in a collector's durable quota counter."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import subprocess


STATUS_REQUEST = """
import os
from urllib.request import Request, urlopen

request = Request(
    'http://127.0.0.1:8000/v1/ingress/status',
    headers={'Authorization': 'Bearer ' + os.environ['SUPERLILY_ADMIN_TOKEN']},
)
with urlopen(request, timeout=8) as response:
    print(response.read().decode())
"""


def open_state(path):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS samples (
            instance_id TEXT NOT NULL,
            spool_id TEXT NOT NULL,
            observed_at REAL NOT NULL,
            total INTEGER NOT NULL,
            PRIMARY KEY (instance_id, spool_id)
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY,
            instance_id TEXT NOT NULL,
            spool_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            previous_total INTEGER NOT NULL,
            current_total INTEGER NOT NULL,
            emitted INTEGER NOT NULL DEFAULT 0
        );
    """)
    return connection


def record_poll(connection, rows):
    with connection:
        connection.execute("BEGIN IMMEDIATE")
        for row in rows:
            spool = row.get("spool") or {}
            instance_id = row["instance_id"]
            spool_id = spool.get("spool_id")
            observed_at = spool.get("observed_at")
            total = spool.get("quota_rejections")
            if not spool_id or not observed_at or type(total) is not int or total < 0:
                continue
            timestamp = datetime.fromisoformat(observed_at).timestamp()
            previous = connection.execute(
                "SELECT observed_at,total FROM samples WHERE instance_id=? AND spool_id=?",
                (instance_id, spool_id),
            ).fetchone()
            if previous is not None and timestamp <= previous[0]:
                continue
            if previous is not None:
                prior_total = previous[1]
            else:
                known_instance = connection.execute(
                    "SELECT 1 FROM samples WHERE instance_id=? LIMIT 1", (instance_id,)
                ).fetchone()
                prior_total = 0 if known_instance else total
            if total > prior_total:
                connection.execute(
                    """INSERT INTO alerts (instance_id,spool_id,observed_at,previous_total,current_total)
                       VALUES (?,?,?,?,?)""",
                    (instance_id, spool_id, observed_at, prior_total, total),
                )
            connection.execute(
                """INSERT INTO samples VALUES (?,?,?,?) ON CONFLICT(instance_id,spool_id)
                   DO UPDATE SET observed_at=excluded.observed_at,total=excluded.total""",
                (instance_id, spool_id, timestamp, total),
            )


def emit_pending(connection):
    for alert_id, instance_id, spool_id, observed_at, previous, current in connection.execute(
        "SELECT id,instance_id,spool_id,observed_at,previous_total,current_total FROM alerts WHERE emitted=0 ORDER BY id"
    ).fetchall():
        print("<3>superlily_ingress_quota_rejected " + json.dumps({
            "alert_id": alert_id, "instance_id": instance_id, "spool_id": spool_id,
            "observed_at": observed_at, "delta": current - previous, "total": current,
        }), flush=True)
        with connection:
            connection.execute("UPDATE alerts SET emitted=1 WHERE id=?", (alert_id,))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--container", default="deploy-lily-core-1")
    args = parser.parse_args()
    os.umask(0o077)
    with open_state(args.state) as connection:
        emit_pending(connection)
        status = subprocess.run(
            ["docker", "exec", args.container, "python", "-c", STATUS_REQUEST],
            check=True, capture_output=True, text=True, timeout=12,
        )
        record_poll(connection, json.loads(status.stdout))
        emit_pending(connection)


if __name__ == "__main__":
    main()
