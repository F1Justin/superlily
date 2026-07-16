# Phase 2 final production audit

This is the repeatable close-out procedure for the final Phase 2 canary. No
authoritative policy-v5 window is running yet. The next window starts only
after the reviewed code, migration `0011_claim_ack`, Core, and both bridges are
deployed; both bridge counters and exact image/config hashes are recorded; and
the controlled test matrix below passes in `qq:group:708309706`. Its start and
end must then be passed explicitly to `phase2_final_audit.sql`; the script has
no stale date defaults.

The completed policy-v4 window (`2026-07-15 10:15:49 CST` through
`2026-07-16 10:15:49 CST`) is retained as diagnostic evidence, not acceptance.
The full close-out review found defects that aggregate zero-count checks had
missed: collision-prone account-local source IDs, equal strong fingerprints
split by adapter timestamp skew, structural command false positives, private
recipient and reply/summon policy gaps, a conversation-slot Nekro response
attribution race, a deny response that could be committed but not installed by
the peer, and send timeouts that were recorded as confirmed failures despite
ambiguous platform completion. Earlier rejected windows and their evidence
remain in `ACCEPTANCE.md`.

Results from the next window are copied into `ACCEPTANCE.md`; this file is the
procedure, not evidence by itself. `phase2_final_audit.sql` is the read-only
executable form of the count, invariant, outcome, structured-data, instance,
and registry checks below.

At the new deployment boundary, record each bridge's `queue_depth`, `dropped`,
`claim_failures`, process/container start time, code hash, and platform
connection state. Claim requests use a one-second fail-open deadline while
background ingestion uses a separate two-second deadline. Final acceptance
compares the ending counters with these exact baselines and correlates every
increase with timestamped bridge, Core, database, and NapCat logs.

The earlier candidate window was deliberately not signed off. A pre-close
audit found six QQ custom-scheme URI query strings and materially increasing
0.5-second transport timeout counters. The sanitizer, stored rows, and bridge
timeouts were corrected before this replacement window began; the detailed
evidence remains in `ACCEPTANCE.md`.

## Runtime and deployment

At close-out, the evidence must prove every item below. They are acceptance
criteria, not claims about the not-yet-deployed policy-v5 candidate:

- Core container is `healthy`, uses the reviewed image digest, and runs as
  UID/GID 65532.
- `/health/ready` returns database `ok`.
- Core and both bridge runtime copies match the reviewed commit. Restart scope,
  supervisor behavior, and process/container start times are recorded rather
  than inferred from a health endpoint.
- Production is at Alembic head `0011_claim_ack`. Both `/v1/instances` rows are
  online with fresh, identical `onebot_v11.qq.v1` capability snapshots.
- `pip check`, dependency comparison with `deploy/constraints.txt`,
  `alembic current`, and `alembic check` pass.

## Canonical event invariants

For source events first received in the window:

- every source has exactly one `event_decisions` row;
- no source has more than one observation from the same instance;
- a correlation-v3 source has no more than two observations in the current
  two-instance deployment;
- two-observation v3 sources contain Lily and Nekro exactly once each;
- no native-time conflict is hidden inside a merged source;
- one strong correlation fingerprint does not exist on more than one canonical
  source, even when Lily and Nekro normalized timestamps differ by more than
  the legacy two-second window;
- all new QQ message observations use the content-free
  `qq:source:v2:<sha256>` reported source identity; a reused reported source ID
  or idempotency key never maps to two canonical sources. Rejected HTTP 409
  identity conflicts are also enumerated in Core logs because rejected rows
  cannot appear in a database-only query;
- bot-authored events use `observe_only / bot_message_observed` and never
  produce an actionable claim.
- every decision uses `qq-v3-policy-v5`; every group decision records exactly
  `command_only`, `conversation_only`, `full`, or `observe_only`. Commands are
  actionable only in command/full modes and target Lily; conversation is
  actionable only in conversation/full modes and targets Nekro.
- `features.command_eligible` is a JSON boolean equal to an independent
  reconstruction from the deciding observation's leading OneBot segments.
  Leading images, attachments, or an `at` to another account cannot turn later
  text into a Lily command; only a leading reply and the observing bot's own
  ToMe `at` may be skipped before text.
- resolved reply ownership is invariant: a reply to Nekro routes to Nekro in
  a talk-enabled mode regardless of its text, while a reply to Lily remains
  observation-only. Ambiguous/conflicting targets never become actionable. An
  unresolved/reply-to-other message may route to Nekro only when it explicitly
  summons or mentions a known bot and talk is enabled; otherwise it is
  observation-only.
- QQ private messages remain addressed to the receiving account: a Lily-private
  public command can target Lily, ordinary Lily-private text stays observed,
  and only Nekro-private conversation can target Nekro. No account hands a
  private event to a different bot.

The audit records total sources, observations, v3 two-observer sources,
single-observer sources, correlation diagnostics, decision types/reasons, and
policy versions. Counts alone never prove the invariants; violation queries
must return zero rows.

## Claim and behavioral invariants

- `mode=canary` and the exact allowlist contains only
  `qq:group:708309706`.
- No enforced claim exists outside that conversation in the window.
- Every source has at most one enforced allow.
- A deny is coordination evidence only after the denying bridge acknowledges
  that it installed suppression through `POST /v1/claims/{claim_id}/ack`.
- For every enforced allow, the audit independently recomputes the set of peer
  observations received before the allow and the set of prior acknowledged
  enforced peer denies. The sets must match exactly and be non-empty. This gate
  uses database rows and `IS DISTINCT FROM`; it does not trust claim feature
  JSON as the authority. Feature-vs-database drift is a separate violation.
- An acknowledgement may exist only on an enforced deny and cannot precede the
  claim. Delayed, incomplete, unacknowledged, or response-lost peers produce
  `claim_peer_suppressions_not_acknowledged`/another fail-open abstention rather
  than a fictitious exclusive owner.
- Lily denial installs an event-scoped outbound guard and may acknowledge it.
  Nekro denial returns `BLOCK_TRIGGER` but deliberately withholds ACK because
  its public hook offers no post-aggregation confirmation and a later
  `FORCE_TRIGGER` can override the signal. Until that lifecycle gap is closed,
  a controlled Lily-command sample must show Lily `abstain`, Nekro enforced
  unacknowledged deny, one Lily response, and no Nekro response. A controlled
  Nekro-talk sample must show Lily acknowledged deny, Nekro enforced allow, one
  Nekro response, and no Lily response.
- `/v1/decisions/outcomes` is reviewed with a grace period; every `missed`,
  `wrong_instance`, `matched_with_extra`,
  `duplicate_successful_target_response`, `unexpected_response`, confirmed
  failure, `ambiguous_completion`, or unlinked response is inspected rather
  than hidden in an aggregate. `success=false` plus
  `metadata.completion_status=ambiguous` is never silently counted as a
  confirmed failure and is never blindly retried.
- A linked response has a same-instance observation on the same canonical
  platform/conversation/source. Lily's trigger attribution is `event_context`;
  Nekro's is `task_context`, bound to the actual scheduler task rather than one
  mutable conversation slot. The current protocol has no multipart response
  group, so more than one successful response from one instance for one trigger
  is a violation requiring row-level review.

Core/transport failures remain fail-open. Therefore claim rows are coordination
evidence, while linked actual responses and bridge logs are the behavioral
authority.

## Registry, security, and data

- The runtime command snapshot is fresh and hash-valid. Fully introspected
  public triggers are covered; incomplete/unreviewed matches remain unable to
  authorize enforcement.
- All 48 random draw triggers and 90 random mutation triggers remain covered
  by the reviewed registry; the runtime audit contains no uncovered
  `nonebot-plugin-random` trigger. Mutation remains sensitive/non-public Core
  authority even if local plugin configuration is more permissive.
- `/v1/command-registry/runtime` must report a fresh Lily snapshot and an empty
  random-plugin subset of `uncovered_candidates`; the SQL snapshot count alone
  is not a coverage proof because the reviewed registry is file-backed.
- The reviewed non-random uncovered baseline is 55 triggers: 38 blacklist
  SUPERUSER controls, five event-monitor administrator/SUPERUSER controls,
  five matcher-block administrator/SUPERUSER controls, five today-waifu
  regex forms already covered semantically by the static public/admin rules,
  and two word-cloud admin regex forms already covered semantically. All are
  incomplete runtime candidates and therefore force claim abstention. A
  changed count/plugin distribution is new evidence to investigate; the
  unchanged reviewed baseline is not a newly discovered public-command gap.
- `raw_json` is SQL `NULL` or JSON literal `null` for window events/responses;
  no object, array, string, number, or boolean payload is retained. PostgreSQL
  JSON columns may encode Python `None` as JSON `null`, so `IS NOT NULL` alone
  is not a valid leakage query.
- Structured metadata/segments/attachments/reference raw contain no sensitive
  key whose value is not `[REDACTED]`, URL/URI userinfo, or query/fragment
  suffixes in `url`, `uri`, `link`, `href`, `src`, `file`, and `platform_id`
  fields regardless of scheme. The audit also scans custom-scheme scalar URIs
  and rejects retained local `file://` identifiers. A redacted sensitive key
  name is evidence that the sanitizer ran, not a leak. User-authored/display
  text is not searched as if it were structured configuration and is not
  silently altered. U+0000 is the sole transport-safety exception and is
  replaced recursively with U+FFFD because PostgreSQL rejects NUL in text and
  JSON values.
- Reporter drop/claim-failure counters, Core 4xx/5xx, bridge warnings, failed
  responses, and instance status transitions are enumerated for the window.
- The existing retention boundary remains explicit: the audit does not delete
  chat history or change keys without separate operator authorization.

## Final gate

After all violation sets are empty and every exceptional row is explained:

1. deploy policy v5/Core/bridges and `0011_claim_ack`, then record the new
   baseline; do not reuse a pre-fix hour;
2. pass controlled samples in `708309706`: Lily command, Nekro summon, reply to
   Lily, reply to Nekro with and without QQ's automatic `at`, reply to another
   user with and without an explicit summon, leading other-user `at`, leading
   image/non-text, private recipient routing, two close messages in one Nekro
   chat task sequence, Lily claim acknowledgement, withheld Nekro
   acknowledgement, simulated lost deny response,
   Core timeout, and ambiguous send timeout;
3. run an uninterrupted new period of at least 24 hours and execute the SQL
   with its exact explicit start/end timestamps;
4. rerun all tests on SQLite and PostgreSQL 17.10;
5. rerun the fresh PostgreSQL `base -> head -> base -> head` migration chain;
6. verify production head/drift and the pre-migration backup listing;
7. add an authoritative Nekro outbound suppression guard or upstream
   post-aggregation hook, then update every remaining Phase 2 checkbox and the
   review conclusion;
8. run `compileall`, `pip check`, `git diff --check`, and secret-path review;
9. commit the complete Phase 2 implementation and documentation;
10. only after the operator signs this evidence may Phase 3a contract/hash work
    begin. Phase 3 has not started.
