# Phase 3 design: Tool Registry and execution harness

## Purpose

Phase 3 turns selected existing Lily capabilities into typed, auditable tools
without granting a language model execution authority. It separates five
things that old command plugins commonly combine:

1. entry-point parsing;
2. principal and permission evaluation;
3. tool input validation and execution;
4. structured result/artifact creation;
5. rendering and platform delivery.

The command interface remains. A command becomes one trusted caller of the
same tool protocol that future natural-language planning will use.

## Non-goals

- No default RAG or long-term memory injection.
- No model-controlled tool loop; that is Phase 5.
- No broad migration of every matcher.
- No state-changing or administrator tool until sender authorization and
  confirmation exist.
- No tool implementation may send a QQ/Telegram/Web message directly.
- No plugin gains authority merely because runtime discovery found it.

## Core invariants

- Reviewed descriptors grant authority; runtime snapshots only prove current
  availability and implementation identity.
- The exact descriptor version and policy snapshot used for an invocation are
  immutable audit data.
- Input is validated before queueing; output is validated before success.
- Provider code runs outside the Core API process.
- Providers pull bounded leases; Core does not expose inbound bot/plugin
  execution ports.
- Every lease carries a fencing token. A late worker cannot complete a newer
  attempt.
- Reads/compute may retry only under an explicit retry policy. Equal input is
  not assumed to produce equal output; ambiguous writes never retry
  automatically.
- Structured results are separate from render documents and platform sends.

## Tool descriptor

A reviewed descriptor requires at least:

```json
{
  "tool_id": "wolfram.run",
  "version": "1.0.0",
  "title": "Run a Wolfram expression",
  "description": "Evaluate a bounded Wolfram Language expression",
  "provider_selector": {
    "instance_ids": ["lily-command"],
    "protocol": "superlily-provider-pull-v1"
  },
  "source_plugin": "plugins.wolfram",
  "schema_profile": "json-schema-2020-12-superlily-v1",
  "input_schema": {"type": "object"},
  "output_schema": {"type": "object"},
  "side_effect": "compute",
  "determinism": "may_vary",
  "retry_policy": "no_automatic_retry",
  "permission": "public",
  "confirmation": "never",
  "allowed_callers": ["command"],
  "natural_language": false,
  "timeout_ms": 15000,
  "concurrency_limit": 2,
  "rate_limit": {"requests": 5, "window_seconds": 60, "scope": "sender"},
  "resource_budget": {"cpu_ms": 10000, "memory_bytes": 536870912},
  "required_budget_enforcement": ["wall_time", "output_bytes"],
  "required_capabilities": [],
  "data_classification": "conversation",
  "result_retention_seconds": 2592000
}
```

Required enums should be narrow:

- side effect: `read`, `compute`, `write`, `admin`, `external_message`;
- determinism: `deterministic`, `may_vary`, `external_state`;
- retry policy: `retry_safe`, `no_automatic_retry`,
  `provider_idempotency_key_required`;
- permission: `public`, `trusted`, `group_admin`, `superuser`, `service`;
- confirmation: `never`, `on_write`, `always`, `two_person`;
- callers: `command`, `agent`, `admin_api`, `watchdog`, `schedule`.

Idempotency describes effects, not equal outputs. `wolfram.run` can evaluate
time, random, or external-state expressions, so equal input must not be
declared equal output and automatic retry remains disabled until its language
and provider boundary prove retry safety.

Schemas use JSON Schema 2020-12 with a restricted Superlily profile: no remote
`$ref`, no executable/custom formats, bounded schema depth/size, and only
locally bundled references. Authority material is normalized with RFC 8785
JSON Canonicalization Scheme after rejecting NaN/Infinity and duplicate keys;
its SHA-256 is the descriptor hash. Cosmetic fields are excluded only by an
explicit versioned allowlist. The safe default is to version and re-review.

Descriptor lifecycle is `draft -> reviewed -> active -> suspended -> retired`,
with `revoked` available from any nonterminal state. Only an active reviewed
descriptor can be eligible. Retirement stops new invocations while preserving
the immutable version needed to audit old ones; revocation additionally stops
unleased queued work.

## Registry authority and runtime snapshots

The registry has two independent inputs:

1. reviewed descriptors committed/configured by an administrator;
2. authenticated provider snapshots reporting loaded tool IDs, versions,
   implementation hashes, health, concurrency, and supported protocol version.

A tool is eligible only when descriptor and runtime identity match, the
snapshot is fresh, the provider is online, and the caller/principal/capability
gates pass. Missing, extra, stale, incompatible, or hash-mismatched runtime
tools are visible but ineligible.

Each runtime entry reports `tool_id`, descriptor version/hash, provider
protocol version, implementation hash, health, current/max concurrency, and
which budgets it enforces as `hard`, `best_effort`, or `unsupported`. A
descriptor that requires hard enforcement is ineligible on a provider that
only reports or lacks it. Runtime snapshots never add callers, permissions,
network access, filesystem access, side effects, or retry authority.

Recommended APIs:

- `POST /v1/tool-registry/snapshots` — provider-authenticated inventory;
- `GET /v1/tools` — admin view of reviewed/runtime/effective state;
- `GET /v1/tools/{tool_id}` — descriptor versions and eligibility diagnostics.

Eligibility is an explicit result, not a boolean hidden in UI code. Core
returns `eligible` or one or more stable reasons such as `not_reviewed`,
`inactive_descriptor`, `provider_missing`, `provider_stale`,
`implementation_mismatch`, `protocol_incompatible`, `budget_unenforceable`,
`caller_forbidden`, `principal_unauthorized`, or `capability_unavailable`.

## Principal and policy input

An invocation principal is a captured value, not a later lookup by display
name:

- platform and stable sender ID;
- canonical conversation ID/type;
- authenticated bot/caller instance;
- platform roles observed on the source event;
- Superlily trust/group policy roles, when implemented;
- source event, decision, claim, and command/tool entry IDs.

Phase 3 begins with public read/compute tools. Platform roles alone are not
sufficient for Core-enforced administrator writes until role freshness and
cross-platform identity policy are designed.

## Invocation state machine

```text
proposed
  -> rejected
  -> awaiting_confirmation -> rejected/expired
  -> queued -> leased -> running -> succeeded
                         |          -> failed
                         |          -> timed_out
                         -> lease_expired -> queued (safe retry only)
  -> cancelled
```

Terminal states are immutable. State transitions append records; they do not
erase prior attempts.

An invocation row captures:

- invocation and idempotency IDs;
- tool ID/version/descriptor hash;
- normalized input and validated structured output;
- principal, source event, conversation, caller, policy and capability
  snapshots;
- side-effect/determinism/retry/confirmation decisions;
- deadlines, rate and resource budgets;
- current state and terminal classification;
- timestamps and redacted error summary.

Attempt rows capture provider, lease/fencing token, implementation hash,
start/finish/heartbeat time, resource usage, and provider result identity.

## Provider lease protocol

Recommended APIs:

- `POST /v1/tool-invocations` — authenticated caller creates or reuses one
  invocation after descriptor/input/principal/idempotency gates;
- `GET /v1/tool-invocations/{invocation_id}` — caller-scoped or admin status;
- `POST /v1/tool-executions/lease` — provider requests one eligible bounded
  job for its authenticated instance and runtime snapshot;
- `POST /v1/tool-executions/{invocation_id}/start` — validates lease/fence;
- `POST /v1/tool-executions/{invocation_id}/heartbeat` — optional for long
  operations, bounded by invocation deadline;
- `POST /v1/tool-executions/{invocation_id}/complete` — validates fence and
  output schema;
- `POST /v1/tool-executions/{invocation_id}/fail` — structured failure;
- `POST /v1/tool-invocations/{invocation_id}/cancel` — caller/admin cancel;
- `POST /v1/tool-invocations/{invocation_id}/confirm` — confirmation bound to
  normalized input and expiry.

Leasing avoids exposing inbound execution endpoints on the existing bots. A
provider outage leaves work queued/expired according to policy; it does not run
inside Core as fallback.

The lease response contains invocation ID, attempt ID, monotonically
increasing fencing token, descriptor/input hashes, normalized input, absolute
deadline, allowed result/artifact sizes, and the exact resource budget. It
never contains an admin token or another provider's credentials. Start,
heartbeat, complete, and fail require the provider identity, attempt ID,
lease secret, and current fence; replay or a late fence is rejected even if
the invocation is not yet terminal.

## Persistence model

Suggested tables:

- `tool_registry_snapshots` and immutable snapshot entries;
- `tool_descriptors` with version, authority hash, review status, and source;
- `tool_invocations` with current state and immutable request snapshots;
- `tool_invocation_transitions` append-only state history;
- `tool_attempts` with lease/fence/provider/resource data;
- `tool_confirmations` with principal, normalized request hash, expiry, and
  consumed time;
- `tool_artifacts` with content hash, MIME, size, scope, retention, and storage
  reference.

Database constraints enforce one active lease, legal terminal uniqueness, and
idempotency. PostgreSQL advisory locks may serialize transition decisions, but
full IDs/hashes and unique constraints remain the correctness authority.

Migration allocation is frozen before implementation:

- `0011_tool_registry`: immutable descriptor versions, review/lifecycle
  records, provider snapshots, and snapshot entries (Phase 3a only);
- `0012_tool_invocations`: invocations and append-only transitions;
- `0013_tool_attempts`: leases, fencing tokens, attempt heartbeats and usage;
- `0014_tool_confirmations_artifacts`: confirmation ledger plus bounded
  artifact reservations and metadata.

Later migrations may split these tables but may not collapse immutable audit
history into mutable JSON blobs. Phase 3a must not create execution rows or
endpoints ahead of 3b.

## Error and result model

Provider failures are structured and bounded:

- `invalid_input`, `permission_denied`, `confirmation_required`;
- `rate_limited`, `budget_exceeded`, `provider_unavailable`;
- `timeout`, `cancelled`, `execution_failed`, `invalid_output`;
- `artifact_failed`, `internal_error`.

User-visible messages are rendered later from error class and safe details.
Provider stack traces, credentials, local paths, and raw upstream bodies are
not returned as tool output.

Successful output is JSON matching the descriptor plus optional artifact
references. Artifacts include content hash, MIME, byte size, dimensions when
applicable, producer/tool/version, scope, and expiry. A path or URL is not an
artifact contract.

For the Phase 3 `latex.render` slice, Core issues a short-lived, one-use
artifact reservation containing allowed MIME types and maximum bytes. The
provider streams bytes to the reservation endpoint; Core independently counts
and hashes them, then atomically finalizes metadata before an invocation can
succeed. Provider-local paths and arbitrary remote URLs are never accepted as
completed artifacts. Phase 4 may replace storage/delivery without changing
this reference contract.

## Idempotency, retries, and concurrency

- Command calls derive an idempotency key from canonical source event, tool,
  descriptor version, and normalized arguments.
- Duplicate create returns the existing invocation.
- Read/compute retries require descriptor permission and a new attempt with the
  same invocation/fence rules.
- Writes require a provider idempotency key and explicit recovery policy;
  unknown completion is terminal/manual-review, not blind retry.
- Concurrency is enforced at tool, provider, conversation, and optionally
  sender scopes before lease.

## Security and privacy

- Descriptor schemas and result sanitization have size/depth/item limits.
- Tool inputs are data-classified; admin views redact by policy.
- Tool-result text is untrusted input to later models and renderers.
- Remote fetch, local file, subprocess, network, and sandbox permissions are
  descriptor/provider policy, not arguments a caller can escalate.
- The initial Wolfram provider denies process launch, unrestricted filesystem
  access, arbitrary network access, package installation, and front-end
  evaluation unless a later descriptor explicitly grants a separately
  sandboxed profile. A timeout without process/resource isolation is not a
  hard budget.
- Confirmation tokens are single-use, short-lived, principal-bound, and hash
  the exact normalized request.
- Audit retention is independent from artifact retention; deletion leaves the
  minimum integrity/provenance record required by policy.

## Migration sequence

### First: `status.inspect`

Proves descriptor registration, provider freshness, lease, structured output,
audit, timeout, and command compatibility without expensive compute or
artifacts.

The first implementation may wrap Lily's existing status plugin, but it still
uses the provider pull protocol. It does not execute inside the Core FastAPI
process and does not receive the Core admin token.

### Second: `wolfram.run`

Wrap the existing persistent worker behind typed inputs and structured outputs.
Keep raw expression size, evaluation time, memory, output size, image count,
and concurrency bounded. Fatal worker recovery remains provider-side and is
reported as attempt health.

### Third: `latex.render`

Proves content-addressed artifacts, MIME/hash/size validation, and later
renderer/delivery separation. It remains read-only but must treat TeX input as
untrusted.

### Later read tools

`history.search`, `docs.search`, and `state.get` wait for explicit scope,
privacy, retention, and provenance contracts. They are not default context.

### Writes last

Moderation, configuration, announcements, restarts, cross-platform messages,
event control, and memory writes wait for Core sender authorization,
confirmation, compensation/rollback semantics, and stronger canaries.

## Command compatibility

During migration, an existing command matcher may remain the parser and
principal source, but it must submit a tool invocation and consume structured
result/error data. It does not call the old implementation and tool provider in
parallel for a state-changing operation.

Read-only shadow comparison records normalized old/new result summaries with
redaction and strict size limits. A compatibility route is removed only after
the tool path passes equivalence, latency, error, resource, and operator
rollback gates.

## Phase 3 acceptance

- Descriptor hashing, schema validation, and runtime matching are deterministic.
- Unknown/stale/mismatched providers never become eligible.
- Principal, permission, capability, confirmation, rate, concurrency, budget,
  and provider-health gates are individually tested and auditable.
- Invocation transitions reject illegal/replayed/late fencing operations.
- SQLite and PostgreSQL test the schema and concurrency invariants; fresh
  migration upgrade/downgrade and production drift checks pass.
- Provider/Core restart, timeout, cancellation, lease expiry, malformed output,
  and ambiguous write completion have explicit tested outcomes.
- At least `status.inspect`, `wolfram.run`, and `latex.render` use the common
  protocol while old command entry points remain available.
- One exact conversation canaries each migrated tool, followed by a stable
  evidence window and security/data-retention audit.
- Natural-language callers remain disabled at Phase 3 completion.

## First implementation work packet

After the start checklist is green, implementation begins in this exact order:

1. Write contract-only Pydantic models for descriptor authority material,
   restricted schemas, provider snapshot entries, canonicalization and stable
   eligibility reasons; add golden hash vectors shared by Core and providers.
2. Add migration `0011_tool_registry` and dual-database upgrade/downgrade
   tests. No invocation or execution endpoint exists yet.
3. Add provider-authenticated snapshot ingestion and admin-only effective
   registry views. Unknown, stale and mismatched examples must all be visible
   and ineligible.
4. Deploy 3a with zero active descriptors and prove that runtime discovery
   alone cannot make any tool callable.
5. Review and activate only the `status.inspect` descriptor; still keep
   execution disabled while registry evidence is collected.
6. Begin 3b with migrations `0012` and `0013`, transition tests, lease/fence
   fault injection, then a standalone status provider. Do not start Wolfram or
   LaTeX migration until the status slice passes its exact-conversation
   canary and rollback gate.

The first code change of Phase 3 is therefore a contract/hash test, not a
provider wrapper and not an agent tool call.

## Start checklist

Phase 3 implementation may begin only when Phase 2 acceptance records:

- clean final canary and 24-hour decision/claim/response evidence;
- exact canary confinement and zero duplicate enforced owners;
- fresh runtime registry and both instances online;
- typed capability snapshots from both bridges;
- clean secret/URL/raw retention audit;
- SQLite/PostgreSQL suites, migration head, production drift, backup, and
  rollback evidence;
- committed Phase 2 code and documentation.

## Handoff to later phases

Phase 3 must leave stable seams for `FUTURE_PHASES_DESIGN.md` without
implementing them early:

- validated structured output remains distinct from RenderDocument and
  platform delivery;
- artifact identity/reservation can be generalized by Phase 4 without a
  second competing artifact ledger;
- invocation, principal, policy, capability, provider, budget, and outcome
  snapshots are immutable inputs to future AgentRun audit;
- provider leases/fencing are reusable by renderer, failover, and operations
  workers rather than copied into phase-specific queues;
- descriptors do not embed QQ segments, model prompts, renderer markup,
  Watchdog policy, memory retrieval, or device commands.

The detailed Phase 4–11 work packets and release dependencies are in
`docs/FUTURE_PHASES_DESIGN.md`. Their design may be reviewed during Phase 3,
but their migrations, endpoints, credentials, and production authority remain
disabled until the numbered gates in `ROADMAP.md` pass.
