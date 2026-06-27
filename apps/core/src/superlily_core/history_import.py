"""Dry-run helpers for future historical event imports.

This module intentionally does not write to Core storage.  It validates
candidate EventIn-shaped records and reports whether they carry the fields
needed for a safe observation import and later reference resolution.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from superlily_contracts import EventIn


def _source_label(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        label = metadata.get("original_source") or metadata.get("source")
        if label:
            return str(label)
    return "unknown"


def dry_run_payloads(payloads: Iterable[dict[str, Any]]) -> dict[str, Any]:
    total = 0
    valid = 0
    invalid = 0
    references = 0
    reply_references = 0
    with_platform_message_id = 0
    with_text = 0
    by_original_source: Counter[str] = Counter()
    errors: list[dict[str, Any]] = []

    for index, payload in enumerate(payloads, start=1):
        total += 1
        by_original_source[_source_label(payload)] += 1
        try:
            event = EventIn.model_validate(payload)
        except ValidationError as exc:
            invalid += 1
            if len(errors) < 20:
                errors.append({"index": index, "error": exc.errors(include_url=False)})
            continue

        valid += 1
        references += len(event.references)
        reply_references += sum(1 for reference in event.references if reference.type == "reply_to")
        if event.message and event.message.id:
            with_platform_message_id += 1
        if event.message and event.message.text:
            with_text += 1

    return {
        "total": total,
        "valid": valid,
        "invalid": invalid,
        "references": references,
        "reply_references": reply_references,
        "with_platform_message_id": with_platform_message_id,
        "with_text": with_text,
        "by_original_source": dict(sorted(by_original_source.items())),
        "sample_errors": errors,
        "writes": 0,
    }


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            value = json.loads(stripped)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} is not a JSON object")
            yield value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dry-run EventIn-shaped historical import candidates.")
    parser.add_argument("jsonl", type=Path, help="JSONL file containing one candidate EventIn payload per line")
    args = parser.parse_args(argv)
    print(json.dumps(dry_run_payloads(iter_jsonl(args.jsonl)), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
