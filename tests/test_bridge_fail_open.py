import importlib.util
import sys
import time
from pathlib import Path


def load_reporter_module():
    path = Path("bridges/lily_nonebot/lily_core_bridge/reporter.py")
    spec = importlib.util.spec_from_file_location("lily_bridge_reporter_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_full_bridge_queue_drops_without_blocking() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter("http://127.0.0.1:9", "token", 1, 0.05)
    item = module.ReportItem("/v1/events", {"example": True}, "stable-key")

    started = time.perf_counter()
    accepted = reporter.enqueue(item)
    dropped = reporter.enqueue(item)
    elapsed = time.perf_counter() - started

    assert reporter.queue.qsize() == 1
    assert reporter.dropped == 1
    assert accepted is True
    assert dropped is False
    assert elapsed < 0.05


def test_bridge_with_empty_token_is_silent_noop() -> None:
    module = load_reporter_module()
    reporter = module.BackgroundReporter("http://127.0.0.1:8765", "", 1, 0.05)
    assert reporter.enqueue(module.ReportItem("/v1/events", {"example": True})) is False
    assert reporter.queue.empty()
    assert reporter.dropped == 0
