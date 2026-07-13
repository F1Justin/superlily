# Superlily

Superlily is the Lily Core described in [`manifesto.md`](manifesto.md). Phase 1
provides the observability spine; Phase 2 adds canonical correlation,
deterministic decisions, authenticated runtime command inventory, outcome
auditing, and an opt-in fail-open claim canary. Tool execution remains outside
Core until Phase 3.

The runtime is deliberately fail-open: telemetry failures never block either
bot, and claim failures preserve their existing behavior.

## Layout

- `packages/contracts`: versioned ingestion schemas and payload sanitization.
- `apps/core`: FastAPI ingestion/query service and database models.
- `bridges/lily_nonebot`: NoneBot event/API observer.
- `bridges/nekro`: Nekro local plugin observer.
- `deploy`: Docker Compose and integration examples.
- `docs`: operations, security, and acceptance criteria.

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for local setup.
The implementation sequence and cross-phase gates are in
[`docs/ROADMAP.md`](docs/ROADMAP.md); the next-phase protocol is specified in
[`docs/PHASE3_TOOL_REGISTRY.md`](docs/PHASE3_TOOL_REGISTRY.md).
Renderer, agent, Watchdog, platform, memory, event, avatar, and optional
runtime-replacement plans are decomposed in
[`docs/FUTURE_PHASES_DESIGN.md`](docs/FUTURE_PHASES_DESIGN.md).
The final Phase 2 production gate is reproducible from
[`docs/PHASE2_FINAL_AUDIT.md`](docs/PHASE2_FINAL_AUDIT.md).
