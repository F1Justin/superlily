"""Conservative payload sanitization for optional diagnostic data."""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SENSITIVE_KEY = re.compile(
    r"(?:^|_)(?:access_?token|api_?key|authorization|cookie|credential|database_?(?:dsn|url)|dsn|password|private_?key|secret|session|ticket|token)(?:$|_)",
    re.IGNORECASE,
)
_URL_KEY = re.compile(r"(?:url|uri|link)$", re.IGNORECASE)
_JSON_STRING_KEY = re.compile(r"(?:^|_)(?:data|content|json|payload)(?:$|_)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class SanitizationPolicy:
    enabled: bool = False
    max_bytes: int = 32_768
    max_depth: int = 8
    max_items: int = 128
    max_string: int = 4_096


def _strip_url_query(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    # OneBot URL fields also carry platform URI schemes such as ``mqqapi``
    # and ``mqzone``.  Their query strings can contain the same session-like
    # routing data as HTTP URLs, so scheme allowlisting would leave a gap.
    # Plain display strings without a scheme remain untouched.
    if not parsed.scheme:
        return value
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _sanitize(value: Any, policy: SanitizationPolicy, depth: int, key: str = "") -> Any:
    if depth > policy.max_depth:
        return "[MAX_DEPTH]"
    if _SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (child_key, child_value) in enumerate(value.items()):
            if index >= policy.max_items:
                result["_truncated_items"] = len(value) - policy.max_items
                break
            child_key = str(child_key)[:256]
            result[child_key] = _sanitize(child_value, policy, depth + 1, child_key)
        return result
    if isinstance(value, (list, tuple)):
        result = [_sanitize(item, policy, depth + 1, key) for item in value[: policy.max_items]]
        if len(value) > policy.max_items:
            result.append({"_truncated_items": len(value) - policy.max_items})
        return result
    if isinstance(value, str):
        lowered = value.lower()
        if lowered.startswith(("base64://", "data:")):
            return "[BINARY_DATA]"
        if key.lower() in {"file", "local_path"} and lowered.startswith("file://"):
            return "[LOCAL_FILE]"
        if _JSON_STRING_KEY.search(key) and value.lstrip().startswith(("{", "[")):
            try:
                nested = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                if isinstance(nested, (dict, list)):
                    sanitized_nested = _sanitize(nested, policy, depth + 1, key)
                    encoded_nested = json.dumps(
                        sanitized_nested,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    if len(encoded_nested) > policy.max_string:
                        return json.dumps(
                            {
                                "_truncated": True,
                                "_sanitized_chars": len(encoded_nested),
                            },
                            separators=(",", ":"),
                        )
                    value = encoded_nested
                    lowered = value.lower()
        if _URL_KEY.search(key) or lowered.startswith(("http://", "https://")):
            value = _strip_url_query(value)
        if len(value) > policy.max_string:
            return value[: policy.max_string] + "...[TRUNCATED]"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[: policy.max_string]


def sanitize_payload(payload: dict[str, Any] | None, policy: SanitizationPolicy) -> dict[str, Any] | None:
    """Return an allow-sized, recursively redacted diagnostic payload.

    Raw diagnostics are disabled by default. If the sanitized result is still
    too large, no preview is retained because even a redacted preview can carry
    unintended personal data.
    """

    if not policy.enabled or payload is None:
        return None
    sanitized = _sanitize(payload, policy, 0)
    encoded = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > policy.max_bytes:
        return {"_truncated": True, "_sanitized_bytes": len(encoded)}
    return sanitized
