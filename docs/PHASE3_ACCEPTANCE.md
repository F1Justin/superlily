# Phase 3 acceptance checklist

## Status and entrance gate

Phase 3a contract implementation has started after its recorded Phase 2
entrance prerequisite, and its zero-authority Registry schema/read surface is
now deployed. The accepted ADRs and contract library are evidence for their
narrow checked items only; production Provider/descriptor authority,
invocation, artifact, and execution items remain unchecked until their own
evidence exists.

- [x] `ACCEPTANCE.md` contains the signed policy-v6 Phase 2 controlled samples,
  reused policy-v5 24-hour audit plus policy-v6 counterfactual replay,
  acknowledged claim coordination, response attribution, SQLite/PostgreSQL
  tests, `0011_claim_ack` migration/drift, backup and rollback.
- [x] Phase 3 ADRs approve descriptor/JCS authority, provider identity,
  transitions/recovery, artifact storage, credentials and control-plane auth.

## 3a: authority and effective registry

- [ ] Git-reviewed descriptor bundles are the only authority source; import
  stores immutable canonical bytes/hash, commit, lifecycle and reviewer audit.
- [ ] The restricted JSON Schema profile rejects duplicate keys, non-finite
  numbers, remote/dynamic refs, cycles, unknown/unsafe keywords and all size,
  depth, item and expansion-limit violations.
- [ ] Shared JCS golden vectors produce identical canonical bytes/hash in Core,
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
