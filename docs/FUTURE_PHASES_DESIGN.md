# Phase 4–11 design: renderer, agency, operations, and runtime evolution

## Purpose and authority boundary

This document turns the long-range roadmap after Tool Registry into staged,
testable work. It is design input, not authorization to start these phases.
Production authority still advances in the order defined by `ROADMAP.md`, and
Phase 3 implementation still waits for the final Phase 2 acceptance gate.

The later phases are intentionally not a list of features to build in
parallel. They establish replaceable boundaries in this order:

```text
typed tool result
  -> render document and artifact
    -> capability-aware delivery
      -> bounded agent run
        -> health/failover control
          -> additional platform principals
            -> scoped retrieval and memory
              -> event operations
                -> avatar/device intents
                  -> optional legacy replacement
```

Each arrow is an audit boundary. Data crossing it has a versioned contract,
captured authority, bounded size and lifetime, and a recorded outcome.

## Cross-phase rules

### One authority increase at a time

Design and contract work may overlap, but a release may increase only one of
these authorities at a time:

1. which implementation may execute;
2. which principal may request execution;
3. which side effect may occur;
4. which platform may receive or originate data;
5. which retained data may be retrieved;
6. which component may assume a failed component's role.

For example, adding a Telegram adapter and enabling administrator writes are
two releases, not one. Adding a model planner and allowing it to execute tools
are also two releases.

### Shared envelope and provenance

New contracts use a common envelope where applicable:

- `schema_version` and immutable object ID;
- producer instance, implementation version, and contract hash;
- canonical source event, conversation, and captured principal references;
- created, deadline, expiry, and received timestamps;
- data classification, access scope, and retention class;
- trace ID and parent object ID;
- bounded metadata after sanitization.

Provider claims are never accepted from display names, model text, artifact
paths, or unsigned runtime discovery. Exact authenticated instance identity
and reviewed authority remain separate.

### Logical migration slices

Only Phase 3 migration numbers `0011`–`0014` are reserved. Later phases keep
logical slices until the preceding phase exits, so planning does not force a
bad physical schema years early:

- renderer documents/jobs, artifacts, and delivery receipts;
- agent runs/model attempts/context snapshots;
- incidents, degradation policies, and failover leases;
- principal links, platform credentials, and adapter receipts;
- state/doc/history/memory indexes and consent records;
- event programs, staff roles, tickets, raffles, and operation logs;
- avatar sessions, intents, acknowledgements, and safety events.

Before implementation of each slice, it receives the next linear Alembic
revisions, explicit upgrade/downgrade behavior, retention impact, and a
production backup/restore test. An append-only audit table is not collapsed
into a mutable JSON column merely to reduce migration count.

The Phase 4 artifact slice extends or deliberately generalizes the Phase 3
`tool_artifacts` authority. It does not create a competing artifact identity,
duplicate the same bytes under unrelated ledgers, or bypass Phase 3
reservation/finalization. Any rename or generalization is an explicit data
migration with compatibility views and rollback.

### Delivery is not tool execution

Tools return structured results and artifact references. Renderers convert
those results into presentation. Platform adapters create delivery attempts
and receipts. These remain distinct even when one compatibility plugin wraps
all three steps temporarily.

No tool provider receives a platform bot token. No renderer may call a send
API. No platform adapter gains permission to invoke arbitrary tools merely
because it can deliver their results.

### Common rollout record

Every later-phase canary records:

- exact contract and implementation hashes;
- exact conversations, principals, providers, and platform adapters in scope;
- old and new path selection plus rollback switch;
- latency, error, timeout, resource, and queue baselines;
- privacy/retention query results;
- duplicate, replay, crash, stale lease, and outage results;
- operator-visible start/end timestamps and explained exceptions.

The stable window is chosen per risk. A pure contract-only release may need a
shorter observation period than delivery, model execution, failover, or a
state-changing event system.

## Phase 4: Unified Renderer and delivery boundary

### Phase objective

Phase 4 makes presentation and delivery independent of tools and callers. The
same validated tool result must be renderable against materially different
capability profiles without platform conditionals inside the tool provider.
Phase 4 proves this with QQ plus a deterministic adapter simulator; Phase 7
later repeats the proof on a real second platform.

### Non-goals

- The renderer does not choose or execute tools.
- The renderer does not authorize a principal or side effect.
- The renderer does not send messages.
- Phase 4 does not introduce general remote browsing or arbitrary local-file
  access.
- Pixel-identical output across platforms is not required; explainable,
  capability-driven semantic equivalence is.

### 4a. RenderDocument contract

Define a versioned `RenderDocument` with a deliberately small node set:

- `text`, `heading`, `paragraph`, `list`, and `quote`;
- `code` with language and wrapping policy;
- `math` with source format and display/inline intent;
- `table` with bounded rows, columns, and cell depth;
- `image` and generic `artifact_ref`;
- `card` with title, fields, status, and actions that are presentation-only;
- `progress`, `warning`, and `error_summary`;
- `group` and `alternative` for ordering and explicit fallback.

Every node has a stable ID and optional accessibility text. Nodes cannot embed
scripts, raw platform segments, arbitrary HTML, filesystem paths, bot tokens,
or executable callbacks. Platform mentions and replies are delivery metadata,
not inline renderer markup.

Canonicalization is deterministic. Limits cover document bytes, node count,
tree depth, table cells, code/math length, artifact count, and total declared
artifact bytes. Unsupported node versions fail closed before rendering.

### 4b. Artifact lifecycle

Artifacts are content-addressed immutable byte objects plus mutable retention
state. Metadata includes:

- SHA-256, MIME type, byte size, and dimensions/duration where applicable;
- producer tool/renderer version, source invocation, and render job;
- data classification and access scope;
- creation, expiry, legal/operational retention, and deletion status;
- verified storage backend key, never a provider-local path;
- optional accessibility text and safe filename hint.

Creation uses the Phase 3 reservation pattern: Core grants a one-use bounded
reservation, independently counts and hashes bytes, validates allowed MIME,
and finalizes metadata atomically. A provider URL is not an artifact. Remote
fetch, if later allowed, is a separate reviewed tool with SSRF, size, MIME,
redirect, DNS-rebinding, and timeout policy.

Deletion removes retrievable bytes while retaining the minimum hash,
provenance, and deletion record required for audit. Access URLs are scoped,
short-lived, and never stored as durable artifact identity.

### 4c. Render jobs and provider protocol

Reuse the Phase 3 lease/fencing protocol rather than inventing a renderer-only
queue. A render job captures:

- input document hash and renderer profile;
- requested output families such as text, raster image, audio, or web card;
- deadline and byte/pixel/font/resource budgets;
- locale, theme, accessibility, and deterministic-cache inputs;
- provider snapshot and exact renderer implementation hash;
- attempts, fence, structured failure, and finalized artifacts.

Only deterministic, non-sensitive renders are globally cacheable. Cache keys
include contract version, canonical document, renderer profile, theme/font
bundle versions, locale, and implementation hash. Conversation-sensitive
documents use scoped caches or no cache.

### 4d. Capability planning and delivery

Core creates a `DeliveryPlan` from a RenderDocument, available artifacts, and
the target adapter's typed capability snapshot. It records selected and
rejected alternatives and every degradation reason. Typical paths are:

```text
native text -> native text
markdown -> platform markdown | sanitized image | plain text
math -> native math | rendered image + alt text | source text
table -> native table | image + alt summary | bounded text rows
image -> native image | accessible link | alt text
```

A delivery intent contains target platform/conversation, reply/mention
semantics, ordered payload parts, expiry, idempotency key, and capability
snapshot hash. The adapter returns attempt and platform acknowledgement data.
Unknown delivery is not retried blindly when the platform may already have
accepted the message.

### Phase 4 work packets

1. **4a contract only:** RenderDocument schemas, canonicalization, limits,
   golden vectors, and validation; no renderer or send change.
2. **4b artifacts:** reservation/finalization, local production storage,
   retention/deletion, and one deterministic text/image renderer.
3. **4c planner:** capability negotiation, explicit degradation records, and
   delivery intent/receipt ledger in shadow.
4. **4d compatibility migration:** status, Wolfram, LaTeX, and help paths move
   one at a time from provider formatting/sending to result -> render ->
   delivery, each with rollback.
5. **4e capability-profile proof:** render and plan the same fixtures for QQ
   plus a deterministic constrained-adapter simulator. Phase 7 owns the later
   real second-platform canary.

### Phase 4 exit gate

- Tool providers call no platform send API on migrated paths.
- Render and delivery ledgers explain every selected fallback and receipt.
- Artifact size/MIME/hash/scope/expiry are independently enforced.
- Malicious Markdown/HTML/SVG/LaTeX, font bombs, image bombs, local paths,
  remote URLs, and oversized trees have tested bounded outcomes.
- Duplicate, timeout, crash, stale fence, unknown platform completion, and
  artifact deletion are tested on SQLite and PostgreSQL where applicable.
- Status, Wolfram, LaTeX, and help retain command compatibility and pass an
  exact-conversation canary.

## Phase 5: Natural-language planning and controlled tool loop

### Phase objective

Phase 5 lets a model propose and later execute eligible tools through Core
without weakening deterministic command paths. Model output is a request, not
authority.

### AgentRun contract and state

An `AgentRun` captures one bounded response attempt:

- source event, reply graph, conversation, and principal snapshot;
- policy/prompt/context recipe versions and hashes;
- eligible tool descriptor summaries exposed to the model;
- model provider, model/version, routing reason, and pricing snapshot;
- token, monetary, wall-time, turn, tool-call, and result-byte budgets;
- append-only model attempts, proposals, invocations, and render/delivery IDs;
- terminal outcome and safe error classification.

Suggested state machine:

```text
proposed -> context_ready -> model_running
  -> awaiting_tool_decision -> tool_running -> model_running
  -> awaiting_confirmation -> model_running
  -> response_ready -> rendering -> delivered
  -> rejected | failed | timed_out | budget_exhausted | cancelled
```

Terminal transitions are immutable. A model retry is a new attempt under the
same run budget, not erasure of the failed attempt.

### 5a. Planner-only shadow

The default context is intentionally small:

- system and safety policy;
- current normalized message and resolved reply graph;
- a bounded recent conversation window;
- current principal and platform capability summary;
- only currently eligible tool summaries, not full unrelated schemas.

The planner emits structured answer/tool proposals and uncertainty, but Core
executes nothing. Shadow scoring covers false calls, missed calls, wrong tool,
argument validity, forbidden-tool requests, unnecessary retrieval, and
disagreement with deterministic command routing. User-authored prompt content
cannot change the caller, principal, tool eligibility, or budgets.

### 5b. Bounded read/compute loop

Core validates each proposal against the exact Phase 3 descriptor and current
principal/policy/provider/capability state. It then creates a normal invocation
and returns only validated structured output or a bounded error. The model
never receives a provider lease, bot token, database credential, local path,
or raw stack trace.

The first execution allowlist is explicit and read/compute-only. Per-run
limits include maximum model attempts, tool calls, sequential depth, parallel
fan-out, total duration, tokens, cost, input/output bytes, and artifact bytes.
Repeated equivalent calls may be rejected as a loop even when the tool itself
is not deterministic.

Tool results are untrusted model input. They are delimited, size-limited,
classified, provenance-tagged, and prohibited from altering system policy.
Provider/model errors reduce functionality; they never break command tools.

### 5c. Confirmation-bound writes

Writes remain disabled until Core has a real principal and authorization
model. A confirmation binds:

- principal and authenticated confirmation channel;
- tool ID/version/hash and normalized arguments hash;
- exact scope and described effect;
- invocation/run ID, expiry, and single-use nonce;
- required role count and optional second approver.

Any argument, descriptor, principal, scope, or policy change invalidates the
confirmation. Ambiguous completion enters manual review and is never retried
from model insistence.

### Model routing and privacy

Model providers are registered like execution providers: reviewed profiles
define data locality, retention, supported structured-output protocol, context
limits, cost, health, and permitted data classifications. A fallback provider
may receive a run only when policy explicitly allows the same data class and
capabilities; “available” is not sufficient.

No full prompt or chain-of-thought is required for audit. Store bounded input
references, policy/context hashes, structured proposals, usage, safe summaries,
and provider request IDs according to retention policy. Sensitive prompt
capture is opt-in and redacted.

### Phase 5 work packets and exit gate

1. **5a:** run/context/proposal contracts and planner-only shadow.
2. **5b:** read-only `status.inspect`, then bounded `wolfram.run` and
   `latex.render`, one tool canary at a time.
3. **5c:** confirmation UX and simulated writes before any real write tool.

If an independently accepted query tool already exists, 5b also proves that it
is exposed only on demand. Otherwise retrieval integration is not a Phase 5
exit requirement and waits for Phase 8. Phase 5 never creates a retrieval
store as a shortcut, and history always waits for Phase 8 scope policy.

Exit requires adversarial prompt/tool-result injection, schema abuse, loops,
budget exhaustion, retries, provider failover, confirmation replay, and Core
outage tests. Command latency and availability remain independent of model
health, and no unconfirmed write is reachable.

## Phase 6: Watchdog, incidents, and role failover

### Phase objective

Phase 6 turns existing heartbeats into explicit incident handling and bounded
role failover. It does not create a third general chat participant.

### Health and incident model

Separate raw signals from derived state:

- signals: heartbeat, platform connection, provider snapshot, queue/latency,
  error rate, claim failure, platform risk/logout, and operator report;
- health evaluation: `healthy`, `degraded`, `unavailable`, `unknown` with
  policy version and evidence window;
- incidents: opened, acknowledged, mitigated, recovering, resolved, or
  suppressed, with append-only transitions and affected resources;
- notifications: channel, recipient principal, dedup key, attempt, receipt,
  escalation, and quiet-hours policy.

One missing heartbeat does not page immediately. Policies use consecutive
failures, rolling windows, cooldown, hysteresis, maintenance suppression, and
explicit recovery thresholds. Clock skew and delayed telemetry cannot create
permanent health.

### Degradation matrix

Each role/tool row declares:

- primary instance/provider;
- permitted fallback and required capabilities;
- maximum degraded permission, rate, concurrency, and data scope;
- forbidden failover cases;
- lease duration, fence domain, and recovery cooldown;
- user-visible degradation and administrator notification.

Talk may not inherit administrator commands merely because Command is down.
Watchdog may not inherit ordinary chat or memory access merely because both
are down. Platform account risk may require shutting down sends rather than
failing over and risking another account.

### Failover protocol

Failover is a Core-issued role lease tied to incident, policy, capability, and
instance health snapshots. Fencing prevents both recovered primary and
fallback from executing the same invocation or delivery. Recovery first stops
new fallback leases, waits for in-flight ownership to settle, observes a
cooldown, and only then restores primary authority.

If Core or PostgreSQL is unavailable, no new failover authority can be issued.
Existing bots follow their documented local fail-open behavior; Watchdog uses
an isolated administrator alert path with bounded local facts, not a shadow
copy of Core authority.

### Watchdog account isolation

The Watchdog account has separate credentials, NapCat/runtime, rate limits,
and administrator allowlist. It does not subscribe to ordinary chat by
default. Its ingress is restricted to health/incident events and explicit
administrator requests. Its output is limited to incident summaries and
read-only status until separately reviewed.

### Phase 6 work packets and exit gate

1. Incident/state/notification ledger in shadow from current heartbeats.
2. Administrator-only alert delivery with dedup, quiet hours, and receipts.
3. Simulated degradation matrix and lease/fence fault injection.
4. One low-risk read-only fallback canary.
5. Watchdog account deployment only after isolation review.

Exit requires tested loss and recovery of each bot, NapCat, provider, Core,
PostgreSQL, network path, and notification channel. No failback duplicates a
reply/invocation, maintenance does not page, and Watchdog never observes normal
conversation traffic.

## Phase 7: Additional platforms and Web Admin

### Thin adapter contract

Every adapter implements only:

- authenticated platform/account instance identity;
- normalized inbound events, references, attachments, and platform receipts;
- typed capability and limit snapshots;
- delivery intents, attempts, acknowledgements, recalls where authorized;
- principal evidence such as stable IDs and platform roles;
- health and platform-specific error classification.

It does not implement tool policy, model prompts, memory policy, cross-platform
identity inference, or provider logic.

### Principal mapping

Platform principals remain distinct until an explicit binding is created.
Binding records both stable IDs, method, verifier, actor, timestamps, expiry or
revocation, assurance level, and audit evidence. Display name, avatar, shared
group membership, or model belief is never binding evidence.

Administrator bindings require a stronger ceremony such as an existing
trusted channel plus a short-lived challenge on the new channel. Role evidence
has a freshness window. Revocation immediately removes new authority but
preserves historical audit identity.

### Platform sequence

1. **Telegram administrator private chat:** alerts and read-only status with an
   explicit Telegram principal binding; no group ingestion or writes.
2. **Web Admin read-only:** health, events, decisions, claims, invocations,
   render/delivery, incidents, and audit views with pagination/redaction.
3. **Web confirmation:** strong login, short sessions, CSRF protection,
   re-authentication for high-risk actions, and immutable action audit.
4. **Second-platform tool/render proof:** a reviewed read-only tool and common
   RenderDocument path.
5. Lower-priority WeChat/Discord/email/live adapters only after a concrete use
   case and privacy/retention review.

Web Admin is not a privileged shortcut into database models. It calls the same
policy-enforced application services as other callers. Secrets are written via
dedicated secret management paths and are never returned to the browser.

### Cross-platform operations

Forwarding, mirroring, announcing, or replying across platforms is an
`external_message` tool. It captures source consent, destination scope,
principal, redaction, attachment policy, confirmation, and delivery receipts.
Adapters never forward automatically because two conversations look similar.

### Phase 7 exit gate

The same event/tool/render/delivery contracts work on QQ and one second
platform; provider code contains no platform branch. Principal binding,
revocation, role freshness, CSRF/session failure, duplicate web requests,
delivery ambiguity, attachment limits, and adapter outage are tested. Web and
Telegram compromise do not grant unrestricted provider or Core admin access.

## Phase 8: Retrieval and Memory as Tool

### Retrieval classes

Implement in increasing privacy and ambiguity order:

1. `state.get`: explicit typed state with owner/scope/version.
2. `docs.search`: curated versioned documents with source citations.
3. `history.search`: authorized conversation history with bounded queries.
4. `memory.lookup`: curated durable facts with provenance and expiry.
5. embeddings/reranking only after exact retrieval misses are measured.

All results share a retrieval envelope: item ID/type, source and source time,
scope, data classification, confidence, retrieval method/version, redactions,
expiry, and a short excerpt or typed value. The model cannot treat retrieval
confidence as authorization.

### Scope and authorization

Conversation history is authorized by stable principal and conversation
membership/role evidence at query time, plus retention policy. Being able to
talk to Lily in one group does not grant access to another group, private chat,
deleted content, or administrator records. Cross-conversation search is a
separate privileged capability.

Queries are bounded by time range, result count, scanned bytes, excerpt size,
attachments, and execution time. Results preserve message/source IDs and
reply relationships where available. Search does not silently merge Lily,
Nekro, and Core history lacking a verified canonical key.

### Memory records and writes

A durable memory record includes subject scope, fact, provenance references,
creator, review state, confidence, expiry/review time, sensitivity, and consent
basis. Model-inferred personality, relationships, health, identity, or other
sensitive profiles are not automatically persisted.

Writes use separate tools such as `memory.propose`, `memory.approve`,
`memory.correct`, and `memory.delete`. Proposal is not publication. Correction
and deletion append audit events and remove retrieval eligibility as required.

Export, access review, retention expiration, source deletion propagation, and
consent withdrawal are first-version work, not cleanup after launch.

### Retrieval safety

Retrieved text is untrusted and may contain prompt injection, secrets, or
malicious markup. Core labels and bounds it before the model and renderer.
Documents record ingestion source, parser version, checksum, ACL, and update
time. Embedding indexes are rebuildable derivatives, never the authority or
only copy.

### Phase 8 exit gate

- `state.get` and `docs.search` prove typed scope and provenance first.
- `history.search` passes membership, private/group separation, deletion,
  export, pagination, abuse, and no-cross-source-fuzzy-merge tests.
- `memory.lookup` returns only approved, unexpired, authorized records.
- Default chat context remains free of automatic history/memory injection.
- Exact retrieval quality is measured before adding embeddings; vector outage
  cannot break exact state/docs paths.

## Phase 9: Event operations

### Event domain boundary

An event is a versioned operational scope, not a collection of chat commands.
Core models program items, venues, staff roles, tickets/attendees, check-in,
raffle pools/draws, timers, announcements, OBS scenes, and operation runs with
explicit ownership and lifecycle.

Event roles are separate from global bot administrators. A stage operator may
advance one program item without gaining ticket export, raffle editing, bot
configuration, or server restart authority.

### State-changing operation contract

Every operation records actor principal, event role, source UI/channel,
normalized arguments, confirmation, precondition/version, effect, external
receipt, and rollback or compensation plan. Optimistic version checks prevent
two staff clients from silently overwriting state.

External devices and OBS are providers behind leases, not directly controlled
from browser JavaScript. Unknown completion is surfaced for operator review.

### Tickets, check-in, and privacy

Ticket secrets and attendee identity use stricter classification, encryption,
access logging, export restrictions, and short retention. Check-in supports
idempotent scans and clear duplicate/already-used outcomes. Offline mode uses
pre-authorized scoped data, signed operation records, and a documented conflict
reconciliation policy rather than a full privileged database copy.

### Reproducible raffles

Raffle configuration freezes eligible input, exclusions, weighting rules,
draw count, algorithm/version, and an input-set hash. A commit/reveal or
operator-approved randomness record makes the draw reproducible without
publishing private ticket secrets. Redraws are new linked operations with a
reason; prior outcomes are never overwritten.

### Rehearsal and degraded operation

Simulation uses the real contracts and policy with fake destinations and
clearly isolated data. A rehearsal timeline can be replayed from audit records.
Runbooks cover loss of venue internet, Core, database, bot accounts, OBS,
ticket scanner, display, and alert path. Manual fallback and later
reconciliation are explicit.

### Phase 9 work packets and exit gate

1. Program/timer/state read model and read-only staff view.
2. Event-scoped role policy and simulated operation ledger.
3. Announcements/OBS through confirmed tools and receipts.
4. Reproducible raffle in simulation, then controlled live canary.
5. Ticket/check-in only after privacy and offline design review.

Exit requires a full rehearsal, replayable audit, deterministic raffle proof,
role-separation tests, duplicate/late/offline operation handling, and a tested
manual runbook. Every write has an actor, confirmation where required, and
rollback or compensation semantics.

## Phase 10: Fumo and avatar adapters

### Intent contract

Core emits versioned, expiring intents rather than device commands:

- `speak`: text/SSML reference, voice profile, interrupt policy;
- `subtitle`: text, style token, timing, and accessibility;
- `emotion`: named bounded state and intensity;
- `action`: allowlisted motion/action ID and duration;
- `display_card`: RenderDocument/artifact reference;
- `attention`: safe direction or target token, never arbitrary motor values.

Each intent carries avatar session, monotonically increasing sequence, issue
and expiry times, priority, deduplication key, required capabilities, and safe
fallback. Adapters acknowledge accepted, started, completed, expired, rejected,
or interrupted. Reconnect never replays expired motion or speech.

### Local safety authority

Physical devices retain a local safety controller that may reject Core:

- hardware emergency stop and mute;
- motion, volume, temperature, battery, and duty-cycle limits;
- bounded queues and watchdog timeout to a safe idle state;
- visible microphone/camera indicators and local privacy switch;
- no movement on stale, unordered, duplicated, or unauthenticated intents.

The safety controller cannot invoke tools or become a Core administrator. A
device credential is scoped to its avatar session and telemetry/intents only.

### Sensor and feedback boundaries

Microphone, camera, QR, NFC, and button input become normalized events with
explicit capture indicators, consent, retention, and rate limits. Raw audio or
video is not retained by default. Voice output must not recursively trigger
the same microphone pipeline; echo/loop prevention and operator override are
tested.

OBS/Live2D uses the same intent contract but a different capability profile.
It may render richer emotion/actions, yet still owns no planning or tool
authority.

### Phase 10 work packets and exit gate

1. Intent/session/ack contracts and a simulator with deterministic time.
2. OBS/Live2D adapter canary without sensors.
3. Fumo display/button path with physical mute/stop.
4. Audio, then camera/NFC only under separate privacy and safety reviews.

Exit requires disconnect, reorder, duplicate, expiry, queue overflow,
emergency stop, privacy switch, audio feedback, and malicious intent tests.
Operator override always wins, and device compromise cannot grant tool or
administrator authority.

## Phase 11: Optional legacy runtime replacement

### Replacement is a scorecard, not a milestone slogan

A component is a replacement candidate only when the stable contracts around
it make old and new implementations comparable. The scorecard includes:

- functional behavior and known compatibility exceptions;
- p50/p95/p99 latency, throughput, memory/CPU, and startup/recovery time;
- error classification, outage behavior, and observability completeness;
- security surface, dependency/update burden, and credential isolation;
- data ownership/migration/retention boundary;
- operator effort and rollback time;
- active features/users that still require the legacy implementation.

“Custom” is not automatically safer or simpler. A healthy legacy provider can
remain indefinitely when replacement has no demonstrated benefit.

### Candidate order

Prefer the narrowest replaceable boundary first:

1. adapter shim or response renderer already governed by Core contracts;
2. standalone provider/runner with Phase 3 leases;
3. model router/agent loop after Phase 5 run contracts stabilize;
4. platform adapter after Phase 7 proves a second implementation;
5. plugin host or full NoneBot/Nekro retirement last.

A monolithic simultaneous rewrite is forbidden.

### Shadow, migration, and rollback

Golden fixtures and sampled redacted production traffic run through old and
new components. Shadow never duplicates a write or platform send. Differences
are classified by semantics, presentation, latency, resource use, and error
boundary rather than raw text alone.

Data migration defines source of truth, overlap boundary, idempotency,
verification, rollback/read-only archive, and deletion timeline. No fuzzy merge
is introduced to make counts align. Cutover uses an exact scope and reversible
selector; old credentials remain isolated and available only for the approved
rollback window.

### Phase 11 exit gate

Each replaced component independently passes shadow, exact canary, stable
window, outage, data migration, credential revocation, and rollback rehearsal.
The system is considered complete even if some legacy runtimes remain as
healthy providers behind the shared contracts.

## Dependency and overlap matrix

The table distinguishes technical prerequisites from release authority.
Design may move ahead where shown, but production authority also waits for
every earlier numbered phase to pass unless `ROADMAP.md` is explicitly revised
with new acceptance and rollback reasoning.

| Work | Hard prerequisites | Design may start | Production authority waits for |
|---|---|---|---|
| Phase 4 RenderDocument/artifacts | Phase 3 descriptor/result/artifact contract | During late Phase 3 | Phase 3 exit for provider migration |
| Phase 5 planner shadow | Phase 3 eligible tool summaries; Phase 2 event/reply model | During Phase 3 canaries | Phase 4 result/render boundary for user delivery |
| Phase 6 incident shadow | Phase 2 heartbeats/status transitions | Now as design | Phase 5 exit plus Phase 3 leases for real failover |
| Phase 7 Telegram/Web read-only | Stable event/audit APIs and principal design | During Phase 4 | Phase 6 exit plus principal binding and adapter security review |
| Phase 8 state/docs retrieval | Phase 3 tool protocol and principal scopes | During Phase 5a | Phase 7 exit and cross-platform identity policy |
| Phase 9 event read model | Phase 3 tools, Phase 7 Web, event role design | During Phase 6/7 | Phase 8 exit plus confirmed writes and renderer/delivery receipts |
| Phase 10 intent simulator | Phase 4 RenderDocument/artifacts | During Phase 7/9 | Phase 9 exit plus device safety and session leases |
| Phase 11 component shadows | Stable boundary around that component | Per component | Phase 10 exit plus its replacement scorecard and rollback gate |

“Design may start” means schemas, fixtures, threat models, and simulations. It
does not permit production endpoints, credentials, migrations, or execution.

## Decisions intentionally deferred

The following choices should not be frozen before measurements exist:

- artifact backend beyond a local production-safe first implementation;
- a browser/Web Admin framework or identity provider;
- vector database/reranker for memory retrieval;
- Watchdog hosting/account topology;
- Fumo hardware, speech stack, and camera/NFC components;
- whether NoneBot or Nekro is ever fully replaced;
- Redis or another queue until Phase 3/6 load and lease behavior require it.

Each deferred decision receives an ADR when its owning phase begins, with
alternatives, evidence, security impact, migration cost, and reversal path.

## Definition of ready for a later phase

A later phase is ready to implement only when:

1. its hard prerequisites have production acceptance evidence;
2. contract boundaries and non-goals are reviewed;
3. logical persistence and retention are mapped to proposed migrations;
4. authority changes and captured principal inputs are explicit;
5. failure, ambiguity, replay, and rollback outcomes are enumerated;
6. initial exact canary scope and success metrics are chosen;
7. an implementation work packet can be completed without simultaneously
   enabling the next phase's authority.

This keeps the roadmap ambitious without turning it into a parallel rewrite.
