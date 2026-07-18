# ADR 0003: Invocation transitions and recovery

- Status: accepted
- Date: 2026-07-18

## Context

Tool execution must survive retries, process crashes, worker restarts, clock
skew, cancellations, and ambiguous completion without granting a stale worker
authority over a newer attempt.

## Decision

- Invocation transitions are append-only and terminal states are immutable.
- `off`, `ledger_only`, exact `canary`, and reviewed `enforce` are distinct
  modes. Global stop, tool/version suspension, and provider quarantine are
  independent inputs that each prevent new leases.
- Providers pull leases. Core does not execute provider code in the API process
  or expose inbound execution ports on Lily or Nekro.
- Database time is authoritative for confirmation expiry, lease/deadline,
  heartbeat freshness, and reaping.
- Every attempt has a monotonically newer fencing token plus a provider-bound,
  attempt-bound secret. Replayed or stale-fence operations append rejection
  evidence and cannot mutate the current invocation.
- Automatic retry requires an explicit retry-safe descriptor policy. Ambiguous
  state-changing completion is terminal manual review.
- Queue fairness, bounded reaping, cancellation races, invalid output, worker
  outage, Core outage, starvation, and restart recovery are contract tests
  before the first executable lease.

## Consequences

Migration `0013_tool_invocations` can support `ledger_only` without leases.
Migration `0014_tool_attempts` adds executable lease/fence state only after the
transition table passes both databases.

## Required evidence

- Complete legal/illegal transition matrix.
- Duplicate, late, replayed, expired, cancelled, and stale-fence tests.
- SQLite and PostgreSQL concurrency tests using database time.
