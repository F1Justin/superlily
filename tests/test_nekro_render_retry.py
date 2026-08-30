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

    assert "raise RenderRetryRequired" in source
    assert "retry_instruction(error_code, diagnostic)" in source
    assert "RenderRetryTracker" not in source
    assert "content_failure" not in source
    assert "_send_render_text_fallback" not in source
    assert "fallback_text=markdown_text" not in source
    assert "markdown_plain_text" not in source
