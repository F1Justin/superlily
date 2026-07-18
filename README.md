# Superlily

Superlily is the Lily Core described in [`manifesto.md`](manifesto.md). Phase 1
provides the observability spine; Phase 2 adds canonical correlation,
deterministic decisions, authenticated runtime command inventory, outcome
auditing, and an opt-in fail-open claim canary. Tool execution remains outside
Core. Phase 3a has deployed authority contracts, bounded JSON Schema/JCS
validation, provider inventory models, a verifier CLI, and shared vectors in
zero-authority mode. The current priority is the authority-neutral C0-D
collection-reliability packet; production tool authority and execution remain
absent and disabled.

The runtime is deliberately fail-open: telemetry failures never block either
bot, and claim failures preserve their existing behavior.

## Layout

- `packages/contracts`: versioned ingestion/tool schemas, canonical authority
  validation, shared vectors, and payload sanitization.
- `apps/core`: FastAPI ingestion/query service and database models.
- `bridges/lily_nonebot`: NoneBot event/API observer.
- `bridges/nekro`: Nekro local plugin observer.
- `deploy`: Docker Compose and integration examples.
- `docs`: operations, security, and acceptance criteria.

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for local setup.
The implementation sequence and cross-phase gates are in
[`docs/ROADMAP.md`](docs/ROADMAP.md); the next-phase protocol is specified in
[`docs/PHASE3_TOOL_REGISTRY.md`](docs/PHASE3_TOOL_REGISTRY.md).
The durable product consensus for archive-oriented event collection, nested
merged forwards, platform actions, progressive tool disclosure, fast-path
chat behavior, and cost-aware model routing is in
[`docs/COLLECTION_AND_AGENT_CONSENSUS.md`](docs/COLLECTION_AND_AGENT_CONSENSUS.md).
The implemented C0-D1 through C0-D3 boundary and the still-open action/rollout gates
are tracked separately in [`docs/C0D_ACCEPTANCE.md`](docs/C0D_ACCEPTANCE.md).
Renderer, agent, Watchdog, platform, memory, event, avatar, and optional
runtime-replacement plans are decomposed in
[`docs/FUTURE_PHASES_DESIGN.md`](docs/FUTURE_PHASES_DESIGN.md).
The selected three-account high-availability topology is detailed in
[`docs/PHASE6_THREE_ACCOUNT_HA.md`](docs/PHASE6_THREE_ACCOUNT_HA.md).
The final Phase 2 production gate is reproducible from
[`docs/PHASE2_FINAL_AUDIT.md`](docs/PHASE2_FINAL_AUDIT.md).
