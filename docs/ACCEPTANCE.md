# Acceptance checklist

## Current roadmap position

- [x] Phase 1 observability spine is deployed.
- [x] Phase 2a reply links, unresolved-link retention, debug views, and a
  non-writing history-import dry run are implemented.
- [x] Phase 2a.1 native QQ identity collection ran in production for 24 hours.
- [x] Phase 2a.2 correlation v3 and deterministic canonical decisions.
- [x] Phase 2b.2 runtime command-registry synchronization.
- [x] Phase 2 typed platform-capability snapshots are live on both bridges.
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
- [x] A reply to another user without an independent summon remains
  `observe_only`; an automatic reply `at` is not treated as an independent
  summon. Policy v5 separately allows explicit summon text/known-bot mention in
  talk-enabled groups.
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
- [x] The earlier Nekro one-shot inferred response trigger and controlled
  sample were reviewed. The final review later found this design insufficient
  under overlapping scheduler tasks; policy v5 replaces it with task-bound
  attribution and requires new evidence below.
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

## Platform capability model

- [x] Heartbeat contracts use a typed, versioned capability profile rather
  than arbitrary metadata or adapter-name inference.
- [x] Unsupported and missing capabilities remain unknown; no wildcard or
  implicit “supports everything” behavior exists.
- [x] The conservative OneBot QQ profile declares only text, image, reply, and
  mention, all exercised by the existing adapters.
- [x] Lily and Nekro production heartbeats expose identical validated
  `onebot_v11.qq.v1` snapshots in `/v1/instances`.

## Phase 2c claim-lock canary

- [x] Claims are idempotent per source event and instance, and an enforced
  allow owner cannot race to a different instance.
- [x] The earlier deny-before-allow database ordering was implemented and
  canaried.
- [ ] Policy v5 enforced allow requires an acknowledged installed suppression
  from every other observed instance; a committed deny whose HTTP response was
  lost cannot create a fictitious exclusive owner. Lily denial can satisfy
  this gate. Nekro intentionally withholds ACK because its public plugin API
  has no post-signal-aggregation hook and `FORCE_TRIGGER` can override
  `BLOCK_TRIGGER`; Lily-target claims therefore conservatively abstain pending
  an outbound suppression guard/upstream lifecycle hook.
- [x] Decision recomputation is serialized per canonical source across event,
  response, resolver, and claim paths.
- [x] Only actionable `command`/`talk` decisions can allow or deny;
  `observe_only` abstains.
- [x] Missing v3 identity, fewer than two observations, low confidence,
  stale registry, uncovered runtime trigger, uncertain reply, or offline
  target each abstains.
- [x] Incompletely introspected, sensitive, or non-public commands also
  abstain rather than suppressing the other bot.
- [x] Shadow claim requests run on both bridges with zero enforced claims.
- [x] One exact QQ test conversation runs in canary mode; all other
  conversations remain unchanged.
- [x] Lily deny suppresses sends without disabling chat recording. Nekro deny
  preserves history with `BLOCK_TRIGGER` but is not yet acknowledged as an
  authoritative installed suppression.
- [x] A Core outage/timeout leaves both bots on their existing behavior.
- [ ] `qq:source:v2` bridge identity, Core conflict rejection, strong
  fingerprint de-splitting, structural `command_eligible`, private-recipient
  policy, task-bound response attribution, and ambiguous completion are
  deployed and covered by controlled samples.
- [ ] Canary evidence is stable before Phase 3 begins.

## Phase 1 acceptance

- [x] The original health table/API sketch is satisfied by readiness,
  `bot_instances`, and append-only status transitions.
- [x] Empty conversation/identity authority tables and an unused Redis service
  are deliberately deferred; no Phase 2 claim depends on either. Formal models
  remain a gate before administrator writes or a second platform.

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
- [x] The original pre-install baseline checkpoint is superseded by a stronger
  controlled canary comparison; no retroactive pre-install claim is made.
- [x] Confirm one Lily message and response appear in Core.
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

The final reviewed build passed 91 tests independently on SQLite and
PostgreSQL 17. A disposable PostgreSQL database completed
`base -> 0010 -> base -> 0010`; the migration graph has one head, production
reports `0010_claim_owner_index`, and `alembic check` found no schema drift.

The exact-conversation canary used QQ group `708309706`, which the operator
confirmed is an otherwise empty test group. `wf 2+3` produced one enforced Lily
allow, one enforced Nekro deny, and one Lily response. A “莉莉” summon and a
reply to Nekro with QQ's automatic `at` deleted each produced one enforced
Nekro allow, one enforced Lily deny, and one linked Nekro response. An ordinary
message and a reply to Lily with automatic `at` deleted both abstained and
produced no response. Nekro's database retained the denied command event. No
conversation outside the exact allowlist produced an enforced claim.

Review then found that bot-authored replies containing “莉莉” polluted outcome
audits even though the two-observation gate safely abstained. Policy v2 now
classifies known bot senders as `observe_only / bot_message_observed`; a live
v2 canary proved both the intended user-message routing and the corrected bot
output decision.

The first deny-before-allow production sample exposed a telemetry-only edge:
when Nekro was the canonical target but safely received
`abstain / claim_peers_not_denied`, its local normalized message still had
`is_tome=false`. Nekro replied through its legacy matcher as intended, but its
response was stored without a trigger. Claim gates now expose the canonical
decision type and target as correlation metadata, and the Nekro bridge retains
that target on abstention without treating it as authorization.

The corrected controlled sample at 2026-07-13 10:26 CST used
`莉莉，superlily-response-link-1025` in the exact canary group. The two bot
views had different account-local message IDs and the same native
`real_seq=11101`, producing one v3 source, two observations, and one revision-2
`talk -> nekro-agent` decision. Nekro again received
`abstain / claim_peers_not_denied` before Lily's enforced deny, then produced
exactly one successful response linked to the canonical trigger. There was no
non-target response, and Lily classified the outgoing Nekro message as
`observe_only / bot_message_observed`.

The response-attribution candidate image was
`sha256:7f0f4091eb811e55d13b50eb67d74c0e1013f8c82acd58f66e2023d689111968`.
Core and PostgreSQL were recreated by Compose at 2026-07-13 10:23 CST; the
existing database volume retained 298,620 source events, 317,848 observations,
180,886 decisions, 9,680 claims, and 12,691 responses immediately after the
restart, and production remained at Alembic head `0010_claim_owner_index`.
Nekro restarted once to load the bridge fix and reconnected at 10:24:05 CST.
Lily remained live in its existing `nb` tmux process and resumed fresh Core
heartbeats after the brief Core outage.

That candidate window was not accepted. At 2026-07-14 10:06 CST, before its
scheduled 10:24:05 endpoint, a pre-close audit of 14,626 sources, 17,467
observations, 14,626 decisions, 14,163 claims, and 949 responses found:

- zero canonical-event violations and zero claim-coordination violations;
- zero enforced claims outside `qq:group:708309706` and exactly one enforced
  deny in the canary;
- 45 missed talk outcomes, of which 32 had an unlinked successful Nekro
  response in the same conversation within five minutes and 23 within 30
  seconds. That timing is compatible with the known correlation hint being
  lost when a claim request timed out, but does not by itself prove one-to-one
  attribution;
- three explicit platform send failures and four unexpected Lily responses for
  manual comparison in the replacement window;
- six retained QQ `mqqapi://`/`mqzone://` query strings in URL-typed segment
  fields; and
- Lily counters `dropped=21, claim_failures=156`, up 17 and 153 from baseline.
  Nekro's counters after an operator-issued 2026-07-13 21:40:27 restart were
  `dropped=4, claim_failures=4`. The Core remained healthy; bridge logs identify
  the failures as 0.5-second `ReadTimeout` fail-open events.

The Nekro restart degraded its Core status at 21:40:44 and returned online at
21:41:14. Lily continued recording events during the gap, and no single-observer
event obtained an enforced allow. This is useful fail-open evidence, but a
manual restart does not excuse the transport or sanitizer findings above.

The remediation separates the one-second claim deadline from the two-second
background-ingestion deadline and sanitizes URL/URI userinfo, query, and
fragment data for every scheme. Twelve affected observation rows were updated
in one transaction; an immediate idempotency pass found zero remaining changes,
and the structured URL violation query returned zero. The post-fix build passes
92 tests on both SQLite and PostgreSQL 17.10, `compileall`, `pip check`, and the
fresh `base -> 0010 -> base -> 0010` PostgreSQL chain.

The replacement Core image is
`sha256:8dbd276b3def03241a51c36986f5274b1f8bea5a12debe2163a766a7cb20dc7a`.
Core started at 2026-07-14 10:14:11 CST. Lily was restarted through its existing
tmux/systemd supervisor and reconnected at 10:16:34; only after Lily returned
online was Nekro restarted, reconnecting at 10:17:25. Both bridges reported
online with `queue_depth=0, dropped=0, claim_failures=0`, identical
`onebot_v11.qq.v1` capabilities, and the fresh 28-plugin/194-candidate runtime
registry before the replacement window began.

The authoritative replacement window is 2026-07-14 10:19:02 CST through
2026-07-15 10:19:02 CST. A pre-Phase-3 production dump was created at
`/home/justin/backups/superlily/superlily-phase2-final-20260714.dump` before
the corrective deployment: 105,934,432 bytes, PostgreSQL 17.10 custom format,
78 TOC entries, SHA-256
`85acab932dd32944349e3a9622e4cc70cbc39a6a485cdaf96917784c0f628d02`.
`pg_restore --list` succeeded. No retained pre-Phase-2-migration dump was found,
so this is explicitly a current rollback baseline rather than retroactive
evidence.

That 24-hour window was not accepted. Its structural invariants remained
clean, but the close-out review found three release blockers:

- one QQ message containing U+0000 produced four Core 500 responses across
  Lily/Nekro event and claim reporting because PostgreSQL rejects NUL in text
  and JSON values;
- a resolved reply to a Nekro response whose body was `换老婆` was classified as
  a Lily command before reply ownership, producing one `matched_with_extra`
  outcome; and
- six random-plugin outcomes were classified as wrong-instance and most
  unexpected Lily responses were directory-derived random commands absent
  from the static registry.

The bounded outcome review contained 13,459 sources, 16,135 observations,
13,459 decisions, 13,155 claims, and 729 responses. Canonical and claim
invariant violation queries returned zero. The classified outcomes were 617
matched, one matched-with-extra, 28 missed, seven unexpected responses, six
wrong-instance, two failed, and approximately 12,798 matched-no-response.
Most missed rows had timing-compatible but unlinked Nekro responses; two were
separately explained by sibling-prompt attribution and the observed NapCat
send outage. Those explanations do not waive the three blockers above.

Policy v3 was deployed in Core image
`sha256:268e0e02a2e5fa958963fa3764ee05644675131ba6fe53352ba118bdbdbe40b8`,
started at 2026-07-15 09:05:24 CST. It:

- replaces U+0000 recursively with U+FFFD at the versioned wire-model boundary
  before correlation and persistence;
- gives resolved reply ownership precedence over command/summon text, with
  replies to Nekro routing to Nekro and replies to Lily/other users remaining
  observation-only;
- reviews all 48 live random draw prefixes and 90 random add/delete prefixes,
  using NoneBot's longest-command-prefix semantics and treating mutation as
  sensitive group-administrator authority; and
- adds an initial audited per-group `command_only`/`full` policy.

The deployed runtime inventory remained fresh at 28 plugins and 194 candidate
rows. Random-plugin uncovered triggers fell from 138 to zero; total uncovered
triggers fell from 193 to 55, all still subject to the existing incomplete or
unreviewed abstention boundary. A production PostgreSQL smoke sent U+0000
through both account claim paths and a response path. Both observations
correlated to one source, every request succeeded, and text, segment, error,
and metadata values stored U+FFFD with policy
`qq-v3-policy-v3 / conversation_mode_command_only`.

The 55-trigger non-random baseline was reviewed item by item before policy v4:
38 are blacklist SUPERUSER controls, five are event-monitor admin/SUPERUSER
controls, five are matcher-block admin/SUPERUSER controls, five are
today-waifu runtime regex spellings already covered semantically by the static
public/admin rules, and two are word-cloud admin regex spellings already
covered semantically. Every row is runtime-incomplete and therefore claim
authority abstains. There is no remaining uncovered public random/high-traffic
command; an unchanged 55-row baseline is a known permission/introspection
boundary rather than a later release blocker.

The policy-v3 replacement window was stopped early by an immediate preflight
review rather than waiting 24 hours. The binary map had treated recent Nekro
observation traffic as conversation availability: group `949959173` was
marked `full` even though Nekro's authoritative channel row was
`is_active=false`, while four active Nekro-only groups could not express that
Lily commands were unavailable. Observation proves collection, not response
permission. Static command matching also depended on manually ordering
overlapping command prefixes even though runtime matching already followed
NoneBot's longest-prefix trie.

Policy v4 was deployed in Core image
`sha256:ea07129a1b28fd81ca3bc6b65f9e77029fb01d475ea22c314744ac3980d49f9d`,
started at 2026-07-15 10:13:35 CST. It retains the NUL/reply/random fixes and:

- models `command_only`, `conversation_only`, `full`, and `observe_only`;
- derives the reviewed production map from Lily's live `get_group_list` and
  Nekro's channel `is_active` state: six full, four conversation-only, and ten
  explicit observe-only groups, with unlisted groups defaulting to
  command-only; and
- applies longest-command-prefix matching in both the static registry and the
  runtime inventory path.

The final reviewed build passed 107 tests independently on SQLite and
PostgreSQL 17.10. A freshly recreated disposable test schema completed
`base -> 0010 -> base -> 0010`; both test and production `alembic check`,
`compileall`, `pip check`, Compose validation, and `git diff --check` passed.
Policy-v4 deployment replaced only Core; Lily, Nekro Agent, and both NapCat
processes stayed running.

Production smoke covered all four modes plus both resolved reply owners. In a
full group, body `换老婆` replying to Nekro routed `talk -> nekro-agent`; body
`莉莉继续` replying to Lily stayed observation-only. Command-only routed
`换老婆` to Lily but suppressed a summon; conversation-only suppressed that
command but routed the summon to Nekro; observe-only routed neither. The live
runtime snapshot was fresh at 28 plugins/194 candidates with zero uncovered
random triggers.

The policy-v4 window from 2026-07-15 10:15:49 CST through
2026-07-16 10:15:49 CST completed, but the final code/row review rejected it as
authoritative Phase 2 evidence. The previous aggregate audit did not detect:

- collision-prone short NapCat message IDs reused as reported source and
  idempotency identity;
- equal strong fingerprints split when Nekro's normalized timestamp lagged
  Lily beyond the correlation window;
- concatenated text recognizing a command after an `at` to another account or
  another leading non-text segment;
- private-account and unresolved/reply-to-other summon routing gaps;
- a mutable conversation-local Nekro response trigger overwritten by a later
  message while the first scheduler task was still running;
- a Core-committed deny whose HTTP response could be lost before the bridge
  installed suppression; and
- a Nekro `BLOCK_TRIGGER` that cannot be acknowledged as authoritative before
  the SDK completes aggregate plugin signal handling; and
- OneBot send timeouts recorded as confirmed failures even when peer evidence
  showed the message may already have been delivered.

Policy v5, bridge source identity v2, task-bound trigger attribution, explicit
ambiguous completion, and `0011_claim_ack` are the remediation set. They are not
claimed deployed or accepted in this record. After review/tests/deployment,
the controlled matrix in `PHASE2_FINAL_AUDIT.md` must pass in test group
`708309706`. A new baseline then starts a new uninterrupted 24-hour window;
its exact timestamps, counters, hashes, SQL output, exceptional-row review,
and operator signature will be appended here. Phase 3 has not started.

The unchecked items intentionally require an operator-visible live deployment;
development tests do not mutate either running bot.
