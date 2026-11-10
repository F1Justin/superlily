import importlib.util
from pathlib import Path


def test_quota_increase_is_durable_even_after_spool_recovers(tmp_path, capsys):
    path = Path(__file__).parents[1] / "scripts/check_ingress_quota.py"
    spec = importlib.util.spec_from_file_location("ingress_quota_monitor", path)
    monitor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(monitor)
    state = tmp_path / "alerts.sqlite3"

    def sample(second, total, spool_id="s1"):
        return [{
            "instance_id": "lily-command",
            "spool": {"state": "healthy", "spool_id": spool_id,
                      "observed_at": f"2026-11-10T12:00:{second:02d}+00:00",
                      "quota_rejections": total},
        }]

    connection = monitor.open_state(state)
    monitor.record_poll(connection, sample(0, 100))
    monitor.record_poll(connection, sample(1, 101))
    monitor.record_poll(connection, sample(1, 101))
    monitor.record_poll(connection, sample(0, 102))
    connection.close()
    connection = monitor.open_state(state)
    monitor.emit_pending(connection)
    assert '"delta": 1' in capsys.readouterr().out
    monitor.emit_pending(connection)
    assert capsys.readouterr().out == ""

    monitor.record_poll(connection, sample(2, 2))
    monitor.record_poll(connection, sample(3, 3))
    monitor.record_poll(connection, sample(4, 2, "s2"))
    assert connection.execute(
        "SELECT previous_total,current_total,emitted FROM alerts ORDER BY id"
    ).fetchall() == [(100, 101, 1), (2, 3, 0), (0, 2, 0)]
    connection.close()
