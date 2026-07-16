# Phase 2 completion review

This review is the gate before Phase 3 Tool Registry work. It covers Core,
contracts, migrations, both bridges, deployment defaults, production data
boundaries, and the live canary sequence. The policy-v4 24-hour run is not a
sign-off: the final row-level/code review exposed additional identity,
attribution, command-structure, private-recipient, and claim-handshake defects.
Those findings define policy v5 and require a fresh deployment, controlled
samples, and a new 24-hour window before this review can be signed.

## Review fixes

- Correlation v3 is limited to the QQ group-message domain actually validated
  in production. Private messages remain separate.
- Native `real_seq` is the content-free key; account-local IDs and text cannot
  become fallback identity. Native time is a conflict guard.
- Each bridge uses the same versioned `qq:source:v2:<sha256>` local message
  identity over canonical conversation and allowlisted native identity. Reused
  short NapCat `message_id` values do not reuse a source/idempotency key. Core
  rejects source/idempotency reuse with conflicting native identity instead of
  returning another event's decision.
- A matching native time permits identical strong fingerprints to merge even
  when the two adapters' normalized `occurred_at` values differ by more than
  the former correlation window. A database audit detects any remaining split
  fingerprint.
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
- Generic token/database credential keys are redacted, URL/URI userinfo and
  queries are removed for every scheme, and bridge HTTP clients ignore ambient
  proxy settings.
- JSON-encoded OneBot segment payloads are parsed only in JSON container
  fields and recursively sanitized; ordinary user text is not reinterpreted.
- URL-typed fields, `file`, and `platform_id` remove userinfo, query, and
  fragment data for every scheme, including QQ `mqqapi`/`mqzone` deep links
  rather than only HTTP(S). Custom `scheme://` scalars are covered as well.
- Reporter queues remain bounded and all reporting/claim failures are fail-open.
- Claim and background-ingestion deadlines are independent. Claims retain a
  one-second bounded fail-open path, while the background reporter gets two
  seconds so a committed request is not routinely counted as dropped under
  normal PostgreSQL latency.
- Lily runtime snapshot hashing uses the same canonical row ordering as Core;
  the 28-plugin production snapshot is contract- and hash-verified.
- Alembic 0001-to-0010 upgrade and downgrade paths were previously verified on
  SQLite/PostgreSQL. The new `0011_claim_ack` migration and the complete
  `base -> head -> base -> head` chain remain required evidence after the
  policy-v5 implementation is final; this document does not pre-claim them.
- Claim evaluation backfills a missing decision for a replayed legacy
  observation before applying the strong-correlation abstention gate.
- Authentication configuration rejects reused ingest tokens and admin/ingest
  token overlap before the service starts.
- Nekro response attribution mirrors its per-chat scheduler: an idle/debounced
  source, the current task source, and a pending next-task source remain
  distinct. All outputs from the same task retain `task_context`; a second
  message cannot overwrite the first task's attribution. Lily retains its
  native `event_context`. Canonical target metadata is correlation-only and
  does not grant authority.
- Policy v5 treats messages authored by a known Lily/Nekro bot identity as
  `observe_only / bot_message_observed`. Bot outputs remain available for reply
  resolution but no longer create false talk/command outcomes from their own
  text.
- Policy v5 records `command_eligible` separately from concatenated display
  text. NoneBot-compatible leading-segment structure prevents an `at` to
  another user, image, or other non-text prefix from becoming a Lily command.
- Resolved replies to Nekro route to Nekro whenever talk is enabled; replies to
  Lily remain observed. Ambiguous/conflicting replies remain observed, while
  an unresolved/reply-to-other item needs an explicit Lily summon/known-bot
  mention before it may route to Nekro.
- Private routing is recipient-bound. Lily-private traffic cannot hand an
  ordinary conversation to Nekro, and Nekro-private traffic cannot invoke a
  Lily command that Lily never received.
- Heartbeats carry a typed, versioned platform-capability snapshot. The first
  QQ profile is deliberately limited to text, image, reply, and mention, so
  future tools/renderers can degrade without guessing from adapter names.
- The production Python base image is pinned by digest and the validated
  transitive dependency set is constrained. Rebuilding the same revision no
  longer silently selects newer package releases.
- Claim ownership uses deny-installation acknowledgement, not merely
  deny-before-allow database order. A target allow requires all prior observed
  peers to have acknowledged enforced denies; a committed deny whose HTTP
  response was lost cannot manufacture a fictitious exclusive owner. Lily can
  acknowledge after its event-scoped outbound guard is installed. Nekro now
  supplements `BLOCK_TRIGGER` with an exact-source OneBot send guard bound to
  the active event and task-aware response tracker. It records same-event send
  attempts made before its plugin callback and acknowledges only when the
  event matches, no prior send exists, and both guard paths are installed.
  Missing context, prior output, or ACK failure remains a safe abstention.
- Canonical decision recomputation has its own per-source transaction lock, so
  observation, response, reference-backfill, and claim paths cannot overwrite
  one another's revision/features.
- The PostgreSQL 17.10 image is digest-pinned and Core exposes a Compose
  readiness healthcheck rather than treating a live process as ready.

## Deliberate residual boundaries

- The manifesto's early `health_checks`, `conversation_configs`, and
  `identity_mappings` sketches were not materialized as empty speculative
  tables. Readiness plus instance/status transitions provide the health
  equivalent. Formal conversation/principal mappings are required before any
  administrator write tool or second platform, but public read/compute Phase
  3 slices do not depend on invented identity authority.
- Redis is intentionally absent. PostgreSQL transaction/uniqueness locks and
  bounded bridge queues have explicit current responsibilities; Phase 3 may
  add Redis only when distributed leases/rates/queues need it.
- The old Lily and Nekro databases are not bulk-copied. Their schemas do not
  contain a verified cross-account key, and overlap would duplicate live Core
  records. See `HISTORY_DRY_RUN.md`.
- Missing identity, a missing second observer, Core outage, or any uncertain
  control-plane input preserves the bots' old behavior. Fail-open therefore
  prioritizes availability over exactly-once response ownership.
- Runtime triggers that are absent from the reviewed static registry remain
  visible but cannot authorize enforcement.
- Nekro's public response hook still exposes no native trigger ID. The
  task-bound scheduler association is explicitly labeled attribution rather
  than a platform-native receipt, and remains subject to response/source
  consistency audits.
- OneBot send timeouts have ambiguous completion: the platform may have sent
  before the local API timed out. They remain `success=false` with
  `completion_status=ambiguous`, are reviewed separately, and are never blindly
  retried or reported as a confirmed failure.
- The current phase routes existing bots; it does not execute tools, evaluate
  command arguments, or replace the plugins' own permission checks.

## Entrance gate for Phase 3

Phase 3 may start only after all of the following are recorded in
`ACCEPTANCE.md`:

1. fresh PostgreSQL and SQLite suites, plus a fresh 0001-to-0011 migration and
   upgrade/downgrade chain;
2. deployed runtime inventory with uncovered/incomplete matchers reviewed;
3. deterministic resolution of only uniquely supported pending links;
4. a controlled decision/actual-response sample;
5. shadow claims from both bridges with zero enforcement;
6. one exact QQ canary covering command eligibility, private recipient policy,
   resolved/unresolved/ambiguous reply cases, task-bound response attribution,
   deny acknowledgement/lost-response behavior, ordinary traffic, ambiguous
   send completion, and Core outage;
7. a stable new 24-hour post-policy-v5 deployment evidence window and a final
   secret/custom-URI/raw storage audit.

The exact close-out procedure and zero-violation sets are defined in
`PHASE2_FINAL_AUDIT.md`.
