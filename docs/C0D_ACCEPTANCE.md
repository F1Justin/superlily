# C0-D collection reliability acceptance

This document tracks the release gate for the authority-neutral C0-D packet.
It deliberately separates the implemented collection foundation from complete
platform-action coverage and production fault evidence. C0-D is not complete,
and Phase 3b remains gated, until the remaining C0-D4/D5 gates below pass.

## Current implementation snapshot

As of 2026-07-18, C0-D1 through C0-D3 are implemented in the workspace and are
awaiting the authorized production rollout. Production remains at
`0012_tool_registry` until the rollout evidence section is updated.

Implemented:

- versioned `ingress`, `capture` and `actions` fields on the backward-compatible
  event contract;
- exact-conversation capture policies, with `operational` as the default and
  no route or deployment that enables `archive_full`;
- immutable observation snapshots for effective policy, completeness,
  sanitizer version, original payload hash/size, omissions and bounded
  sanitized platform extras;
- normalized factual action observations whose unresolved target remains
  explicit and whose local target ID is resolved only within observer and
  conversation scope;
- atomic event/observation/action/receipt persistence, exact replay binding and
  conflict rejection for reused `instance + spool_id + sequence` identities;
- highest-seen and highest-contiguous Core watermarks plus an authenticated
  admin read endpoint;
- independent SQLite `synchronous=FULL` bridge spools for Lily and Nekro,
  stable spool identity/sequence/hash, receipt-checked compaction, persistent
  retry state, strict pending order, quotas and quarantine;
- typed bridge heartbeat status and an authenticated reconciliation view that
  combines local pending/quota/quarantine state with Core lag/gap watermarks;
- separate platform occurrence, bridge capture, Core receive and commit times,
  so reconnect backlog deliveries are not silently rewritten as historical
  event times;
- linear Alembic migration `0013_collection_reliability`, with no C0-A tables
  and no tool execution authority.

C0-D2/D3 are implemented; their production rollout evidence is the remaining
part of C0-D5. Real OneBot action mapping remains C0-D4.

## Verification evidence

The current workspace passes:

- the complete SQLite suite: `233 passed`;
- the complete PostgreSQL 17 suite against the disposable
  `superlily_test` database: `233 passed`;
- fresh Alembic upgrade to `0013_collection_reliability`, schema drift check,
  downgrade to `0012_tool_registry`, and re-upgrade to head;
- targeted Core-offline restart replay, strict pending order, corrupt database
  evidence preservation, receipt mismatch, out-of-order gap closure, sequence
  collision, late action target resolution, exact capture-policy snapshot,
  claim-path receipt and real Core API delivery tests.

These tests establish workspace behavior. They do not replace the controlled
production rollout and post-restart evidence required by C0-D5.

## Remaining release gates

### C0-D2: bridge durability

- [x] Lily atomically appends each eligible record to a local durable spool
  before submitting it to Core.
- [x] Nekro does the same with an independent spool namespace and storage.
- [x] Both bridges delete or compact only records covered by a matching Core
  commit receipt.
- [x] Restart replay preserves `spool_id`, sequence and record hash exactly.
- [x] Disk quota, corrupt-tail handling and quarantine fail visibly without
  silently discarding records.

### C0-D3: coverage diagnostics

- [x] Core stores receipt identity and highest-seen/contiguous watermarks.
- [x] Core exposes authenticated watermark/gap state.
- [x] Bridge status reports pending count/bytes, oldest age, quota pressure and
  quarantined records.
- [x] Core/admin diagnostics expose lag and distinguish pending, gap,
  unavailable and reconciled states without inferring completeness.

### C0-D4: real platform actions

- [ ] Lily maps supported OneBot/NapCat reaction, recall and poke notices into
  the shared action contract.
- [ ] Nekro maps the same observable facts without assigning feedback meaning.
- [ ] Both paths have fixtures for missing actor/target/value fields and for
  ambiguous or unavailable target observations.

### C0-D5: end-to-end faults and rollout

- [ ] Controlled Core outage loses no numbered records.
- [ ] Controlled PostgreSQL outage loses no numbered records.
- [ ] Bridge crash/restart, duplicate replay, out-of-order delivery, corrupt
  spool tail and full-quota behavior have deterministic evidence.
- [ ] Images remain placeholders/metadata rather than PostgreSQL byte payloads.
- [ ] Command behavior, Nekro replies, claim decisions and Tool Registry
  authority are unchanged before and after rollout.
- [ ] Production migration, canary observation and rollback evidence are
  recorded before C0-D is signed complete.

## Explicitly outside this gate

Nested merged-forward expansion, formal `archive_full` activation, portable
exports, reconstruction, retention/deletion propagation and old-history import
belong to C0-A. Once all C0-D gates above pass, those archival capabilities may
proceed independently of Phase 3b and cannot block the invocation ledger on
rare nested-forward cases.
