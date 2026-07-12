# Phase 2 completion review

This review is the gate before Phase 3 Tool Registry work. It covers Core,
contracts, migrations, both bridges, deployment defaults, production data
boundaries, and the live canary sequence.

## Review fixes

- Correlation v3 is limited to the QQ group-message domain actually validated
  in production. Private messages remain separate.
- Native `real_seq` is the content-free key; account-local IDs and text cannot
  become fallback identity. Native time is a conflict guard.
- Canonical decisions are recomputed from all observations and one row is
  revised rather than duplicated per account.
- Direct and reported reply references must match platform, canonical
  conversation, and causal time. Resolver and response-attribution lookups have
  dedicated indexes.
- Runtime trigger coverage is bound to plugin and matcher semantics. Composite
  rules, custom constraints, or matcher permissions are marked incomplete.
- Sensitive and non-public commands remain shadow-only until Core has a sender
  authorization model.
- PostgreSQL advisory locking is backed by a partial unique index that permits
  only one enforced `allow` owner per source event.
- Heartbeat liveness uses the earlier of bridge time and Core receipt time and
  rejects stale updates. A future or delayed heartbeat cannot keep an instance
  online.
- A response that arrives before its event is linked when the event later
  appears, preserving outcome auditability.
- Generic token/database credential keys are redacted, HTTP URL userinfo and
  queries are removed, and bridge HTTP clients ignore ambient proxy settings.
- JSON-encoded OneBot segment payloads are parsed only in JSON container
  fields and recursively sanitized; ordinary user text is not reinterpreted.
- Reporter queues remain bounded and all reporting/claim failures are fail-open.
- Lily runtime snapshot hashing uses the same canonical row ordering as Core;
  the 28-plugin production snapshot is contract- and hash-verified.
- Alembic 0001-to-0010 upgrade and downgrade paths are verified on SQLite,
  including batch column changes and the partial claim-owner index; a fresh
  PostgreSQL 17 database also reaches the single 0010 head.
- Claim evaluation backfills a missing decision for a replayed legacy
  observation before applying the strong-correlation abstention gate.
- Authentication configuration rejects reused ingest tokens and admin/ingest
  token overlap before the service starts.
- Nekro response attribution now remembers a ready Core selection of the
  Nekro instance as well as Nekro's local ToMe flag. This covers preset-name
  triggers that Nekro recognizes only after the plugin callback; the
  conversation-local association remains one-shot and explicitly inferred.
- Policy v2 treats messages authored by a known Lily/Nekro bot identity as
  `observe_only / bot_message_observed`. Bot outputs remain available for reply
  resolution but no longer create false talk/command outcomes from their own
  text.

## Deliberate residual boundaries

- The old Lily and Nekro databases are not bulk-copied. Their schemas do not
  contain a verified cross-account key, and overlap would duplicate live Core
  records. See `HISTORY_DRY_RUN.md`.
- Missing identity, a missing second observer, Core outage, or any uncertain
  control-plane input preserves the bots' old behavior. Fail-open therefore
  prioritizes availability over exactly-once response ownership.
- Runtime triggers that are absent from the reviewed static registry remain
  visible but cannot authorize enforcement.
- Nekro's public response hook exposes no native trigger ID. Its one-shot
  conversation-local Core-target-or-ToMe association is explicitly labeled as
  inference.
- The current phase routes existing bots; it does not execute tools, evaluate
  command arguments, or replace the plugins' own permission checks.

## Entrance gate for Phase 3

Phase 3 may start only after all of the following are recorded in
`ACCEPTANCE.md`:

1. fresh PostgreSQL and SQLite suites, plus a fresh 0001-to-head migration;
2. deployed runtime inventory with uncovered/incomplete matchers reviewed;
3. deterministic resolution of only uniquely supported pending links;
4. a controlled decision/actual-response sample;
5. shadow claims from both bridges with zero enforcement;
6. one exact QQ canary covering command, talk, reply, ordinary, and outage
   behavior;
7. a stable 24-hour post-deployment evidence window and a final secret/URL
   storage audit.
