# Acceptance checklist

## Current roadmap position

- [x] Phase 1 observability spine is deployed.
- [x] Phase 2a reply links, unresolved-link retention, debug views, and a
  non-writing history-import dry run are implemented.
- [x] Phase 2a.1 native QQ identity collection ran in production for 24 hours.
- [x] Phase 2a.2 correlation v3 and deterministic canonical decisions.
- [x] Phase 2b.2 runtime command-registry synchronization.
- [x] Phase 2b shadow decision/actual-response comparison and acceptance.
- [ ] Phase 2c fail-open claim-lock canary.
- [ ] Phase 2 completion review before Tool Registry work begins.

## Phase 2a.1 production evidence

The 24-hour snapshot ending 2026-07-11 16:51 CST contained 9,133 Lily
messages and 3,273 Nekro messages. Both bridges captured the complete native
identity allowlist for 100% of those stored messages. Among 2,679 messages
observed by both accounts, `real_seq`, sender, conversation, and native time
had zero conflicts, while account-local `message_id`, `message_seq`, and
`real_id` matched zero times. Correlation v2 consequently produced 5,358
source events and 5,358 decisions for those 2,679 messages.

Text is explicitly disqualified as a strong identity input: 717 paired
observations had different text representations, and sender/text/two-second
fuzzy matching produced 11 false candidate edges in the same snapshot.

## Phase 2a.2 acceptance

- [x] Use group-message QQ `real_seq` plus canonical conversation and sender
  for correlation v3; treat native time as a conflict guard rather than key
  material, and leave private messages uncorrelated.
- [x] Never cross-account merge from account-local IDs or fuzzy text/time.
- [x] Two accounts observing one message produce one source event, two
  observations, and one canonical decision.
- [x] Two rapid identical messages with different `real_seq` stay separate.
- [x] Recomputing after either observation order produces the same decision.
- [x] Reply to a Nekro response routes to `talk / nekro-agent` with the QQ
  automatic `at` either retained or deleted.
- [x] Reply to a Lily response remains `observe_only` with the QQ automatic
  `at` either retained or deleted.
- [x] A reply to another user remains `observe_only`; an automatic reply `at`
  is not treated as an independent summon.
- [x] Conflicting or missing native identity remains fail-open and visible in
  diagnostics.
- [x] Production shadow verification shows no duplicate canonical decisions
  for messages observed by both accounts.

The first post-v3 production sample, from deployment at 2026-07-12 17:41 CST
through the 18:08 review, contained 514 source events and 606 observations.
Ninety-two source events had observations from both instances; none had more
than two instances or more than one observation from the same instance. All
514 sources had exactly one decision row.

## Historical data dry run

- [x] Nekro's real PostgreSQL history was inspected read-only. At 2026-07-11
  18:20 CST it held 1,142,966 rows from 2025-09-11 through 2026-07-11.
- [x] The Core overlap boundary is explicit. Nekro has 1,035,247 rows before
  its first Core event and 107,719 overlapping/newer rows that must never be
  re-imported.
- [x] The conservative Nekro candidate set is 947,371 pre-Core OneBot rows
  with non-empty local message IDs; 115,055 carry reply hints and 740,822
  carry text. Fifty local `(adapter, chat, message_id)` keys are duplicated
  twice and require source-specific idempotency.
- [x] Lily's real chatrecorder database has the equivalent read-only counts
  and overlap boundary recorded in `docs/HISTORY_DRY_RUN.md`.
- [x] Historical rows have no verified cross-account `real_seq`. Therefore no
  Lily/Nekro text-time merge or bulk copy is authorized. Any future import is
  source-specific, excludes the overlap period, and creates no synthetic
  canonical equality.
- [x] Run the resolver against the live pending links. At execution time the
  live queue had grown to 821 links. The indexed resolver examined all 821 in
  1.17 seconds, resolved only 8 unique targets, classified 0 as ambiguous, and
  retained 813 without target evidence. An immediate repeat resolved 0.

## Phase 2b.2 runtime registry and outcome audit

- [x] Authenticated snapshots reject a content-hash mismatch.
- [x] An unchanged periodic snapshot refreshes liveness without duplicating
  content; missing/stale snapshots are visible and force claim abstention.
- [x] Runtime discovery covers NoneBot command/shell rules, Alconna main
  commands and aliases, exact/prefix/suffix/keyword/regex rules where they are
  safely introspectable.
- [x] Static rules are filtered to loaded plugins. Runtime candidates without
  reviewed target/permission/sensitive metadata remain uncovered and never
  become enforcement authority.
- [x] Composite/custom-rule or permission-constrained matchers are marked
  incomplete instead of being overclaimed; sensitive and non-public commands
  abstain until Core has an authorization model.
- [x] `/v1/decisions/outcomes` distinguishes matched, missed, wrong-instance,
  failed, pending, and unexpected responses over a bounded window.
- [x] Nekro's inferred response trigger is consumed once and explicitly
  labeled; Core target selection supplements Nekro's local ToMe flag, while
  proactive later sends remain unlinked.
- [x] Production runtime inventory and a controlled decision/response sample
  have been reviewed.

The deployed Lily snapshot is fresh and hash-verified: 28 plugins, 198
matchers, 191 classified matchers, and 194 candidate rows. Of those, 183 rows
are deliberately incomplete because of custom/composite rules or permission
checkers. The 193 uncovered triggers are all incomplete; no fully introspected
public trigger remains uncovered.

The controlled shadow sample at 2026-07-12 23:34 CST used
`莉莉，superlily-shadow-2333` in QQ group `708309706`. Lily and Nekro reported
different account-local message IDs but the same native `real_seq=11085`,
forming one correlation-v3 source with two observations and one revision-2
`talk -> nekro-agent` decision. Claims were ready `allow` for Nekro and ready
`deny` for Lily with zero enforcement in shadow mode. Nekro's successful reply
was stored with the canonical trigger source, closing the previously unlinked
response gap.

## Phase 2c claim-lock canary

- [x] Claims are idempotent per source event and instance, and an enforced
  allow owner cannot race to a different instance.
- [x] Only actionable `command`/`talk` decisions can allow or deny;
  `observe_only` abstains.
- [x] Missing v3 identity, fewer than two observations, low confidence,
  stale registry, uncovered runtime trigger, uncertain reply, or offline
  target each abstains.
- [x] Incompletely introspected, sensitive, or non-public commands also
  abstain rather than suppressing the other bot.
- [x] Shadow claim requests run on both bridges with zero enforced claims.
- [ ] One exact QQ test conversation runs in canary mode; all other
  conversations remain unchanged.
- [ ] Lily deny suppresses sends without disabling chat recording. Nekro deny
  preserves history with `BLOCK_TRIGGER`.
- [x] A Core outage/timeout leaves both bots on their existing behavior.
- [ ] Canary evidence is stable before Phase 3 begins.

## Phase 1 acceptance

## Automated

- [x] Contract rejects timezone-naive events.
- [x] Secrets and URL queries are removed from optional diagnostic payloads.
- [x] Replaying an idempotency key does not create another observation.
- [x] Two bot accounts can observe one source event independently.
- [x] Two bot accounts can concurrently create observations of one source event.
- [x] Instance tokens cannot impersonate another instance.
- [x] Responses without a trigger event are accepted.
- [x] Heartbeats update liveness and admin endpoints remain protected.
- [x] A full bridge queue drops telemetry immediately instead of blocking.
- [x] Lily bridge imports under the installed Lily NoneBot runtime.
- [x] Nekro bridge imports under the pinned Nekro 2.2.1 image.
- [x] The production image builds, migrates a fresh PostgreSQL 17 database, and serves health APIs as a non-root user.
- [x] A real background reporter writes through Core into PostgreSQL.
- [x] With Core stopped, a report fails in the background without blocking or crashing the caller.

## Live smoke test before enabling broad ingestion

- [x] Confirm Lily is running in the `nb` tmux session under the enabled
  `tmux-nb.service` auto-restart supervisor.
- [x] Confirm the current Lily process is receiving live OneBot events before
  bridge installation.
- [ ] Record one known-good Lily command response before bridge installation.
- [ ] Confirm one Lily message and response appear in Core.
- [x] Confirm one Nekro message and `message_sent` response appear in Core.
- [x] Stop Core for two minutes and verify both bots continue processing.
- [x] Restart Core and verify heartbeats recover without bot restarts.
- [x] Confirm images store metadata only and no remote URL query strings in
  records created after the reviewed deployment.
- [x] Confirm no structured access token or model API key appears in recent
  records.

The 2026-07-12 outage drill kept Lily's tmux MainPID and Nekro's container
StartedAt unchanged while Nekro continued collecting at least 18 group
messages. Both bridges logged rate-limited fail-open transport failures. Core
then returned ready and accepted both heartbeats without either bot restarting.
The post-deployment security window contained 430 observations at audit time:
zero retained raw payloads, zero structured sensitive markers, and zero
structured URL queries. A separate historical maintenance pass sanitized 637
legacy structured rows with zero truncation; user-authored text was retained.

The final pre-canary build passed 86 tests independently on SQLite and
PostgreSQL 17. A disposable PostgreSQL database completed
`base -> 0010 -> base -> 0010`; the migration graph has one head, production
reports `0010_claim_owner_index`, and `alembic check` found no schema drift.

The unchecked items intentionally require an operator-visible live deployment;
development tests do not mutate either running bot.
