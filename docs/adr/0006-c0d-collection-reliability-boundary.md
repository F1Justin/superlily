# ADR 0006: C0-D collection reliability boundary

- Status: accepted
- Date: 2026-07-18

## Context

Phase 2 proved canonical event routing and Phase 3a deployed a zero-authority Tool Registry, but bridge telemetry still depends on bounded memory queues and the generic event envelope loses normalized platform-action details. The operator also values the bot-visible conversation history as long-lived community material.

The initial C0 proposal combined durable ingress, platform actions, nested merged-forward expansion, archival exports, reconstruction, retention propagation and old-history import. That was conceptually coherent but made Phase 3b depend on a large archive project whose rare edge cases are unrelated to invocation-ledger correctness.

## Decision

Split collection work into two authority-neutral packets:

- `C0-D` is a Phase 3b prerequisite and covers collection reliability: exact capture profiles, versioned sanitizer/completeness evidence, bridge-owned durable spool identities, Core commit receipts, collector watermarks/gaps, idempotent replay and normalized basic platform actions.
- `C0-A` follows C0-D and covers archive completeness: nested merged forwards, formal `archive_full` rollout, portable exports, reconstruction, retention/deletion propagation and old-history import. C0-A and Phase 3b may proceed independently after C0-D is stable.

Migration `0013_collection_reliability` adds only the Core-side C0-D foundation:

- capture profile/policy snapshots and completeness/sanitizer evidence on `event_observations`;
- exact-scope `conversation_capture_profiles`, with no HTTP mutation route and no production `archive_full` activation;
- `platform_action_observations`, recording facts without feedback semantics;
- one `ingress_receipt` per observation and optional binding to an authenticated instance's `spool_id + sequence + record_sha256`;
- one `collector_watermark` per instance/spool namespace.

The bridge remains the owner of QQ credentials and future durable spool files. Core accepts a spool binding only from that bridge's existing authenticated ingest identity. A local message ID is never global: action target resolution is scoped to observing instance and canonical conversation. Missing or ambiguous targets remain explicit.

Capture profiles are Core authority, not bridge claims. An observation snapshots the effective exact-conversation policy; the bridge reports only completeness, sanitizer version, source payload hash/size, omissions and bounded sanitized platform extras. Image bytes are excluded. Unknown or incomplete collection is recorded as `unassessed`, `partial` or `unavailable`, never upgraded to complete by inference.

The existing `/v1/events` response becomes a durable commit receipt while retaining its observation/source IDs and duplicate flag. Receipts without a spool binding preserve compatibility with current bridges. When a binding is present, duplicate replay must reproduce it exactly; reuse of one instance/spool sequence for another observation is a conflict. Watermarks track highest seen and highest contiguous sequence so gaps remain visible.

## Consequences

- Current bridges and claim behavior remain compatible; no tool, model or platform-send authority is added.
- C0-D2 must implement local spool durability and replay before the receipt/watermark tables prove end-to-end no-loss collection.
- C0-D4 must map real OneBot/NapCat notices into the action contract on both Lily and Nekro.
- C0-A receives new migrations only when its schemas are reviewed; no forward/archive/export table is created by `0013`.
- Previously planned, undeployed Phase 3 migrations shift to `0014_tool_invocations`, `0015_tool_attempts` and `0016_tool_confirmations_artifacts`; the Alembic history stays linear.
- This ADR supersedes only the undeployed migration-number references in accepted ADRs 0003 and 0004. Their protocol, authority and safety decisions remain unchanged.

## Rejected alternatives

- Blocking Phase 3b on all C0 archive work: rejects useful tool-ledger progress for unrelated archival edge cases.
- Treating reactions as feedback records: assigns semantics that collection cannot prove.
- Enabling global raw payload storage: duplicates known content and can retain credentials, temporary media authorization and unbounded personal data.
- Letting Core fetch merged forwards: gives the Core QQ credentials and crosses the adapter boundary.
- Creating a second canonical message-history database: duplicates `source_events`/`event_observations` and makes correlation authority ambiguous.
