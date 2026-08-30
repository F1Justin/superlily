"""Bounded, dependency-free feedback for model-authored Markdown rendering."""

from __future__ import annotations

import re


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
