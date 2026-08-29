from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).parents[1]


def _load_retry_module():
    path = ROOT / "bridges" / "nekro" / "superlily_bridge" / "render_retry.py"
    spec = spec_from_file_location("superlily_nekro_render_retry", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_content_failure_allows_one_real_iteration_then_suppresses() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=60, max_entries=4)

    assert tracker.content_failure("source-1", now=1) == "retry"
    assert tracker.content_failure("source-1", now=2) == "suppress"
    assert tracker.content_failure("source-1", now=3) == "suppress"

    tracker.succeeded("source-1")
    assert tracker.content_failure("source-1", now=4) == "retry"


def test_terminal_state_never_turns_a_failure_into_a_text_send() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=60, max_entries=4)

    tracker.mark_terminal("source-1", now=1)
    assert tracker.content_failure("source-1", now=2) == "suppress"


def test_expired_retry_state_does_not_leak_to_a_new_request() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=10, max_entries=4)

    assert tracker.content_failure("source-1", now=1) == "retry"
    assert tracker.content_failure("source-1", now=12) == "retry"


def test_retry_instruction_contains_only_bounded_actionable_diagnostic() -> None:
    retry = _load_retry_module()

    instruction = retry.retry_instruction(
        "renderer_content_error",
        {
            "schema_version": "1.0",
            "stage": "xelatex",
            "error_class": "undefined_control_sequence",
            "command": r"\unknowncommand",
            "node_id": "md007",
            "raw_log": "must-not-leak",
        },
    )

    assert issubclass(retry.RenderRetryRequired, RuntimeError)
    assert instruction.startswith("RENDER_CONTENT_ERROR")
    assert "md007" in instruction
    assert r"\unknowncommand" in instruction
    assert "must-not-leak" not in instruction
    assert "Revise the Markdown" in instruction


def test_python_escape_diagnostic_requires_a_raw_string() -> None:
    retry = _load_retry_module()

    instruction = retry.retry_instruction("markdown_math_escape_corrupted")

    assert 'r"""..."""' in instruction
    assert "failed source" in instruction


def test_bridge_raises_for_real_iteration_and_has_no_failure_fallback() -> None:
    source = (ROOT / "bridges" / "nekro" / "superlily_bridge" / "__init__.py").read_text(
        encoding="utf-8"
    )

    assert "_render_retry_tracker.content_failure" in source
    assert "raise RenderRetryRequired" in source
    assert "retry_instruction(error_code, diagnostic)" in source
    assert "_send_render_text_fallback" not in source
    assert "fallback_text=markdown_text" not in source
    assert "markdown_plain_text" not in source
