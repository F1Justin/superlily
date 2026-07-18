# Phase 3 acceptance checklist

## Status and entrance gate

Phase 3a completed after its recorded Phase 2 entrance prerequisite. Its
Registry schema/read surface, real reviewed `status.inspect` authority, shared
Provider SDK, stable Provider identity, and reporting-only runtime are deployed.
The accepted ADRs, tests, and production record are evidence for these narrow
checked items only; invocation, lease, artifact, tool execution, and
natural-language authority remain unchecked until their own evidence exists.

- [x] `ACCEPTANCE.md` contains the signed policy-v6 Phase 2 controlled samples,
  reused policy-v5 24-hour audit plus policy-v6 counterfactual replay,
  acknowledged claim coordination, response attribution, SQLite/PostgreSQL
  tests, `0011_claim_ack` migration/drift, backup and rollback.
- [x] Phase 3 ADRs approve descriptor/JCS authority, provider identity,
  transitions/recovery, artifact storage, credentials and control-plane auth.

## 3a: authority and effective registry

- [x] Git-reviewed descriptor bundles are the only authority source; import
  stores immutable canonical bytes/hash, commit, lifecycle and reviewer audit.
- [x] The restricted JSON Schema profile rejects duplicate keys, non-finite
  numbers, remote/dynamic refs, cycles, unknown/unsafe keywords and all size,
  depth, item and expansion-limit violations.
- [x] Shared JCS golden vectors produce identical canonical bytes/hash in Core,
  CLI and provider SDK; semantic Wolfram/LaTeX whitespace is preserved.
- [x] Migration `0012_tool_registry` passes SQLite/PostgreSQL fresh upgrade,
  downgrade/re-upgrade, concurrency and production drift tests.
- [x] Provider identity/credential is separate from bot ingest/admin identity;
  inventory snapshots are immutable/hash-verified and heartbeat freshness is
  separate.
- [x] Desired, reported and effective state plus stable ineligibility reasons
  are independently testable. Unknown/stale/mismatched inventory or heartbeat
  never grants authority.
- [x] First deployment has zero active descriptors and execution `off`; runtime
  discovery alone cannot create an invocation or lease.

### Zero-authority production deployment evidence

On 2026-07-18 at 14:21 CST, commit `164c81b` was deployed as Core image
`sha256:ae1686707cec1c2b6f1ebe11be16698218ebca1b59a7c892e4e64c3b8efb298d`
with Compose config hash
`a81571e3303bcb033a53a3ef9b3cb4766f41f0c50c7e3a283c33691fc159e5ff`.
Before migration, PostgreSQL 17.10 was backed up to
`/home/justin/backups/superlily/superlily-pre-phase3a-20260718-141630.dump`
(141,659,973 bytes, SHA-256
`5e2d87098245cd4b3ae9bb4087d2034a3730a72b77ede2056fcbf459eccff199`)
and restored successfully into an isolated PostgreSQL 17 container at
`0011_claim_ack`; the restored key-table counts were internally consistent.

The production startup log records `0011_claim_ack -> 0012_tool_registry`.
`alembic current` reports `0012_tool_registry`, `alembic check` reports no
drift, and all eight Registry tables contain zero rows. The running environment
has zero Provider tokens; the admin view reports zero descriptors, active
descriptors, eligible tools, providers and healthy inventories with execution
`off`. Both Provider write surfaces reject unauthenticated probes, no
invocation/attempt/lease table or route exists, and `status.inspect` remains
404 because the golden vector was not imported. PostgreSQL, Lily, Nekro and
NapCat were not restarted; both bot instances remained online, their runtime
command snapshot remained fresh, and legacy event/claim/heartbeat ingestion
continued successfully immediately after the Core-only replacement.

### Real authority and reporting-only Provider evidence

On 2026-07-18 at 22:43 CST, commit
`c48aaa18e35d99ab6468a683329311586c7f1518` deployed the first real authority:
`status.inspect@1.0.0`, descriptor SHA-256
`65af3c28c09b250b3418269416841fa980fae9cfb8ffcb87c6df5305f6fbd62c`.
The Core image is
`sha256:010209464fb4105c33bf430b07ee5a56ff19884a3b6f97cccb17ab83b985aed5`
and the reporting-only Provider image is
`sha256:e1650313c1708b07442867aefef905a6cfa7123154d852bec6f6e5f539636d3a`.
Before mutation, PostgreSQL was backed up in custom format to
`/home/justin/backups/superlily/20260718-phase3a-status/superlily-pre-phase3a-status-c48aaa1.dump`
(147,741,882 bytes, mode `0600`, SHA-256
`763f2e33906040a3da3962406d62be6d7b7d448af8c7d09166a2f9e0909741b1`);
`pg_restore --list` read the archive successfully.

The descriptor was loaded from that exact Git object, imported as `reviewed`
and never activated. Provider `provider-status-primary` has an unrelated
environment credential and an active stable registration. The container has a
read-only root, drops all capabilities, publishes no port, receives no admin or
bot token, and has no invocation/lease client. The installed implementation
self-test and its output schema passed in that container. SQLite and PostgreSQL
each passed all 254 tests; the focused descriptor/SDK/Core suite passed all 36.

After five minutes the Provider had independently created two immutable
inventory observations and eleven heartbeats, proving the 300-second inventory
refresh is distinct from 30-second health. Neither authority bytes nor
heartbeat metadata contains its bearer credential. The admin view reports one
descriptor, zero active descriptors, zero eligible tools, one Provider, one
fresh inventory and one healthy Provider. Runtime reasons are exactly
`budget_unenforceable`; effective reasons are exactly `inactive_descriptor`,
`budget_unenforceable`, and `execution_off`, because the later hard wall-time
lease executor does not exist. `POST /v1/tool-invocations` remains 404,
execution mode is `off`, and invocation endpoints, leases, and natural-language
callers are all false.

Production remains at `0013_collection_reliability`; `alembic check` reports no
drift and no Phase 3b table was added. Only Core was recreated and the new
Provider was started; PostgreSQL, Lily, Nekro, and NapCat were not restarted.
Legacy claims/events continued across the Core replacement and Nekro remained
healthy. Lily's process and message/event capture also continued, but its
ordinary heartbeat and command snapshot had already stopped refreshing at
22:14 CST, before the 22:42 Core replacement. The stale observer status is
therefore not attributed to this rollout and must be diagnosed separately
before a Phase 3b execution canary; it does not grant any tool authority.

## 3b: invocation and execution safety

- [ ] `off`, `ledger_only`, exact `canary`, and reviewed `enforce` semantics are
  tested. Canary binds tool/version/hash, conversation, caller and provider.
- [ ] Global stop, per-tool/version suspension and provider quarantine each
  independently prevent new leases and have audited rollback.
- [ ] Migration `0014_tool_invocations` and every legal/illegal transition,
  idempotent create, cancellation, deadline and append-only invariant pass both
  databases. `ledger_only` creates no executable lease.
- [ ] Migration `0015_tool_attempts` proves one active lease, monotonically new
  fences, provider/attempt-secret binding and DB-time authority.
- [ ] Duplicate, replayed, late and stale-fence start/heartbeat/complete/fail are
  rejected without mutating the current invocation and remain auditable.
- [ ] Restart, reaper crash, lease expiry, cancellation race, provider/Core
  outage, clock skew, invalid output, safe retry, unknown completion and queue
  starvation have deterministic tested outcomes.
- [ ] Input/output, rate, concurrency, wall-time, CPU, memory and byte budgets
  are enforced or make the provider ineligible; best-effort is never labeled
  hard.
- [ ] Network, filesystem, subprocess, secret, sandbox, remote-fetch and
  artifact permissions are machine-readable, non-escalatable arguments.

## Artifacts and first tools

- [ ] Migration `0016_tool_confirmations_artifacts` passes both databases and
  reservation/upload/finalize state is immutable/idempotent.
- [ ] Upload secrets are one-use/provider-attempt-fence-bound. Core enforces
  expiry, count, bytes, MIME/hash/dimensions and quarantine before atomic
  finalize; late/failed/orphan cleanup is tested.
- [ ] `status.inspect` passes registry with execution off, then ledger-only,
  exact canary, fault/rollback, stable evidence and old-command compatibility.
- [ ] Text-only `wolfram.run` passes worker recovery and resource/error gates;
  image output waits for finalized artifacts.
- [ ] `latex.render` accepts only finalized content-addressed artifacts and
  passes malicious TeX, timeout, MIME/hash/size, cleanup and renderer-boundary
  tests.
- [ ] No provider sends a platform message; command parsing, invocation,
  execution, result, rendering and delivery remain separately observable.
- [ ] Existing command paths remain rollback until per-tool shadow/canary
  equivalence, latency, errors, budgets and evidence window are signed.

## Control panel, security, and operations

- [ ] Read-only panel desired/reported/effective/actual counts and reason codes
  match direct API/SQL evidence; descriptor content is not edited in Phase 3.
- [ ] Auditor/operator/reviewer/security-admin/break-glass boundaries, short
  server-side sessions, CSRF/Origin, reauth, CAS, idempotency, preview, CSP,
  redaction, export and append-only audit tests pass before any mutation.
- [ ] No bearer token is in browser storage, logs, URLs, tool inputs/results,
  artifacts or exported evidence. Provider/bot/admin credentials are distinct
  and rotation/revocation is tested.
- [ ] Production backup/restore, head/drift, image/commit/config hashes,
  kill-switch drill, provider/Core/database outage and rollback are recorded.

## Phase 3 exit

- [ ] `status.inspect`, `wolfram.run`, and `latex.render` use the common
  descriptor/invocation/provider/artifact protocol with stable signed canaries.
- [ ] Natural-language callers remain disabled; no write/admin tool is enabled.
- [ ] All exceptional rows and security/retention findings are explained, docs
  and code are committed, and the operator signs the Phase 3 evidence record.
