# ADR 0002: Provider identity, inventory, and health

- Status: accepted
- Date: 2026-07-18

## Context

Bot ingest identity, administrator identity, implementation discovery, and
current worker health are different trust surfaces. Combining them would let a
display name, heartbeat, or discovered plugin accidentally gain authority.

## Decision

- Providers have stable opaque IDs and credentials unrelated to bot ingest and
  administrator credentials. Token reuse across these classes is rejected.
- Registration records the allowed provider protocol versions, owner,
  lifecycle, descriptor selectors, and credential rotation metadata.
- Inventory snapshots are immutable, content-hashed reports of tool versions,
  descriptor hashes, implementation hashes, protocols, and hard/best-effort
  budget support.
- Heartbeats are separate health/load observations tied to one accepted
  inventory hash. A heartbeat does not refresh inventory, and inventory does
  not prove current health.
- Runtime state never expands descriptor callers, permissions, side effects,
  network/filesystem/process access, retry policy, or resource budgets.
- Eligibility is a structured result with stable reason codes. Missing, stale,
  unknown, mismatched, quarantined, or under-enforced providers are ineligible.

## Consequences

Phase 3a needs separate provider authentication and desired/reported/effective
views. The first deployment contains zero active descriptors and execution is
off, so a valid provider snapshot still cannot create work.

## Required evidence

- Token separation and provider/payload binding tests.
- Immutable snapshot hash/idempotency tests.
- Independent inventory-staleness and heartbeat-staleness tests.
- Stable eligibility reason vectors for every less-authoritative state.
