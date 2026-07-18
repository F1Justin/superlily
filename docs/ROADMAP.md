# Superlily execution roadmap

This document is the authoritative implementation sequence after the Phase 2
event-routing foundation. `manifesto.md` remains the architectural vision;
phase-specific design documents define implementable contracts and acceptance
gates.

Detailed post-Tool-Registry design: `docs/FUTURE_PHASES_DESIGN.md`. It defines
the shared boundaries, internal work packets, failure models, and exit gates
for Phases 4–11 without authorizing those phases to start early.

The durable product decisions behind collection completeness, progressive
tool disclosure, Unix-style resource exploration, natural-language command
compatibility, fast-path chat behavior, and cost-aware model routing are
recorded in `docs/COLLECTION_AND_AGENT_CONSENSUS.md`.

## Current position

Phase 2 is complete and signed in `ACCEPTANCE.md`; the Phase 3 entrance gate is
open, and the Phase 3a zero-authority Registry is deployed. Correlation v3,
runtime command inventory, response attribution, the exact-conversation claim
canary, typed platform capabilities, policy v6, migration `0011_claim_ack`,
Lily bridge 0.3.2, and Nekro bridge 0.3.4 are deployed. A deterministic reply to another
person without an explicit Lily summon denies every observing bot in claim
scope, while an explicit summon still routes to Nekro. The canary allowlist
remains exactly `qq:group:708309706`.

The policy-v4 replacement window reached 24 hours, but the complete row-level
and code review rejected it as final evidence. It exposed short-message-ID
collision risk, split strong fingerprints under adapter timestamp skew,
leading-segment command false positives, private/reply policy gaps, Nekro
task-attribution races, a committed-deny/response-loss claim hole, and
ambiguous send timeouts counted as confirmed failures.

The completed policy-v5 window, policy-v6 counterfactual replay, live
no-summon/summon pair, post-deployment claim/ACK/response checks, long-task
attribution repair, and operator sign-off are recorded in `ACCEPTANCE.md`.
Earlier rejected windows remain diagnostic evidence. Migration
`0012_tool_registry`, local Git-bound imports, separately authenticated
provider inventory/heartbeat endpoints, and the admin-only
desired/reported/effective view are deployed in zero-authority mode. The
authority-neutral `0013_collection_reliability` migration and C0-D1 through
C0-D4 bridge durability, diagnostics and factual platform actions are also
deployed. The complete
SQLite/PostgreSQL 17 suite, fresh migration, downgrade/re-upgrade,
backup and production drift checks pass. Production has zero Provider
credentials, descriptors, active or eligible tools, and execution remains
off. Only C0-D5 controlled fault/behavior evidence remains before Phase 3b.
The next Phase 3 gate is the shared Provider SDK plus
the separately reviewed real `status.inspect` authority and runtime evidence;
no invocation or lease surface exists.

Before Phase 3b adds invocation or lease authority, the current operator
priority is the authority-neutral `C0-D` collection-reliability packet. It
pulls forward durable bridge spools, commit receipts, capture profiles,
sanitization, normalized basic platform actions, replay and coverage
diagnostics. The larger `C0-A` archival packet follows C0-D but is not a Phase
3b correctness dependency. Phase 2's signed routing acceptance remains valid,
and Phase 3a's zero-authority Registry remains deployed; neither packet
changes bot response behavior or tool authority.

## Sequencing rules

1. A phase begins only after the previous phase's acceptance evidence exists,
   rollback is documented, and production defaults remain safe.
2. Contracts and ledgers precede execution. Execution precedes model autonomy.
3. Read-only, deterministic, public operations migrate before costly,
   privacy-sensitive, state-changing, or administrator operations.
4. Tools return structured results and artifacts; tools do not send platform
   messages. Rendering and delivery are separate responsibilities.
5. Missing identity, authority, capability, registry freshness, confirmation,
   budget, or provider health always reduces authority rather than expanding
   it.
6. Existing command entry points stay available until their tool-backed path
   has passed shadow comparison, exact-conversation canary, and rollback.

## Immediate authority-neutral packet: C0-D collection reliability

C0-D is an earlier-layer reliability repair scheduled after deployed Phase 3a
and before Phase 3b. Its detailed scope and acceptance criteria are in
`docs/COLLECTION_AND_AGENT_CONSENSUS.md`.

- Define exact capture profiles, versioned sanitizer/completeness metadata,
  and normalized basic platform actions; reaction capture records facts and
  assigns no feedback semantics.
- Keep image bytes out of PostgreSQL while retaining ordered placeholders and
  available metadata. Any future binary retention uses bounded object storage.
- Replace loss-prone in-memory-only telemetry with idempotent durable bridge
  spools, commit receipts, replay, watermarks, lag/gap diagnostics, quotas, and
  quarantine.
- Preserve per-account observations and resolve only with verified strong
  identity. Old or unresolved data is never merged by text/time similarity.

C0-D is complete only when outage/restart replay loses no controlled records,
Lily and Nekro basic action notices are represented with honest completeness
status, receipt/coverage diagnostics and media policy are enforced, and
command/claim/Registry behavior is unchanged.

After C0-D is stable, `C0-A` adds nested merged-forward expansion,
`archive_full` rollout, portable exports, reconstruction, retention/deletion
propagation, and old-history import. C0-A and Phase 3b may be scheduled
independently; Phase 3b does not wait for archival edge cases.

## Dependency shape

The numbered phases describe release gates, not a ban on preparatory design.
Contracts for a later phase may be designed early, but production authority is
enabled only in dependency order:

```text
Phase 2 event identity / decisions / claims / capabilities
    -> Phase 3 tool contract / invocation ledger / provider protocol
        -> Phase 4 renderer IR / artifact delivery / capability degradation
            -> Phase 5 natural-language planning and tool loop

Phase 3 health + audit -> Phase 6 Watchdog
Phase 4 adapter boundary -> Phase 7 additional platforms
Phase 3 query tools + Phase 5 on-demand calls -> Phase 8 Memory as Tool
Phases 3/4/6/7 -> Phase 9 event operations
Phases 4/7/9 -> Phase 10 Fumo and avatar adapters
All stable boundaries -> Phase 11 optional legacy runtime replacement
```

## Phase 3: Tool Registry and controlled execution

Detailed design: `docs/PHASE3_TOOL_REGISTRY.md`. Phase acceptance is in
`docs/PHASE3_ACCEPTANCE.md`; the future operator UI and mutation boundary are in
`docs/CONTROL_PLANE.md`.

### 3a. Authoritative tool descriptors

- Define versioned input/output JSON Schemas, side-effect class, permission,
  confirmation, timeout, rate, concurrency, resource, privacy, provider, and
  required-output-capability metadata.
- Accept authenticated runtime provider snapshots, but keep reviewed authority
  separate from runtime discovery just as Phase 2 separates static command
  review from matcher inventory.
- Expose admin audit views for loaded, stale, missing, incompatible, and
  unreviewed tools.
- Use a Git-tracked reviewed descriptor bundle as the authority source. Store
  immutable canonical descriptor copies/hashes and lifecycle records in the
  database; neither provider inventory nor the control panel edits descriptor
  authority in 3a.
- Separate stable provider registration/authentication from dynamic inventory
  and heartbeat health. Provider credentials are not bot-ingest/admin tokens.
- Do not execute tools in 3a.

Exit gate: descriptor canonicalization and hashes are deterministic; unknown or
stale providers cannot become callable; schema compatibility and authority are
covered on SQLite and PostgreSQL.

### 3b. Invocation ledger and provider lease protocol

- Add an auditable invocation state machine, attempts, confirmations, leases,
  fencing tokens, deadlines, cancellation, structured errors, and artifact
  references.
- Providers pull authenticated leases from Core. Core does not open inbound
  ports into Lily/Nekro and does not run plugin code inside the API process.
- Validate input before queueing and output before success. Capture the exact
  descriptor, principal, policy, capability, and budget snapshots used.
- Apply idempotency per source event/tool/request and never retry an ambiguous
  state-changing completion automatically.
- Ship `off`, `ledger_only`, exact canary, and enforced modes plus independent
  global stop, tool suspension, and provider quarantine before a real lease.
  Canary scope binds exact tool/version, conversation, caller, and provider.
- Use database time for leases/deadlines. Specify cancellation, reaping, late
  completion, invalid output, clock skew, unknown completion, and starvation
  transitions before implementation.
- Implement reserve/upload/finalize content-addressed artifacts before
  `latex.render` or image-producing Wolfram output can succeed.

Exit gate: crash/restart, duplicate delivery, expired lease, timeout,
cancellation, malformed result, provider outage, and Core outage all have
tested deterministic outcomes.

### 3c. First providers and migration order

Recommended order:

1. `status.inspect`: read-only, deterministic, small structured output.
2. `wolfram.run`: bounded compute, persistent worker, explicit time/memory and
   expression-size budgets.
3. `latex.render`: read-only artifact production with MIME/hash/size metadata.
4. `markdown.render_image`: artifact production after renderer boundaries are
   stable enough to avoid duplication with Phase 4.
5. `history.search`: read-only but privacy-sensitive; requires conversation
   scope and audit policy first.
6. Configuration, moderation, announcements, restarts, and other writes only
   after sender authorization and confirmation are implemented.

Each old command becomes a compatibility adapter that creates a typed tool
invocation. The provider may initially wrap existing code, but command parsing,
permission checks, tool execution, result structure, rendering, and sending
must become separately observable steps.

### 3d. Shadow and canary migration

- Shadow only metadata and read-only results; never double-execute a write.
- Compare old-command output with tool-backed structured output using bounded,
  redacted diff records.
- Canary one tool and one exact conversation at a time.
- Keep the old path as rollback until latency, error rate, result equivalence,
  resource budgets, and audit completeness pass a stable window.

Phase 3 exit gate: at least `status.inspect`, `wolfram.run`, and
`latex.render` use the common descriptor and invocation protocol; command
compatibility remains; no natural-language model has execution authority yet.
The control panel may expose read-only effective state during Phase 3, but
mutating operator controls remain gated by the roles/session/audit requirements
in `CONTROL_PLANE.md`.

## Phase 4: Unified Renderer

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-4-unified-renderer-and-delivery-boundary`.

### Deliverables

- Define a versioned `RenderDocument` intermediate representation for text,
  headings, tables, code, math, images, cards, progress, warnings, and artifact
  references.
- Add content-addressed artifacts with MIME, hash, byte size, dimensions,
  provenance, TTL, access scope, and retention.
- Implement renderer providers separately from platform delivery.
- Negotiate against the Phase 2 capability snapshot and record every
  degradation, such as Markdown to image or image to plain text.
- Cache only deterministic, non-sensitive render results; include renderer
  version and normalized input in cache keys.

### Safety and exit gate

Untrusted HTML/SVG/Markdown is sanitized; local-file and remote-fetch policy is
explicit; font and image work has resource limits. Wolfram, LaTeX, status, and
help results render identically through command and tool paths, with QQ text
and image fallbacks tested. Tools no longer call platform send APIs directly.

## Phase 5: Natural-language Tool Calling

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-5-natural-language-planning-and-controlled-tool-loop`.

### 5a. Planner without execution

- Build the light default context: system policy, current message/reply graph,
  short conversation window, and only eligible tool summaries.
- Record proposed calls and explanations without execution.
- Measure false calls, missed calls, schema validity, and permission requests.

### 5b. Read-only execution loop

- Allow a bounded number of calls and model turns with total time/token/cost
  budgets.
- Core validates every call against the Phase 3 descriptor and principal
  policy; the model never talks directly to providers.
- Expose `docs.search`, `state.get`, and later `history.search` only on demand;
  there is no default RAG injection.

### 5c. Confirmed writes

- State-changing tools require explicit confirmation bound to principal, tool,
  normalized arguments, scope, expiry, and invocation ID.
- A changed argument or expired confirmation returns to proposal state.
- High-risk operations can require a second administrator or an out-of-band
  channel.

Exit gate: adversarial prompts, tool-result injection, loops, provider errors,
timeouts, budget exhaustion, confirmation replay, and model failover are
covered; command behavior remains independent of model availability.

## Phase 6: Three-account coordination and Watchdog

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-6-watchdog-incidents-and-role-failover`.
Three-account collection/failover design:
`docs/PHASE6_THREE_ACCOUNT_HA.md`.

Availability-priority decision: the authority-neutral `HA-0` durable ingress
spool and coverage packet is pulled forward into C0-D after Phase 3a and before
Phase 3b. This does not deploy Reserve or enable failover egress; the remaining
Phase 6 release gates stay in numbered order.

- Model Command, Talk, and Watchdog as explicit roles with capability and
  health snapshots, not hard-coded account IDs.
- Run collection active/active/active across Command, Talk, and a continuously
  connected silent Reserve account. Response remains active/passive: Reserve
  speaks only while holding a bounded logical-role lease.
- Add durable per-adapter ingress spools and coverage/watermark diagnostics;
  another in-memory observer does not by itself guarantee no message loss.
- Define a degradation matrix per tool: primary provider, permitted fallback,
  required health, reduced limits, and forbidden failover.
- Reserve's adapter collects protected ordinary chat for ingestion coverage,
  but Watchdog/incident logic consumes only health, coverage, status, and
  incident events. It cannot silently acquire chat context or tool authority.
- Use leases/fencing for failover, cooldown and hysteresis for recovery, and
  an administrator-visible incident timeline.
- When automatic failover is active, managed egress becomes lease-required;
  ingress remains durable and fail-open. This prevents a partitioned primary
  and Reserve from speaking simultaneously.

Exit gate: loss of each bot, NapCat, provider, Core, PostgreSQL, and network
path has a tested outcome; failback does not duplicate a reply or invocation.

## Phase 7: Additional platform entry points

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-7-additional-platforms-and-web-admin`.

Order: Telegram administrator private chat, Web Admin, then lower-priority
WeChat/Discord/email/live-stream adapters.

- Keep platform adapters limited to identity, events, references,
  capabilities, delivery, and platform acknowledgements.
- Map principals across platforms explicitly; never equate display names.
- Web Admin initially exposes read-only audit and health, then confirmation and
  configuration behind stronger authentication, CSRF protection, and an
  immutable audit trail.
- Cross-platform forwarding is a state-changing tool with consent and privacy
  policy, not an adapter shortcut.

Exit gate: the same event/tool/render contracts work on QQ and one second
platform without platform-specific branches in tool providers.

## Phase 8: Memory as Tool

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-8-retrieval-and-memory-as-tool`.

Build in privacy order:

1. `state.get`: explicit structured group/task/event state.
2. `docs.search`: curated documentation with source/version metadata.
3. `history.search`: scoped lexical/SQL search over authorized conversations.
4. `memory.lookup`: curated long-lived facts with provenance and expiry.
5. Embeddings/reranking only where exact retrieval is insufficient.

Every result carries source, scope, time, confidence, and redaction metadata.
Writes require a separate reviewed path; inferred personal profiles are not
silently persisted. Retention, deletion, export, and consent are acceptance
requirements, not later cleanup.

## Phase 9: Event operations

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-9-event-operations`.

- Treat programs, tickets, check-in, raffle pools, staff actions, timers, OBS
  scenes, and announcements as structured state and tools.
- Use an event-scoped role model and a rehearsal/simulation mode.
- Make raffle inputs and draws reproducible and auditable; secrets and attendee
  data receive stricter retention.
- Add offline/degraded runbooks so a venue network failure does not stall the
  event.

Exit gate: a full rehearsal can be replayed from the audit log, and every
state-changing action has confirmation, operator identity, and rollback or
compensation behavior.

## Phase 10: Fumo and avatar adapters

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-10-fumo-and-avatar-adapters`.

- Define versioned output intents: `speak`, `subtitle`, `emotion`, `action`,
  `display_card`, and `attention`.
- Devices and Live2D/OBS clients render intents but own no planning or tool
  authority.
- Add session leases, physical emergency stop/mute, privacy indicators for
  microphones/cameras, bounded queues, and safe offline behavior.

Exit gate: disconnects, stale commands, duplicated intents, audio feedback,
and operator override are tested; no device credential grants Core admin tool
authority.

## Phase 11: Optional legacy runtime replacement

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-11-optional-legacy-runtime-replacement`.

Replacement is evidence-driven and component-by-component. A custom OneBot or
Satori adapter, plugin host, agent loop, runner, sandbox, or admin UI replaces
NoneBot/Nekro only after the shared contracts have proven that component is a
replaceable boundary. Each replacement needs traffic shadowing, behavior and
latency comparison, a data-migration boundary, and immediate rollback.

The project is complete without full replacement if the legacy runtimes remain
healthy providers behind stable Lily Core contracts.

## Cross-phase production gates

Every authority-increasing release records:

- schema migration upgrade/downgrade on SQLite and the production PostgreSQL
  major version;
- deterministic contract/hash compatibility;
- authentication and principal binding;
- permission, confirmation, capability, and budget decisions;
- idempotency/concurrency/crash behavior;
- redaction, retention, and artifact privacy audit;
- latency/error/resource baselines and alert thresholds;
- exact canary scope, fail-open/fail-closed choice, rollback, and a stable
  evidence window;
- updated operator runbook and acceptance checklist.
