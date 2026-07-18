"""Small verification CLI for Git-reviewed Tool Registry authority files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .canonical_json import canonicalize_json, strict_json_loads
from .tool_registry import ToolRegistryContractError, load_tool_descriptor, validate_schema_profile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-tool-registry")
    subparsers = parser.add_subparsers(dest="command", required=True)
    descriptor = subparsers.add_parser("verify-descriptor", help="validate and hash one descriptor")
    descriptor.add_argument("path", type=Path)
    schema = subparsers.add_parser("verify-schema", help="validate one restricted JSON Schema")
    schema.add_argument("path", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        source = args.path.read_bytes()
        if args.command == "verify-descriptor":
            loaded = load_tool_descriptor(source)
            result = {
                "canonical_bytes": len(loaded.authority.canonical_bytes),
                "descriptor_hash": loaded.authority.sha256,
                "tool_id": loaded.descriptor.tool_id,
                "version": loaded.descriptor.version,
            }
        else:
            schema = strict_json_loads(source)
            if not isinstance(schema, dict):
                raise ToolRegistryContractError("schema root must be an object")
            validate_schema_profile(schema)
            authority = canonicalize_json(source)
            result = {
                "canonical_bytes": len(authority.canonical_bytes),
                "schema_hash": authority.sha256,
                "schema_profile": "json-schema-2020-12-superlily-v1",
            }
    except (OSError, ValueError) as exc:
        print(f"verification failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
