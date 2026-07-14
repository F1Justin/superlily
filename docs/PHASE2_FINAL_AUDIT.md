# Phase 2 final production audit

This is the repeatable close-out procedure for the final Phase 2 canary. The
authoritative replacement window is `2026-07-14 10:19:02 CST` through
`2026-07-15 10:19:02 CST` (UTC `2026-07-14T02:19:02Z` through
`2026-07-15T02:19:02Z`). Results are copied into `ACCEPTANCE.md`; this file is
the procedure, not evidence by itself. `phase2_final_audit.sql` is the
read-only executable form of the count, invariant, outcome, structured-data,
instance, and registry checks below. Its psql variables pin the same bounded
window and can be overridden explicitly for a later rerun.

The replacement post-deployment counter baseline is zero for both bridges at
2026-07-14 10:19:02 CST: `queue_depth=0, dropped=0, claim_failures=0`. Claim
requests use a one-second fail-open deadline while background ingestion uses a
separate two-second deadline. Final acceptance checks that these baselines do
not increase and correlates any increase with timestamped bridge/Core logs.

The earlier candidate window was deliberately not signed off. A pre-close
audit found six QQ custom-scheme URI query strings and materially increasing
0.5-second transport timeout counters. The sanitizer, stored rows, and bridge
timeouts were corrected before this replacement window began; the detailed
evidence remains in `ACCEPTANCE.md`.

## Runtime and deployment

- Core container is `healthy`, uses the reviewed image digest, and runs as
  UID/GID 65532.
- `/health/ready` returns database `ok`.
- Lily's supervisor restart and Nekro's sequential restart are recorded; both
  `/v1/instances` rows are online with fresh, identical `onebot_v11.qq.v1`
  capability snapshots. The two observers were never intentionally stopped at
  the same time.
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
- bot-authored events use `observe_only / bot_message_observed` and never
  produce an actionable claim.

The audit records total sources, observations, v3 two-observer sources,
single-observer sources, correlation diagnostics, decision types/reasons, and
policy versions. Counts alone never prove the invariants; violation queries
must return zero rows.

## Claim and behavioral invariants

- `mode=canary` and the exact allowlist contains only
  `qq:group:708309706`.
- No enforced claim exists outside that conversation in the window.
- Every source has at most one enforced allow.
- Every enforced allow's `features.coordination.observed_peer_instance_ids`
  equals its `enforced_deny_instance_ids`; an allow may not precede its peer
  deny record.
- Delayed or incomplete peers produce `claim_peers_not_denied`/another
  fail-open abstention rather than a fictitious owner.
- The final controlled command and talk samples each have two observations,
  one canonical decision, the safe claim pair, one successful target response,
  and no successful non-target response.
- `/v1/decisions/outcomes` is reviewed with a grace period; every `missed`,
  `wrong_instance`, `matched_with_extra`, `unexpected_response`, failed, or
  unlinked response is inspected rather than hidden in an aggregate.

Core/transport failures remain fail-open. Therefore claim rows are coordination
evidence, while linked actual responses and bridge logs are the behavioral
authority.

## Registry, security, and data

- The runtime command snapshot is fresh and hash-valid. Fully introspected
  public triggers are covered; incomplete/unreviewed matches remain unable to
  authorize enforcement.
- `raw_json` is SQL `NULL` or JSON literal `null` for window events/responses;
  no object, array, string, number, or boolean payload is retained. PostgreSQL
  JSON columns may encode Python `None` as JSON `null`, so `IS NOT NULL` alone
  is not a valid leakage query.
- Structured metadata/segments/attachments/reference raw contain no sensitive
  key whose value is not `[REDACTED]`, URL/URI userinfo, or query/fragment
  suffixes in URL-typed fields regardless of scheme. A redacted sensitive key
  name is evidence that the sanitizer ran, not a leak. User-authored/display
  text is not searched as if it were structured configuration and is not
  silently altered.
- Reporter drop/claim-failure counters, Core 4xx/5xx, bridge warnings, failed
  responses, and instance status transitions are enumerated for the window.
- The existing retention boundary remains explicit: the audit does not delete
  chat history or change keys without separate operator authorization.

## Final gate

After all violation sets are empty and every exceptional row is explained:

1. rerun all tests on SQLite and PostgreSQL 17.10;
2. rerun the fresh PostgreSQL `base -> head -> base -> head` migration chain;
3. verify production head/drift and the pre-migration backup listing;
4. update every remaining Phase 2 checkbox and the review conclusion;
5. run `compileall`, `pip check`, `git diff --check`, and secret-path review;
6. commit the complete Phase 2 implementation and documentation;
7. only then begin Phase 3a contract/hash work.
