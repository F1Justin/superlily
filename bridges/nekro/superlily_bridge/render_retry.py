"""Bounded, dependency-free feedback for model-authored Markdown rendering."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Literal


RetryAction = Literal["retry", "suppress"]


@dataclass(slots=True)
class _RetryState:
    failures: int
    updated_at: float


class RenderRetryTracker:
    """Allow one corrected document and never turn content failure into a send."""

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
        state.failures += 1
        state.updated_at = observed_at
        return "suppress"

    def mark_terminal(self, key: str, *, now: float | None = None) -> None:
        observed_at = time.monotonic() if now is None else now
        self._prune(observed_at)
        self._states[key] = _RetryState(failures=2, updated_at=observed_at)

    def succeeded(self, key: str) -> None:
        self._states.pop(key, None)


class RenderRetryRequired(RuntimeError):
    """Stop the current sandbox script so Nekro performs a real agent iteration."""


_COMMAND_RE = re.compile(r"^\\(?:[A-Za-z@]{1,64}|.)$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_DIAGNOSTIC_HINTS = {
    "undefined_control_sequence": "uses a math command unavailable in the renderer",
    "missing_package_or_file": "requests a TeX package or file unavailable in the renderer",
    "unbalanced_group": "contains an extra or missing TeX brace",
    "missing_math_delimiter": "contains an invalid math-mode delimiter",
    "invalid_environment": "contains an invalid or mismatched TeX environment",
    "generic_compile_error": "was rejected by XeLaTeX",
}


def retry_instruction(error_code: str, diagnostic: object = None) -> str:
    if error_code == "markdown_math_escape_corrupted":
        reason = (
            "A Python string escape damaged TeX backslashes. Use a raw "
            'triple-quoted string such as r"""...""".'
        )
    elif isinstance(diagnostic, dict):
        error_class = diagnostic.get("error_class")
        command = diagnostic.get("command")
        node_id = diagnostic.get("node_id")
        reason = _DIAGNOSTIC_HINTS.get(str(error_class), "could not be compiled")
        if isinstance(node_id, str) and _NODE_ID_RE.fullmatch(node_id):
            reason = f"Markdown node {node_id} {reason}"
        else:
            reason = f"The submitted Markdown {reason}"
        if isinstance(command, str) and _COMMAND_RE.fullmatch(command):
            reason += f": {command}"
        reason += "."
    else:
        reason = "The submitted Markdown could not be compiled."
    return (
        "RENDER_CONTENT_ERROR\n"
        f"{reason}\n"
        "Revise the Markdown or math expression using the real diagnostic above, "
        "then call submit_rendered_markdown again. Do not send the failed source "
        "to the user."
    )


def unavailable_instruction() -> str:
    return (
        "RENDERER_UNAVAILABLE\n"
        "The requested image renderer is unavailable. Do not call it again in this "
        "turn. Use a concise ordinary-text answer only if that still satisfies the "
        "user's request."
    )


RENDER_SUPPRESSED = (
    "INTERNAL_RENDER_STOPPED. No content was sent. Do not send the failed Markdown "
    "automatically or retry another platform action for this request."
)
