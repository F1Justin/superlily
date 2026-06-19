# Superlily

Superlily is the observability-first Lily Core described in
[`manifesto.md`](manifesto.md). The first milestone records normalized events,
responses, and instance liveness without taking control of Lily or Nekro.

The runtime is deliberately fail-open: bridge delivery failures must never
block either existing bot.

## Layout

- `packages/contracts`: versioned ingestion schemas and payload sanitization.
- `apps/core`: FastAPI ingestion/query service and database models.
- `bridges/lily_nonebot`: NoneBot event/API observer.
- `bridges/nekro`: Nekro local plugin observer.
- `deploy`: Docker Compose and integration examples.
- `docs`: operations, security, and acceptance criteria.

See [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) for local setup.

