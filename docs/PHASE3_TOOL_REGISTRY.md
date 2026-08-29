# 第三阶段设计：Tool Registry 与执行 harness

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

**当前状态：** Phase 3a、`0014_tool_invocations`、`0015_tool_attempts` 与最小控制面
M0–M3 已部署。M3 关闭了环境 scope 旁路，首包不开放 `enforce`。
`status.inspect@1.0.2` 已通过审阅者控制面激活为 `active/rv4`，
Provider 为 `active/rv3`。五份 Git-reviewed 单次计划已完成四个独立 stop
与一次无平台发送 canary，随后均暂停且消费 1/1。生产已恢复
`ledger_only`、无 active plan/lease；只有成功 canary 产生 1 个 attempt。
自然语言 caller、`enforce`、写工具和平台发送能力仍未开放。
`PHASE3_ACCEPTANCE.md` 是可执行发布清单，`docs/adr/` 中的 accepted ADR 冻结实现
决策，本文定义总体架构。

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
    "provider_ids": ["provider-wolfram-primary"],
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
  "execution_permissions": {
    "network": "deny",
    "filesystem": "sandbox_only",
    "subprocess": "deny",
    "secrets": [],
    "remote_fetch": "deny",
    "artifacts": ["image/png"]
  },
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

The restricted profile is a named, versioned contract, not “whatever the
current validator accepts”. It defines maximum source bytes, canonical bytes,
object depth, properties, array items, enum members, string/number bounds,
local-reference count and expansion depth; rejects unknown keywords, cycles,
ambiguous unions, unbounded containers, duplicate JSON keys, non-Unicode JSON,
NaN/Infinity, remote/dynamic references, and implementation-dependent custom
formats; and resolves `$ref` only inside the reviewed bundle. Parsing happens
from bytes with duplicate-key detection before model construction. Shared
golden vectors cover accepted/rejected schemas and exact JCS bytes/hash across
Core, CLI, and provider SDKs. Descriptor/input/output strings preserve exact
semantic whitespace; they must not inherit the ingestion wire model's global
`str_strip_whitespace` behavior. Identifier fields normalize only where the
descriptor contract explicitly says so.

Descriptor lifecycle is `draft -> reviewed -> active -> suspended -> retired`,
with `revoked` available from any nonterminal state. Only an active reviewed
descriptor can be eligible. Retirement stops new invocations while preserving
the immutable version needed to audit old ones; revocation additionally stops
unleased queued work.

## Registry authority, provider identity, inventory, and health

The registry has two independent inputs:

1. a Git-tracked descriptor bundle reviewed through the normal code-review
   path, with its commit and bundle hash recorded;
2. authenticated provider snapshots reporting loaded tool IDs, versions,
   implementation hashes, health, concurrency, and supported protocol version.

Git is the Phase 3 authority source. On import, Core verifies the reviewed
bundle and stores an immutable canonical descriptor copy, JCS bytes/hash,
source commit, reviewer/lifecycle record, and import outcome. The database is
the durable audit/read model, not an alternate editor. The control panel is
read-only for descriptor content in Phase 3; activation/suspension references
an exact imported version and cannot rewrite it.

Provider registration is separate from both inputs. A stable provider record
has an opaque provider ID, authenticated credential identity, protocol/version
allowlist, descriptor/provider selectors, owner, lifecycle, and credential
rotation/audit metadata. Provider tokens are unrelated to Lily/Nekro ingest or
Core administrator tokens. Display names, bot instance IDs, runtime claims,
implementation strings, and network source addresses never authenticate a
provider.

Dynamic provider state is split again:

- **inventory snapshots** are immutable, hash-verified reports of tool
  versions, descriptor/implementation hashes, protocols and hard/best-effort
  budget support;
- **heartbeats** are lightweight current health, load, capacity and clock-skew
  observations tied to one accepted inventory snapshot.

A fresh heartbeat cannot refresh stale inventory, and a fresh inventory does
not prove the provider is healthy. Stable registration, latest inventory, and
latest heartbeat are displayed separately in effective-state diagnostics.

The shared Provider SDK is deliberately smaller than a general agent runtime.
In Phase 3a it can load the same reviewed descriptor bytes with the same
validator/JCS implementation as Core and the CLI, construct a deterministic
inventory, authenticate inventory/heartbeat reports, and retry only the same
bounded report. It has no invocation create, lease, execution, artifact,
command parsing, model, rendering, or platform-send method. Provider-specific
code supplies an implementation hash and reports budget support honestly;
loading an SDK object never activates a descriptor.

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

Recommended APIs (the contract phase chooses one canonical inventory route;
the aliases below must not ship as competing ledgers):

- `POST /v1/provider-inventory/snapshots` — provider-authenticated immutable
  inventory (replaces the provisional `tool-registry/snapshots` name);
- `POST /v1/providers/heartbeats` — provider-authenticated dynamic health tied
  to one accepted inventory hash;
- `GET /v1/tools` — admin view of reviewed/runtime/effective state;
- `GET /v1/tools/{tool_id}` — descriptor versions and eligibility diagnostics.

Eligibility is an explicit result, not a boolean hidden in UI code. Core
returns `eligible` or one or more stable reasons such as `not_reviewed`,
`inactive_descriptor`, `provider_missing`, `provider_stale`,
`implementation_mismatch`, `protocol_incompatible`, `budget_unenforceable`,
`caller_forbidden`, `principal_unauthorized`, or `capability_unavailable`.

## Execution modes and independent stops

Registry eligibility never implies execution. Effective execution is the
intersection of descriptor lifecycle, provider eligibility, caller/principal
policy, and an explicit rollout mode:

- `off`: invocation creation is rejected and no lease can exist;
- `ledger_only`: a validated proposal/decision is recorded, but no executable
  queue row or lease is produced;
- `canary`: execution requires an active, Git-reviewed database rollout plan
  with an exact tuple of tool ID, descriptor version/hash, canonical
  conversation, caller, provider, expected resource versions, time window,
  and invocation limit.

`enforce` is deliberately closed in the first M3 package. Environment scope
variables are not authority; `SUPERLILY_TOOL_EXECUTION_MODE` is only a ceiling.

Four stops are independent and monotonic toward less authority: a global
execution stop, per-tool/version suspension, provider quarantine, and exact
rollout-plan pause. Any one
prevents new leases immediately. Revocation may additionally cancel safe
queued work; running/ambiguous side effects follow their recorded recovery
policy rather than being falsely marked cancelled. The effective mode and all
stop inputs are immutable invocation snapshots and visible in the panel.

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
  -> recorded_only
  -> queued -> leased -> running -> succeeded/failed/timed_out
                 |          |       -> cancel_requested -> cancelled
                 |          |       -> unknown_completion
                 |          -> lease_expired -> queued (safe retry only)
                 |                           -> unknown_completion
                 -> cancelled
```

Terminal states are immutable. State transitions append records; they do not
erase prior attempts.

The contract includes an explicit transition table before any endpoint is
implemented:

| Event | Permitted source | Result | Required evidence |
|---|---|---|---|
| validation/policy decision | `proposed` | `rejected`, `recorded_only`, `awaiting_confirmation`, or `queued` | descriptor, principal, mode and policy snapshots |
| confirmation | `awaiting_confirmation` | `queued`, `rejected`, or `expired` | exact input hash, principal, expiry, single-use record |
| lease | `queued` | `leased` | DB-issued time/deadline, provider eligibility, attempt and new fence |
| start/heartbeat | `leased`/`running` | `running` | provider identity, attempt secret, current fence, DB receipt time |
| complete | `running` | `succeeded`, `failed`, or `unknown_completion` | current fence, valid output/artifacts, within deadline |
| cancel | pre-lease | `cancelled` | caller/admin authority and reason |
| cancel | `leased`/`running` | `cancel_requested`, then `cancelled` or `unknown_completion` | provider acknowledgement or recovery classification |
| lease/deadline reaper | `leased`/`running` | requeue with a new attempt only when retry-safe; otherwise `timed_out`/`unknown_completion` | current fence, side-effect/retry policy, DB time |

Late/replayed start, heartbeat, complete, and fail operations never mutate the
current invocation; they append a bounded rejected-attempt audit record. An
output-schema or artifact-finalization error is `failed / invalid_output`, not
success. Provider clock values are diagnostic only: Core database time is the
authority for lease, confirmation, deadline, heartbeat freshness, and reaper
decisions. Reaping uses `FOR UPDATE SKIP LOCKED`/equivalent bounded batches and
fair ordering so one busy tool/provider/conversation cannot starve another.
Queue age and oldest-unleased age are explicit metrics and acceptance includes
clock-skew, reaper-crash, and starvation tests.

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

- stable `tool_providers`/credential records, immutable inventory snapshots and
  entries, and separate provider heartbeat observations;
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

Migration allocation was shifted only for the inserted C0-D reliability
foundation; the remaining Phase 3 allocation is frozen before implementation:

- `0011_claim_ack` belongs to and completes Phase 2;
- `0012_tool_registry`: immutable descriptor versions, review/lifecycle
  records, stable providers, inventory snapshots/entries, and heartbeats
  (Phase 3a only);
- `0013_collection_reliability`: authority-neutral C0-D capture evidence,
  actions, receipts and watermarks; no tool execution state;
- `0014_tool_invocations`: invocations and append-only transitions;
- `0015_tool_attempts`: leases, fencing tokens, attempt heartbeats and usage;
- `0016_confirm_artifacts`: confirmation ledger plus bounded
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

Artifact state is explicit: `reserved -> uploading -> finalized`, or
`expired/rejected`. The reservation is bound to invocation, attempt, provider,
fence, allowed MIME set, maximum bytes/count/dimensions, classification, scope,
expiry, and a one-use upload secret. Upload streams into quarantine while Core
enforces byte/time limits and calculates the digest; finalize verifies current
fence, declared and inspected MIME, digest, size and optional dimensions before
moving the object to content-addressed storage. Only finalized artifacts may
appear in successful output. Reaper cleanup is idempotent and never deletes a
referenced finalized object. A failed or late upload cannot mutate invocation
success.

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
- Permissions are machine-readable enums/allowlists for network destinations,
  DNS/IP class, remote fetch, filesystem roots/modes, subprocess profiles,
  sandbox profile, secret names, artifact MIME/count/bytes, and upstream data
  classification. “Provider supports it” is not permission. Unknown values
  make the tool ineligible.
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

The reviewed `1.0.0` bootstrap scope is only `provider_runtime`: it returns a
small structured self-liveness result from a standalone process. It does not
wrap Lily's current `nonebot_plugin_picstatus` command because that command
combines host/bot collection, network tests, background-image fetching,
rendering, and platform delivery, which conflicts with this descriptor's
no-network/no-filesystem structured boundary. The old command remains
unchanged. A later version may add separately permissioned service scopes.

During Phase 3a the standalone process invokes the operation only as a local
self-test and reports inventory/heartbeat. It has no lease consumer and Core
has no invocation route. Until the Phase 3b executor adds a hard wall-time
supervisor, the runtime reports wall-time enforcement as `unsupported`, so the
effective view must show `budget_unenforceable` in addition to
`inactive_descriptor` and `execution_off`. This is evidence of honest reduced
authority, not a failure to be hidden.

### Second: `wolfram.run`

Wrap the existing persistent worker behind typed inputs and structured outputs.
Keep raw expression size, evaluation time, memory, output size, image count,
and concurrency bounded. Fatal worker recovery remains provider-side and is
reported as attempt health. The first canary is structured text-only; any image
output remains ineligible until artifact reservation/upload/finalize has passed
its own acceptance tests.

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

The complete checkbox and evidence format is `PHASE3_ACCEPTANCE.md`; the list
below is only the exit summary.

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

1. Approve ADRs for descriptor authority/JCS, provider identity/inventory,
   invocation transitions/recovery, artifact storage, and control-plane auth.
2. Write contract-only models for the restricted descriptor/schema profile,
   provider registration, inventory, heartbeat, canonicalization and stable
   eligibility reasons; add shared golden hash/rejection vectors.
3. Add migration `0012_tool_registry` and dual-database upgrade/downgrade
   tests. No invocation or execution endpoint exists yet.
4. Import the Git descriptor bundle, authenticate provider inventory/heartbeat,
   and expose admin-only desired/runtime/effective views. Deploy with zero
   active descriptors, execution `off`, and prove discovery grants no authority.
5. Import/review only the real `status.inspect` authority and run its
   reporting-only Provider SDK process; keep execution `off` while collecting
   exact descriptor/provider/effective-state evidence. The expected Phase 3a
   reason includes `budget_unenforceable` until the later hard wall-time
   executor exists. A read-only control-panel view may begin here, but no
   mutation UI.
6. Add migration `0014_tool_invocations`, transition/DB-time/reaper tests, and
   `ledger_only` proposals. No provider lease exists yet.
7. Add migration `0015_tool_attempts`, lease/fence fault injection, all three
   stop controls, then upgrade the reporting-only status process with the
   standalone hard-budget lease executor. Canary the exact
   tool/version/conversation/caller/provider tuple and prove rollback.
8. Add migration `0016_confirm_artifacts` and pass reservation,
   upload, finalize, expiry, quota, MIME/hash, reaper, and late-fence tests.
9. Migrate text-only `wolfram.run`; enable its image output only through the
   artifact path. Migrate `latex.render` after that boundary passes. Old command
   implementations remain rollback until each stable evidence window closes.

The first code change of Phase 3 is therefore a contract/hash test, not a
provider wrapper and not an agent tool call.

## Start checklist

Phase 3 implementation may begin only when Phase 2 acceptance records:

- clean final canary and 24-hour decision/claim/response evidence;
- exact canary confinement, acknowledged peer suppressions, and zero duplicate
  enforced owners/responses;
- fresh runtime registry and both instances online;
- typed capability snapshots from both bridges;
- clean secret/custom-URI/raw retention audit;
- SQLite/PostgreSQL suites, migration head, production drift, backup, and
  rollback evidence;
- committed Phase 2 code and documentation.

## Frozen foundation seams

Phase 3 留下的下列 seam 已成为 stable foundation；本节记录兼容边界，不再生成后续
Phase 任务：

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

任何新 Renderer、Agent、failover、memory 或设备工作都必须按当前
[`ROADMAP.md`](ROADMAP.md) 由真实需求重新立项，不能从本合同推导 production
authority。
