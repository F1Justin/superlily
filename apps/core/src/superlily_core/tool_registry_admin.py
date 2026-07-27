"""Local-only administration CLI for reviewed Phase 3a registry authority."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys

from fastapi import HTTPException

from superlily_contracts import (
    ModelProviderProfile,
    ProviderRegistration,
    strict_json_loads,
)

from .agent_run_service import import_model_profile
from .database import Database
from .rollout_service import import_tool_rollout_plan
from .settings import Settings
from .tool_registry_service import import_tool_descriptor, register_tool_provider


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-tool-registry-admin")
    subparsers = parser.add_subparsers(dest="command", required=True)
    descriptor = subparsers.add_parser(
        "import-descriptor", help="import one reviewed descriptor without activating it"
    )
    descriptor.add_argument("path", type=Path)
    descriptor.add_argument("--repository", type=Path, default=Path.cwd())
    descriptor.add_argument("--source-commit", required=True)
    descriptor.add_argument("--bundle-hash", required=True)
    descriptor.add_argument("--reviewer", required=True)
    rollout = subparsers.add_parser(
        "import-rollout-plan",
        help="import one Git-bound reviewed rollout plan without activating it",
    )
    rollout.add_argument("path", type=Path)
    rollout.add_argument("--repository", type=Path, default=Path.cwd())
    rollout.add_argument("--source-commit", required=True)
    rollout.add_argument("--bundle-hash", required=True)
    rollout.add_argument("--reviewer", required=True)
    model_profile = subparsers.add_parser(
        "import-model-profile",
        help="import one Git-bound reviewed model provider profile",
    )
    model_profile.add_argument("path", type=Path)
    model_profile.add_argument("--repository", type=Path, default=Path.cwd())
    model_profile.add_argument("--source-commit", required=True)
    model_profile.add_argument("--bundle-hash", required=True)
    model_profile.add_argument("--reviewer", required=True)
    provider = subparsers.add_parser(
        "register-provider", help="register one provider bound to an environment credential"
    )
    provider.add_argument("path", type=Path)
    provider.add_argument("--actor", required=True)
    return parser


def _git_authority_source(repository: Path, source_commit: str, path: Path) -> bytes:
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("authority path must be repository-relative and must not contain '..'")
    repository = repository.resolve()
    verified = subprocess.run(
        ["git", "rev-parse", "--verify", f"{source_commit}^{{commit}}"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if verified != source_commit:
        raise ValueError("source_commit must be the full commit ID, not an alias or abbreviation")
    return subprocess.run(
        ["git", "show", f"{source_commit}:{path.as_posix()}"],
        cwd=repository,
        check=True,
        capture_output=True,
    ).stdout


def _git_descriptor_source(repository: Path, source_commit: str, path: Path) -> bytes:
    """兼容旧测试/调用者；新代码统一使用 authority source。"""

    return _git_authority_source(repository, source_commit, path)


async def _run(args: argparse.Namespace) -> dict:
    settings = Settings.from_env()
    database = Database(settings.database_url)
    try:
        async with database.sessions() as session:
            if args.command == "import-descriptor":
                record, duplicate = await import_tool_descriptor(
                    session,
                    _git_descriptor_source(args.repository, args.source_commit, args.path),
                    source_commit=args.source_commit,
                    bundle_hash=args.bundle_hash,
                    reviewer=args.reviewer,
                )
                return {
                    "descriptor_hash": record.descriptor_hash,
                    "duplicate": duplicate,
                    "execution_mode": settings.tool_execution_mode,
                    "lifecycle": record.lifecycle,
                    "tool_id": record.tool_id,
                    "version": record.version,
                }
            if args.command == "import-rollout-plan":
                record, duplicate = await import_tool_rollout_plan(
                    session,
                    _git_authority_source(args.repository, args.source_commit, args.path),
                    source_commit=args.source_commit,
                    bundle_hash=args.bundle_hash,
                    reviewer=args.reviewer,
                )
                return {
                    "duplicate": duplicate,
                    "execution_ceiling": settings.tool_execution_mode,
                    "lifecycle": record.lifecycle,
                    "plan_hash": record.plan_hash,
                    "plan_id": record.plan_id,
                    "version": record.version,
                }
            if args.command == "import-model-profile":
                source = strict_json_loads(
                    _git_authority_source(
                        args.repository,
                        args.source_commit,
                        args.path,
                    )
                )
                profile = ModelProviderProfile.model_validate(source)
                record, duplicate = await import_model_profile(
                    session,
                    profile,
                    source_commit=args.source_commit,
                    bundle_hash=args.bundle_hash,
                    reviewer=args.reviewer,
                )
                return {
                    "duplicate": duplicate,
                    "profile_hash": record.profile_hash,
                    "provider_id": record.provider_id,
                    "version": record.version,
                    "agent_mode": settings.agent_mode,
                }
            source = strict_json_loads(args.path.read_bytes())
            registration = ProviderRegistration.model_validate(source)
            record, duplicate = await register_tool_provider(
                session,
                registration,
                actor=args.actor,
                settings=settings,
            )
            return {
                "duplicate": duplicate,
                "execution_mode": settings.tool_execution_mode,
                "lifecycle": record.lifecycle,
                "provider_id": record.id,
            }
    finally:
        await database.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (HTTPException, OSError, subprocess.CalledProcessError, ValueError) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        print(f"registry administration failed: {detail}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
