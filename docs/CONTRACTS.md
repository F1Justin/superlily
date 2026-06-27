# Ingestion contracts

The wire schema is version `1.0` and the HTTP surface is under `/v1`.

## Write APIs

- `POST /v1/events` requires a bearer token and `Idempotency-Key`.
- `POST /v1/responses` requires a bearer token and `Idempotency-Key`.
- `POST /v1/heartbeats` requires a bearer token.

Each token is bound to exactly one `instance.instance_id`. A Lily token cannot
submit a Nekro payload.

For event ingestion, the request's `source_event_id` is the reporting
account's event identifier. Core stores it as `reported_source_event_id` and
returns the canonical `source_event_id` in the response. Different bot
accounts may therefore submit different request IDs and receive the same
canonical ID. The account-local message ID is retained separately as
`platform_message_id`.

Events may include `references`. Phase 2a records these as first-class
`event_links`. A reference can provide a canonical or reported
`source_event_id`, an account-local `platform_message_id`, and optional target
conversation/sender hints. Core currently extracts QQ `reply` segments as
`reply_to` references. Resolved links point to a canonical `source_event_id`;
unresolved or ambiguous links are retained for later backfill instead of being
dropped.

Phase 2b creates one shadow `event_decision` per canonical source event. A
decision has a policy version, decision type, optional target instance,
confidence, reason, and feature snapshot. Decisions are observational; bridges
do not need to request or obey them in this phase.

Command decisions are driven by the configured command registry, not by a small
hard-coded prefix list. The default registry lives at
`apps/core/config/command_registry.toml` and can be overridden with
`SUPERLILY_COMMAND_REGISTRY_PATH`. Decision features include the registry
version and the matched command rule, when any. If the registry cannot be read,
event ingestion stays fail-open and records the registry error in the decision
features.

Cross-account correlation is conservative and currently applies only to QQ
text messages with a sender. It uses normalized conversation identity, sender,
text, and the configured short time window. Ambiguous or non-text events stay
separate rather than risk a false merge.

## Read APIs

- `GET /health/live` only proves the process is serving HTTP.
- `GET /health/ready` also checks PostgreSQL.
- `GET /v1/events/recent`, `/v1/responses/recent`,
  `/v1/event-links/recent`, `/v1/decisions/recent`,
  `/v1/command-registry`, `/v1/events/{source_event_id}/context`, and
  `/v1/instances` require the admin bearer token.

`/v1/instances` derives `offline` when the most recent heartbeat is older than
the configured threshold. Heartbeats update the latest instance row; only
reported status changes append history.

The authoritative Pydantic definitions are in
`packages/contracts/src/superlily_contracts/models.py`.
