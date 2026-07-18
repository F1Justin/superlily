# C0-D collection reliability acceptance

This document records the signed release gate for the authority-neutral C0-D
packet. It deliberately separates implementation, production rollout and
controlled fault evidence. All C0-D1 through C0-D5 gates passed on 2026-07-18;
the C0-D prerequisite no longer gates Phase 3b.

## Current implementation snapshot

As of 2026-07-18 21:39 CST, C0-D1 through C0-D5 are deployed and accepted in
production. Core is at `0013_collection_reliability`; Lily and Nekro run bridge
`0.5.0` with independent durable spools and real action capture. The controlled
Core/PostgreSQL faults and post-rollout behavior checks passed without changing
claim or Tool Registry authority.

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
- identical Lily/Nekro normalization of the locally observed NapCat
  `group_msg_emoji_like`, group/friend recall and poke payloads: reactions keep
  actor, local message target, emoji and count as `observed_state`; recall keeps
  operator and author separate; poke keeps actor, target and bounded display
  facts while omitting URL/internal-UID fields with explicit capture evidence;
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

C0-D2/D3/D4/D5 have production evidence. C0-D is signed complete.

## Verification evidence

The current workspace passes:

- the complete SQLite suite: `244 passed`;
- the complete PostgreSQL 17 suite against the disposable
  `superlily_test` database: `244 passed`;
- fresh Alembic upgrade to `0013_collection_reliability`, schema drift check,
  downgrade to `0012_tool_registry`, and re-upgrade to head;
- targeted Core-offline restart replay, strict pending order, corrupt database
  evidence preservation, receipt mismatch, out-of-order gap closure, sequence
  collision, late action target resolution, exact capture-policy snapshot,
  claim-path receipt, real Core API delivery, real NapCat action fixtures,
  missing actor/target/value observations, cross-bridge identity parity and
  two-instance action persistence tests.

Production rollout evidence:

- implementation commits `9dd2b23` and `06ffcc1` are the deployed bridge
  source; `06ffcc1` also enforces mode `0600` on SQLite, WAL and SHM files;
- the pre-migration PostgreSQL custom-format backup is
  `/home/justin/backups/superlily/20260718-c0d/superlily-pre-c0d-20260718T115705Z.dump`,
  139 MiB, SHA-256
  `685521e8b28d9903bd5d26a2307f7cae67d669e95f34b83925321308ef2ba872`;
  `pg_restore -l` validates its catalog. The backup directory is `0700`, all
  three recovery artifacts are `0600`; redundant container and `/tmp` copies
  were removed only after the durable copies were verified;
- production startup applied `0012_tool_registry ->
  0013_collection_reliability`; `alembic current` reports head and `alembic
  check` reports no drift;
- Lily restarted at 20:06 CST and Nekro at 20:07 CST. Both bridge logs report
  fail-open durable capture, both runtime health checks are green, PostgreSQL
  and NapCat were not restarted;
- both spool directories are mode `0700`; database, WAL and SHM files are mode
  `0600`. At the 20:08:21 snapshot, Lily sequences 1–62 and Nekro sequences
  1–5 were all distinct and contiguous in Core, with no invalid
  capture-before-commit ordering;
- at the 20:15 final check, authenticated ingress diagnostics reported Lily
  local/Core sequence 231/231 and Nekro 21/21: both collectors online,
  healthy, reconciled, zero pending, zero capture/replay failures and zero
  quarantine;
- commit `d8ed047` deployed bridge 0.5.0 at 20:59 CST (Lily) and 21:00 CST
  (Nekro). NapCat, Core and PostgreSQL retained their prior start times. Both
  bridge packages loaded cleanly, reconnected to OneBot and reported online,
  healthy heartbeats with zero pending/capture failure/quarantine;
- the pre-C0-D4 bridge 0.4.0 sources are archived under
  `/home/justin/backups/superlily/20260718-c0d4` (directory `0700`, files
  `0600`). Lily's tar SHA-256 is
  `94e65adff3aa26f01ffa64c3fe91dd0502302f83499da3a94a87353bcc20b4ea`;
  Nekro's is
  `8d9831e6018a43b5505ce9a73855ba7566ffe83fb3440062108071b2d819fc04`;
- at 21:01:24 CST, both accounts observed the same real poke. Their distinct
  observations carry the same bridge-reported `qq:action:v1` identity and the
  same factual value (`action_id=8`, display text `揉了揉的小猫`), but remain
  separately bound to `lily-command` and `nekro-agent`. Each action is
  `complete`; the intentionally discarded `raw_info` jump/image URLs and
  internal UIDs appear in `omitted_fields`; both numbered spool records reached
  contiguous Core watermarks;
- at the 21:09 rollout snapshot, Lily/Core watermark is 1097/1097 and Nekro/Core
  is 124/124. Production has 14 Lily poke rows, one Lily recall row and one
  Nekro poke row; all action records have receipt ordering
  `captured_at <= received_at <= committed_at`. The initial short canary had no
  natural reaction, so its first reaction evidence came from fixtures copied
  from the local NapCat log rather than a synthesized production event;
- from 21:14:48 through 21:15:32 CST, group `708309706` then produced 14 natural
  `group_msg_emoji_like` observations against platform message `391219067`.
  Thirteen came from one member and one from the Lily command account; all 14
  preserved the platform emoji ID and count, have numbered receipts, and are
  `complete`. The target remains honestly `unresolved` in Core because the
  11:25 target message predates its captured history; the local target ID was
  not guessed or cross-account merged;
- no action observation entered the claim table. Production still has zero
  descriptors, providers, Provider credentials, inventory entries or capture
  profile overrides. Provider token count remains zero and Registry execution
  remains hard-coded off;
- Lily's cumulative spool metric contains one recovered replay retry for
  sequence 520. It was captured at 20:36:07 and committed at 20:36:11, before
  the 0.5.0 restart; current `last_error` is empty and the spool has no pending
  or gap. It is retained as honest transient-retry evidence rather than reset;
- Tool Registry remains `execution.mode=off` with zero descriptors, providers,
  active descriptors and eligible tools. No C0-A archive profile or tool
  authority was enabled;
- the controlled Core outage began at 21:35:15 CST from a clean baseline of
  Lily/Core `1500/1500` and Nekro/Core `199/199`. While Core was unavailable,
  Lily retained sequences `1508-1522` and Nekro retained `200-201`; the first
  record on each bridge accumulated real connection failures while every later
  record remained durably pending in strict sequence. Core was ready again at
  21:36:15. All 17 records received matching Core receipts with byte-identical
  stored hashes, both spools returned to zero pending, and the 21:36:48
  watermarks were contiguous at Lily `1534/1534` and Nekro `201/201`;
- the controlled PostgreSQL outage began at 21:37:36 CST from a clean baseline
  of Lily/Core `1540/1540` and Nekro/Core `201/201`. Core stayed live while
  `/health/ready` correctly returned `503 database unavailable`. Lily retained
  sequences `1544-1545` and Nekro retained `204`; the first record on each
  bridge recorded the real Core HTTP 500 and later work remained ordered.
  PostgreSQL restarted at 21:38:21 and Core was ready at 21:38:28. All three
  records received matching receipts with their original hashes, both spools
  returned to zero pending, and the 21:39:08 watermarks were contiguous at
  Lily `1558/1558` and Nekro `208/208`;
- the fault drills intentionally increased cumulative `replay_failures` to 15
  on Lily and 14 on Nekro. These counters are retained as honest evidence;
  current `last_error` is empty and no test record is pending or quarantined;
- the explicit post-rollout behavior pair is independently attributable in
  Core. At 21:31:51, `今日老婆` received a 95-confidence
  `command_exact:今日老婆` decision for `lily-command` and a successful response
  at 21:32:00. At 21:31:49, `莉莉 这是刘维尔定理吗` was observed by both accounts,
  received a 90-confidence `summons_talk_bot` decision for `nekro-agent`, and
  produced successful image and text responses at 21:33:17-21:33:18;
- the 21:52 final check found Core and PostgreSQL healthy, both bot instances
  online, Alembic at `0013_collection_reliability` with no pending upgrade,
  Lily local/Core at `1672/1672` and Nekro at `223/223`, zero pending,
  capture failure, quota rejection or quarantine, and an empty current
  `last_error`. Registry descriptors, providers, credentials, inventories and
  heartbeats remain zero; capture-profile overrides and action-linked claims
  also remain zero.

The operator authorized the production fault drills and C0 close-out. Core and
PostgreSQL were restored healthy after each bounded outage; Lily, Nekro and
NapCat were not restarted during C0-D5.

## Signed release gates

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

- [x] Lily maps supported OneBot/NapCat reaction, recall and poke notices into
  the shared action contract.
- [x] Nekro maps the same observable facts without assigning feedback meaning.
- [x] Both paths have fixtures for missing actor/target/value fields and for
  ambiguous or unavailable target observations.

### C0-D5: end-to-end faults and rollout

- [x] Controlled Core outage loses no numbered records.
- [x] Controlled PostgreSQL outage loses no numbered records.
- [x] Bridge crash/restart, duplicate replay, out-of-order delivery, corrupt
  spool tail and full-quota behavior have deterministic evidence.
- [x] Images remain placeholders/metadata rather than PostgreSQL byte payloads.
- [x] Claim traffic and Tool Registry authority are unchanged after rollout.
- [x] An explicit post-rollout command/Nekro-reply pair confirms both behavior
  paths beyond process and adapter health.
- [x] Production migration, canary observation and rollback evidence are
  recorded before C0-D is signed complete.

## Explicitly outside this gate

Nested merged-forward expansion, formal `archive_full` activation, portable
exports, reconstruction, retention/deletion propagation and old-history import
belong to C0-A. Once all C0-D gates above pass, those archival capabilities may
proceed independently of Phase 3b and cannot block the invocation ledger on
rare nested-forward cases.

## Sign-off

C0-D was signed complete at 2026-07-18 21:39 CST. Phase 3b may begin with the
shared Provider SDK and separately reviewed `status.inspect` authority. C0-A
may proceed independently and does not reopen this reliability gate unless a
regression violates one of the signed invariants above.
