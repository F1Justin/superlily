# ADR 0007: Phase 3 migration allocation after C0-D

- Status: accepted
- Date: 2026-07-18

## Context

ADR 0003 and ADR 0004 froze the original invocation, attempt, and artifact
migration numbers before the authority-neutral C0-D collection reliability
foundation was inserted. C0-D was then deliberately assigned
`0013_collection_reliability`. Reusing the old numbers would now create two
different histories for the same Alembic revision names.

## Decision

- `0012_tool_registry` remains the Phase 3a authority and Provider registry.
- `0013_collection_reliability` remains the completed C0-D foundation and
  contains no tool execution state.
- The invocation ledger is `0014_tool_invocations`.
- Lease, attempt, and fencing state is `0015_tool_attempts`.
- Confirmation and artifact state is `0016_tool_confirmations_artifacts`.

This ADR amends only the migration-number references in the consequences of
ADRs 0003 and 0004. Their transition, recovery, fencing, and artifact decisions
are unchanged.

## Consequences

No Phase 3b implementation may reuse `0013`, and no archive-completeness work
may be inserted into the invocation, attempt, or artifact migrations. Future
splits receive new revision numbers rather than renaming an applied revision.

## Required evidence

- Documentation and acceptance checklists use one allocation.
- Fresh upgrade, downgrade/re-upgrade, production-head, and drift checks reject
  any conflicting revision history.
