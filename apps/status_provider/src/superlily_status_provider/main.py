"""Registry-only runtime for the first standalone Superlily provider."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import json
import logging
import os
from pathlib import Path
import time

from superlily_contracts import load_tool_descriptor
from superlily_provider_sdk import (
    ProviderRegistryClient,
    ProviderReportError,
    ProviderToolImplementation,
)

from .status import PROVIDER_ID, StatusInspector, status_implementation_hash


DEFAULT_DESCRIPTOR_PATH = Path("registry/descriptors/status.inspect/1.0.0.json")
logger = logging.getLogger("superlily_status_provider")


@dataclass(frozen=True, slots=True)
class StatusProviderConfig:
    core_url: str
    token: str = field(repr=False)
    descriptor_path: Path = DEFAULT_DESCRIPTOR_PATH
    heartbeat_seconds: int = 30
    inventory_seconds: int = 300
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.core_url:
            raise ValueError("SUPERLILY_STATUS_PROVIDER_CORE_URL is required")
        if not self.token:
            raise ValueError("SUPERLILY_STATUS_PROVIDER_TOKEN is required")
        if not 5 <= self.heartbeat_seconds <= 300:
            raise ValueError("status provider heartbeat interval must be between 5 and 300 seconds")
        if not self.heartbeat_seconds <= self.inventory_seconds <= 86_400:
            raise ValueError("inventory interval must be at least the heartbeat interval")
        if self.timeout_seconds <= 0:
            raise ValueError("status provider timeout must be positive")

    @classmethod
    def from_env(cls) -> "StatusProviderConfig":
        return cls(
            core_url=os.getenv("SUPERLILY_STATUS_PROVIDER_CORE_URL", ""),
            token=os.getenv("SUPERLILY_STATUS_PROVIDER_TOKEN", ""),
            descriptor_path=Path(
                os.getenv(
                    "SUPERLILY_STATUS_PROVIDER_DESCRIPTOR_PATH",
                    str(DEFAULT_DESCRIPTOR_PATH),
                )
            ),
            heartbeat_seconds=int(
                os.getenv("SUPERLILY_STATUS_PROVIDER_HEARTBEAT_SECONDS", "30")
            ),
            inventory_seconds=int(
                os.getenv("SUPERLILY_STATUS_PROVIDER_INVENTORY_SECONDS", "300")
            ),
            timeout_seconds=float(
                os.getenv("SUPERLILY_STATUS_PROVIDER_TIMEOUT_SECONDS", "5")
            ),
        )


def _load_runtime(descriptor_path: Path) -> tuple[StatusInspector, ProviderToolImplementation]:
    descriptor_source = descriptor_path.read_bytes()
    loaded = load_tool_descriptor(descriptor_source)
    implementation_hash = status_implementation_hash()
    inspector = StatusInspector(loaded, implementation_hash=implementation_hash)
    implementation = ProviderToolImplementation.from_descriptor(
        descriptor_source,
        implementation_hash=implementation_hash,
        budget_enforcement={
            # Output bytes are checked by StatusInspector. A hard wall-time
            # supervisor belongs to the later lease executor and is not
            # claimed by this reporting-only Phase 3a runtime.
            "output_bytes": "hard",
            "wall_time": "unsupported",
        },
    )
    return inspector, implementation


async def run_reporter(config: StatusProviderConfig, *, once: bool = False) -> None:
    inspector, implementation = _load_runtime(config.descriptor_path)
    client = ProviderRegistryClient(
        base_url=config.core_url,
        provider_id=PROVIDER_ID,
        token=config.token,
        tools=[implementation],
        max_concurrency=implementation.loaded_descriptor.descriptor.concurrency_limit,
        timeout_seconds=config.timeout_seconds,
    )
    last_inventory_report = 0.0
    inventory_hash: str | None = None
    async with client:
        while True:
            loop_started = time.monotonic()
            if (
                inventory_hash is None
                or loop_started - last_inventory_report >= config.inventory_seconds
            ):
                inventory = client.build_inventory()
                try:
                    await client.publish_inventory(inventory)
                except ProviderReportError as exc:
                    logger.warning("inventory report unavailable: %s", exc)
                    if once:
                        raise
                else:
                    inventory_hash = inventory.snapshot_hash
                    last_inventory_report = loop_started

            if inventory_hash is not None:
                try:
                    inspector.inspect({"scope": "provider_runtime"})
                except Exception as exc:
                    health = "unavailable"
                    self_test = f"failed:{type(exc).__name__}"
                else:
                    health = "healthy"
                    self_test = "ok"
                heartbeat = client.build_heartbeat(
                    inventory_hash=inventory_hash,
                    health=health,
                    metadata={
                        "execution_enabled": False,
                        "role": "registry_reporter",
                        "self_test": self_test,
                    },
                )
                try:
                    await client.publish_heartbeat(heartbeat)
                except ProviderReportError as exc:
                    logger.warning("heartbeat report unavailable: %s", exc)
                    if once:
                        raise

            if once:
                return
            elapsed = time.monotonic() - loop_started
            await asyncio.sleep(max(0.0, config.heartbeat_seconds - elapsed))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-status-provider")
    subparsers = parser.add_subparsers(dest="command", required=True)
    verify = subparsers.add_parser(
        "verify", help="validate the reviewed descriptor and local implementation"
    )
    verify.add_argument("--descriptor", type=Path, default=DEFAULT_DESCRIPTOR_PATH)
    report = subparsers.add_parser(
        "report", help="publish inventory and heartbeat without accepting execution"
    )
    report.add_argument("--once", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=os.getenv("SUPERLILY_STATUS_PROVIDER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        if args.command == "verify":
            inspector, implementation = _load_runtime(args.descriptor)
            result = inspector.inspect({"scope": "provider_runtime"})
            print(
                json.dumps(
                    {
                        "descriptor_hash": inspector.loaded_descriptor.authority.sha256,
                        "implementation_hash": implementation.inventory_entry.implementation_hash,
                        "output": result,
                        "execution_enabled": False,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                )
            )
        else:
            asyncio.run(run_reporter(StatusProviderConfig.from_env(), once=args.once))
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("status provider failed safely: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
