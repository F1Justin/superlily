# Ingestion contracts

The wire schema is version `1.0` and the HTTP surface is under `/v1`.

## Write APIs

- `POST /v1/events` requires a bearer token and `Idempotency-Key`.
- `POST /v1/responses` requires a bearer token and `Idempotency-Key`.
- `POST /v1/heartbeats` requires a bearer token.

Each token is bound to exactly one `instance.instance_id`. A Lily token cannot
submit a Nekro payload.

## Read APIs

- `GET /health/live` only proves the process is serving HTTP.
- `GET /health/ready` also checks PostgreSQL.
- `GET /v1/events/recent`, `/v1/responses/recent`, and `/v1/instances` require
  the admin bearer token.

`/v1/instances` derives `offline` when the most recent heartbeat is older than
the configured threshold. Heartbeats update the latest instance row; only
reported status changes append history.

The authoritative Pydantic definitions are in
`packages/contracts/src/superlily_contracts/models.py`.

