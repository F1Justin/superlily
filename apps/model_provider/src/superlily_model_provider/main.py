"""Run one explicit Phase 5a DeepSeek shadow attempt."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys

import httpx

from .deepseek import DeepSeekPlanner, DeepSeekPlannerConfig


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="superlily-deepseek-provider")
    parser.add_argument("run_id")
    parser.add_argument(
        "--tool-loop",
        action="store_true",
        help="treat the positional ID as an AgentToolLoop continuation",
    )
    parser.add_argument(
        "--core-url",
        default=os.getenv("SUPERLILY_MODEL_PROVIDER_CORE_URL", ""),
    )
    parser.add_argument(
        "--core-token",
        default=os.getenv("SUPERLILY_MODEL_PROVIDER_TOKEN", ""),
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("SUPERLILY_DEEPSEEK_API_KEY", ""),
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("SUPERLILY_DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=float(os.getenv("SUPERLILY_DEEPSEEK_TIMEOUT_SECONDS", "30")),
    )
    return parser


async def _run(args: argparse.Namespace) -> dict:
    if not args.core_url or not args.core_token:
        raise ValueError("Core URL and model-provider token are required")
    headers = {"Authorization": f"Bearer {args.core_token}"}
    planner_path = (
        f"/v1/agent-tool-loops/{args.run_id}/planner-input"
        if args.tool_loop
        else f"/v1/agent-runs/{args.run_id}/planner-input"
    )
    report_path = (
        f"/v1/agent-tool-loops/{args.run_id}/attempts"
        if args.tool_loop
        else f"/v1/agent-runs/{args.run_id}/attempts"
    )
    async with httpx.AsyncClient(
        base_url=args.core_url.rstrip("/"),
        timeout=args.timeout_seconds,
    ) as client:
        planner_response = await client.get(
            planner_path,
            headers=headers,
        )
        planner_response.raise_for_status()
        planner = DeepSeekPlanner(
            DeepSeekPlannerConfig(
                api_key=args.api_key,
                base_url=args.base_url,
                timeout_seconds=args.timeout_seconds,
            )
        )
        attempt = await planner.plan(planner_response.json())
        idempotency_key = hashlib.sha256(
            (
                f"deepseek-v4-pro:{args.run_id}:"
                f"{attempt.report.raw_output_sha256}"
            ).encode("utf-8")
        ).hexdigest()
        report_response = await client.post(
            report_path,
            headers={
                **headers,
                "Idempotency-Key": idempotency_key,
            },
            json=attempt.report.model_dump(mode="json"),
        )
        report_response.raise_for_status()
        return report_response.json()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(_run(args))
    except (ValueError, httpx.HTTPError) as exc:
        print(f"DeepSeek planner attempt failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
