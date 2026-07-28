"""Bounded, dependency-free retry state for model-authored Markdown rendering."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Literal


RetryAction = Literal["retry", "fallback", "suppress"]


@dataclass(slots=True)
class _RetryState:
    failures: int
    updated_at: float


class RenderRetryTracker:
    """Allow one corrected document, then fence every further fallback send."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 900.0,
        max_entries: int = 1_024,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._states: dict[str, _RetryState] = {}

    def _prune(self, now: float) -> None:
        expired = [
            key
            for key, state in self._states.items()
            if now - state.updated_at >= self.ttl_seconds
        ]
        for key in expired:
            self._states.pop(key, None)
        overflow = len(self._states) - self.max_entries + 1
        if overflow > 0:
            oldest = sorted(
                self._states,
                key=lambda key: self._states[key].updated_at,
            )[:overflow]
            for key in oldest:
                self._states.pop(key, None)

    def content_failure(self, key: str, *, now: float | None = None) -> RetryAction:
        observed_at = time.monotonic() if now is None else now
        self._prune(observed_at)
        state = self._states.get(key)
        if state is None:
            self._states[key] = _RetryState(failures=1, updated_at=observed_at)
            return "retry"
        state.updated_at = observed_at
        if state.failures == 1:
            state.failures = 2
            return "fallback"
        return "suppress"

    def force_fallback(self, key: str, *, now: float | None = None) -> RetryAction:
        observed_at = time.monotonic() if now is None else now
        self._prune(observed_at)
        state = self._states.get(key)
        if state is not None and state.failures >= 2:
            state.updated_at = observed_at
            return "suppress"
        self._states[key] = _RetryState(failures=2, updated_at=observed_at)
        return "fallback"

    def succeeded(self, key: str) -> None:
        self._states.pop(key, None)


_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def markdown_plain_text(markdown: str) -> str:
    """Remove presentation-only markers while preserving answer content."""

    output: list[str] = []
    in_fence = False
    for original in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        stripped = original.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if not in_fence and _TABLE_SEPARATOR_RE.fullmatch(original):
            continue
        line = original
        if not in_fence:
            line = re.sub(r"^\s{0,3}#{1,6}[ \t]+", "", line)
            line = re.sub(r"^\s{0,3}>[ \t]?", "", line)
            line = line.replace("**", "")
            if line.strip() == "$$":
                continue
        output.append(line.rstrip())
    result = "\n".join(output).strip()
    return result or "内容暂时无法以图片形式显示。"


class RenderRetryRequired(RuntimeError):
    """Stop the current sandbox script so Nekro performs a real agent iteration."""


def retry_instruction(error_code: str) -> str:
    hint = (
        "The Python string damaged TeX backslashes. Regenerate with a raw "
        'triple-quoted string such as r"""...""".'
        if error_code == "markdown_math_escape_corrupted"
        else "Correct the malformed or unbalanced TeX and simplify it if needed."
    )
    return (
        "INTERNAL_RENDER_RETRY_REQUIRED. "
        f"{hint} Call submit_rendered_markdown exactly once more. "
        "Do not send a user-visible message yet. Never mention internal rendering, "
        "failure, retry, availability, or repair to the user. Do not use PIL, "
        "ImageDraw, or Matplotlib for text layout."
    )


FALLBACK_SENT = (
    "INTERNAL_RENDER_FALLBACK_SENT. The answer was already delivered as ordinary "
    "text. Do not send it again and never mention internal rendering status."
)

FALLBACK_SUPPRESSED = (
    "INTERNAL_RENDER_ALREADY_COMPLETED. Do not call a rendering or send method again "
    "for this request and never mention internal rendering status."
)
