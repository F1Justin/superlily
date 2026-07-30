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


async def run_attempt(
    target_id: str,
    *,
    tool_loop: bool,
    core_url: str,
    core_token: str,
    api_key: str,
    base_url: str,
    timeout_seconds: float,
) -> dict:
    if not core_url or not core_token:
        raise ValueError("Core URL and model-provider token are required")
    headers = {"Authorization": f"Bearer {core_token}"}
    planner_path = (
        f"/v1/agent-tool-loops/{target_id}/planner-input"
        if tool_loop
        else f"/v1/agent-runs/{target_id}/planner-input"
    )
    report_path = (
        f"/v1/agent-tool-loops/{target_id}/attempts"
        if tool_loop
        else f"/v1/agent-runs/{target_id}/attempts"
    )
    async with httpx.AsyncClient(
        base_url=core_url.rstrip("/"),
        timeout=timeout_seconds,
        trust_env=False,
    ) as client:
        planner_response = await client.get(
            planner_path,
            headers=headers,
        )
        planner_response.raise_for_status()
        planner = DeepSeekPlanner(
            DeepSeekPlannerConfig(
                api_key=api_key,
                base_url=base_url,
                timeout_seconds=timeout_seconds,
            )
        )
        attempt = await planner.plan(planner_response.json())
        idempotency_key = hashlib.sha256(
            (
                f"deepseek-v4-pro:{target_id}:"
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


async def _run(args: argparse.Namespace) -> dict:
    return await run_attempt(
        args.run_id,
        tool_loop=args.tool_loop,
        core_url=args.core_url,
        core_token=args.core_token,
        api_key=args.api_key,
        base_url=args.base_url,
        timeout_seconds=args.timeout_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    active_argv = list(sys.argv[1:] if argv is None else argv)
    if active_argv and active_argv[0] == "serve":
        from .server import serve

        return serve(active_argv[1:])
    args = _parser().parse_args(active_argv)
    try:
        result = asyncio.run(_run(args))
    except (ValueError, httpx.HTTPError) as exc:
        print(f"DeepSeek planner attempt failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
