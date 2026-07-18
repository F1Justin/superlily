"""Strict JSON loading and RFC 8785 canonicalization for authority material."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

import rfc8785


class CanonicalJSONError(ValueError):
    """Authority JSON is malformed, outside limits, or not canonicalizable."""


@dataclass(frozen=True, slots=True)
class JSONLimits:
    max_source_bytes: int = 262_144
    max_depth: int = 32
    max_object_properties: int = 512
    max_array_items: int = 4_096
    max_total_nodes: int = 32_768
    max_string_characters: int = 262_144


@dataclass(frozen=True, slots=True)
class CanonicalJSON:
    value: Any
    canonical_bytes: bytes
    sha256: str


def _reject_constant(value: str) -> None:
    raise CanonicalJSONError(f"non-finite JSON number is forbidden: {value}")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise CanonicalJSONError("non-finite JSON number is forbidden")
    return parsed


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise CanonicalJSONError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _validate_tree(value: Any, limits: JSONLimits) -> None:
    node_count = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal node_count
        node_count += 1
        if node_count > limits.max_total_nodes:
            raise CanonicalJSONError("JSON node limit exceeded")
        if depth > limits.max_depth:
            raise CanonicalJSONError("JSON depth limit exceeded")
        if node is None or isinstance(node, bool):
            return
        if isinstance(node, str):
            if len(node) > limits.max_string_characters:
                raise CanonicalJSONError("JSON string length limit exceeded")
            return
        if isinstance(node, int):
            return
        if isinstance(node, float):
            if not math.isfinite(node):
                raise CanonicalJSONError("non-finite JSON number is forbidden")
            return
        if isinstance(node, list):
            if len(node) > limits.max_array_items:
                raise CanonicalJSONError("JSON array item limit exceeded")
            for item in node:
                visit(item, depth + 1)
            return
        if isinstance(node, dict):
            if len(node) > limits.max_object_properties:
                raise CanonicalJSONError("JSON object property limit exceeded")
            for key, item in node.items():
                if not isinstance(key, str):
                    raise CanonicalJSONError("JSON object keys must be strings")
                if len(key) > limits.max_string_characters:
                    raise CanonicalJSONError("JSON object key length limit exceeded")
                visit(item, depth + 1)
            return
        raise CanonicalJSONError(f"unsupported JSON value type: {type(node).__name__}")

    visit(value, 0)


def strict_json_loads(source: bytes, *, limits: JSONLimits = JSONLimits()) -> Any:
    """Decode authority JSON without lossy normalization or permissive extensions."""

    if not isinstance(source, bytes):
        raise TypeError("authority JSON source must be bytes")
    if not source:
        raise CanonicalJSONError("authority JSON source is empty")
    if len(source) > limits.max_source_bytes:
        raise CanonicalJSONError("authority JSON source byte limit exceeded")
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalJSONError("authority JSON must be valid UTF-8") from exc
    if text.startswith("\ufeff"):
        raise CanonicalJSONError("authority JSON must not contain a UTF-8 BOM")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except CanonicalJSONError:
        raise
    except (json.JSONDecodeError, ValueError) as exc:
        raise CanonicalJSONError("authority JSON is invalid") from exc
    _validate_tree(value, limits)
    return value


def canonicalize_json_value(value: Any, *, limits: JSONLimits = JSONLimits()) -> CanonicalJSON:
    """Canonicalize an already-decoded JSON value and return its content identity."""

    _validate_tree(value, limits)
    try:
        canonical_bytes = rfc8785.dumps(value)
    except (rfc8785.CanonicalizationError, UnicodeError, ValueError, TypeError) as exc:
        raise CanonicalJSONError("JSON value is outside the RFC 8785 domain") from exc
    if len(canonical_bytes) > limits.max_source_bytes:
        raise CanonicalJSONError("canonical JSON byte limit exceeded")
    return CanonicalJSON(
        value=value,
        canonical_bytes=canonical_bytes,
        sha256=hashlib.sha256(canonical_bytes).hexdigest(),
    )


def canonicalize_json(source: bytes, *, limits: JSONLimits = JSONLimits()) -> CanonicalJSON:
    """Strictly decode and RFC 8785-canonicalize raw JSON authority bytes."""

    return canonicalize_json_value(strict_json_loads(source, limits=limits), limits=limits)
