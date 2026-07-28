from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys


ROOT = Path(__file__).parents[1]


def _load_retry_module():
    path = (
        ROOT
        / "bridges"
        / "nekro"
        / "superlily_bridge"
        / "render_retry.py"
    )
    spec = spec_from_file_location("superlily_nekro_render_retry", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_content_failure_allows_one_real_iteration_then_one_fallback() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=60, max_entries=4)

    assert tracker.content_failure("source-1", now=1) == "retry"
    assert tracker.content_failure("source-1", now=2) == "fallback"
    assert tracker.content_failure("source-1", now=3) == "suppress"

    tracker.succeeded("source-1")
    assert tracker.content_failure("source-1", now=4) == "retry"


def test_infrastructure_fallback_is_immediate_and_fenced() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=60, max_entries=4)

    assert tracker.force_fallback("source-1", now=1) == "fallback"
    assert tracker.force_fallback("source-1", now=2) == "suppress"
    assert tracker.content_failure("source-1", now=3) == "suppress"


def test_expired_retry_state_does_not_leak_to_a_new_request() -> None:
    retry = _load_retry_module()
    tracker = retry.RenderRetryTracker(ttl_seconds=10, max_entries=4)

    assert tracker.content_failure("source-1", now=1) == "retry"
    assert tracker.content_failure("source-1", now=12) == "retry"


def test_markdown_fallback_removes_presentation_markers_but_keeps_content() -> None:
    retry = _load_retry_module()

    plain = retry.markdown_plain_text(
        """# 标题

**结论：** $x=1$

| 项目 | 内容 |
| --- | --- |
| A | B |

```python
print("ok")
```
"""
    )

    assert plain.startswith("标题")
    assert "结论： $x=1$" in plain
    assert "| --- |" not in plain
    assert 'print("ok")' in plain
    assert "**" not in plain


def test_retry_instruction_is_internal_bounded_and_requires_raw_string() -> None:
    retry = _load_retry_module()

    instruction = retry.retry_instruction("markdown_math_escape_corrupted")

    assert issubclass(retry.RenderRetryRequired, RuntimeError)
    assert instruction.startswith("INTERNAL_RENDER_RETRY_REQUIRED")
    assert "exactly once" in instruction
    assert 'r"""..."""' in instruction
    assert "Do not send a user-visible message" in instruction


def test_bridge_raises_to_enter_the_real_nekro_agent_iteration() -> None:
    source = (
        ROOT / "bridges" / "nekro" / "superlily_bridge" / "__init__.py"
    ).read_text(encoding="utf-8")

    assert "统一文档渲染暂时不可用" not in source
    assert "渲染器坏掉" not in source
    assert "_render_retry_tracker.content_failure" in source
    assert "raise RenderRetryRequired(retry_instruction(error_code))" in source
    assert "_render_retry_tracker.force_fallback" in source
    assert "await _send_render_text_fallback" in source
    assert "fallback_text=markdown_text" in source
