# Phase 6 design: three-account collection HA and silent response standby

## Operator-selected topology

Phase 6 uses three QQ bot accounts:

1. the existing Command account remains the normal command and deterministic
   tool speaker;
2. the existing Talk account remains the normal conversation speaker;
3. a third Reserve account stays online and receives protected group traffic
   continuously, but is silent unless Core grants it a bounded failover role
   lease for an unavailable primary.

The primary availability objective is continuity of message collection. Fast
response failover is secondary to avoiding duplicate or split-brain replies.

This gives the two planes different topologies:

```text
collection plane:  Command + Talk + Reserve = active / active / active
response plane:    Command role primary + Talk role primary + Reserve passive
```

“Silent” means no ordinary group reply, command response, proactive message,
or model chat. The Reserve account may report health and observations to Core.
Administrator incident notification uses a separately reviewed private
channel and does not make Reserve a third group personality.

## What can and cannot be guaranteed

The system can target **no loss after at least one adapter has received an
event and durably appended it to its ingress spool**. It cannot prove that QQ
delivered every platform event to any account, and it cannot recover a message
that the platform delivered to none of the three accounts.

The initial guarantee applies to protected QQ group conversations where all
three accounts are members and receive events. A private message addressed to
one bot is not redundantly visible to the other accounts. Administrator
private-channel continuity requires a separate redundant platform/channel,
such as the later Telegram administration path.

Availability claims therefore use explicit terms:

- **adapter-received:** one account runtime observed the platform event;
- **durably captured:** an adapter-received event is in a local acknowledged
  spool segment;
- **Core-committed:** Core accepted an idempotent observation into PostgreSQL;
- **canonically covered:** at least one committed observation represents the
  source event;
- **redundantly covered:** two or three independent accounts committed an
  observation for the same canonical event;
- **reconciled:** local spools have no unacknowledged event before the stated
  watermark and any supported history check has completed.

“No omission” in an acceptance report must name which of these boundaries it
proved. Message counts alone are insufficient.

## Why a third account alone is not enough

The current Phase 2 bridges use bounded in-memory queues. This preserves bot
responsiveness during Core failure, but telemetry may be dropped after queue
or process loss. Adding a third account with the same queue only creates three
best-effort copies; it does not create durable collection.

The current claim protocol also assumes two responding instances:

- claim readiness defaults to two observations;
- an enforced allow waits for deny from every other observed instance;
- Phase 2 audit asserts that a v3 source has at most two observations and that
  a two-observation source is exactly Lily plus Nekro;
- canonical decisions snapshot a concrete `target_instance_id`, not a logical
  role plus current role holder.

If a passive Reserve simply reports a third observation, it becomes an
observed peer with no deny claim and causes legitimate allows to abstain. Phase
6 must distinguish collection observers from eligible response participants
before the third account is connected to production Core.

## Availability priorities

The system resolves competing goals in this order:

1. preserve every adapter-received message durably;
2. avoid two bot accounts responding to one source event;
3. preserve command and talk availability through a confirmed failure;
4. minimize failover and recovery latency;
5. preserve the exact personality/account that normally speaks.

The Reserve account cannot impersonate the unavailable QQ account. A failover
response is visibly authored by Reserve and records which logical role it held.

## Protected conversation inventory

High availability is configured per canonical conversation, not globally. A
protected-conversation record contains:

- platform, conversation type, and canonical conversation ID;
- required collector instance/account IDs;
- minimum healthy and minimum durable collector counts;
- expected membership and relevant platform permissions;
- ingress retention and maximum reconciliation lag;
- whether Command, Talk, or both roles may fail over there;
- operator owner, maintenance policy, and activation history.

Core reports `full`, `degraded`, `single_collector`, `uncovered`, or `unknown`
coverage. Reserve joining a group does not silently make it protected; the
operator must verify membership and one controlled event from all expected
collectors.

## Durable ingress spool

### Local write path

Each adapter owns a small append-only local spool outside the bot framework's
plugin database. The event callback:

1. normalizes and sanitizes the observation envelope;
2. assigns the stable per-instance idempotency/source key;
3. appends a checksummed length-delimited record to the current segment;
4. follows the configured durability policy before declaring local capture;
5. schedules asynchronous Core delivery and immediately returns to the bot
   framework.

The strongest profile fsyncs each record before local acknowledgement. A
batched fsync profile must publish its maximum unflushed interval as nonzero
RPO and is not described as zero-loss.

Spool records contain no account login credential, Core admin token, model
secret, or unnecessary raw payload. Raw remains disabled by default. Files use
restricted ownership, bounded record/segment sizes, checksums, and an explicit
retention quota.

### Delivery and compaction

The bridge sends oldest unacknowledged records first with existing instance
authentication and idempotency. A Core 200/201 response durably marks the
record acknowledged. Segments compact only after every record is acknowledged
and the retention floor has passed.

Retries use bounded exponential backoff and do not block platform callbacks.
Process restart resumes from the local acknowledgement index. Duplicate
delivery is normal and Core remains the idempotency authority.

Disk pressure is a critical coverage incident. The spool never silently drops
oldest data to appear healthy. Policy may stop lower-value diagnostic capture
or place the bot into a visible degraded state, but message observation loss
must be counted and alerted.

### Corruption and recovery

Segment checksum failure isolates the corrupt tail, retains forensic metadata,
and continues in a new segment only under an explicit policy. Recovery tooling
can list, verify, replay, quarantine, and export records without printing
secrets. An operator can prove `local records = acknowledged + pending +
quarantined`; unclassified disappearance is a failed acceptance gate.

### Core and PostgreSQL outage

Collectors keep appending locally while Core or PostgreSQL is unavailable.
When Core returns, they replay independently. This provides collection
continuity across bounded Core downtime without running multiple active Core
leaders.

PostgreSQL backup/replication remains a separate database-availability layer.
Local spools protect the uncommitted ingress interval; they are not a permanent
replacement for database backups, WAL archiving, restore rehearsal, or later
read availability.

## Coverage and gap detection

Core derives per protected conversation and time bucket:

- fresh expected collectors and their connection/heartbeat state;
- source events with one, two, or three distinct observations;
- each collector's observed/committed watermark and latest platform time;
- oldest local unacknowledged spool age and record count;
- replay and reconciliation lag;
- native identity conflicts and uncorrelated observations;
- confirmed test-marker gaps and best-effort history-reconciliation findings.

Native `real_seq` remains the validated QQ group cross-account identity, not a
promise that every integer is contiguous. A numeric jump alone is not declared
a lost message. Gap evidence comes from disagreement among collectors,
controlled marker sequences, adapter reconnect boundaries, and supported
platform history reconciliation.

History retrieval after reconnect is defense in depth. It uses a bounded
watermark window, records the API/account used, enters through the same
idempotent ingest path, and never overrides a live observation. Because
platform history completeness is not guaranteed by the Core contract, it is
not the sole correctness mechanism.

Recommended metrics include:

- `protected_conversation_collectors_fresh`;
- `canonical_sources_by_observation_redundancy`;
- `ingress_spool_pending_records` and `oldest_pending_seconds`;
- `ingress_spool_bytes/quota_bytes`;
- `ingress_replay_rate` and `reconciliation_lag_seconds`;
- `known_gap_events`, `correlation_conflicts`, and `quarantined_records`.

## Logical roles and instance selection

Phase 6 separates a logical response role from the physical bot instance:

- logical roles: `command`, `talk`, and optional `incident_notifier`;
- physical instances: current Command, current Talk, and Reserve;
- instance role capabilities: which roles an instance is reviewed and healthy
  enough to hold, with reduced limits and forbidden operations;
- role assignment: the normal primary holder;
- role lease: the current bounded holder for an exact scope and epoch.

Canonical policy first decides `target_role`. Core then resolves and snapshots
`selected_instance_id` from the active role lease. For backward compatibility,
the existing `target_instance_id` remains the selected-instance snapshot until
all consumers use the explicit role fields.

Reserve may be reviewed for both roles, but the first release allows it to hold
at most one response role at a time. If both primaries are unavailable, the
default is a reduced administrator-visible state rather than silently running
two personalities through one account. Expanding that behavior requires a
separate capacity and policy canary.

## Claim coordination with a passive observer

Collection participants and response participants are different sets:

- `observer_instance_ids`: every instance that reported the source;
- `eligible_responder_instance_ids`: instances allowed to speak for the target
  role under current policy and role-lease epoch;
- `coordination_peer_instance_ids`: eligible responders that must be denied or
  fenced before the selected holder may send.

Passive Reserve observations improve correlation and coverage but do not
create a deny requirement. Observation readiness remains a configurable
evidence threshold; it is not changed to “all three accounts must observe,”
because the point of redundancy is to tolerate a missing collector.

When Reserve holds no role lease it never receives an enforced allow, never
runs command/talk matchers, and never sends. When it takes a role, the failed
primary is excluded only after its previous role epoch has been revoked or
expired; its stale claims and sends are rejected by fence.

The Phase 2 audit's two-instance invariants remain correct for the current
deployment. Phase 6 replaces them with configurable expected-collector and
eligible-responder invariants in the same release that first admits Reserve
observations.

## Two-plane failure policy

### Ingress remains fail-open and durable

Failure to reach Core never blocks the platform receive loop. Events append to
the local spool and replay later. A collector can remain useful even when it
cannot participate in response coordination.

### Egress becomes lease-required when automatic failover is enabled

The current Phase 2 response path fails open when Core is unavailable. That is
appropriate before automatic failover, but it cannot safely coexist with a
Reserve account: during a network partition, the primary could fail open and
speak while Core grants Reserve a failover allow.

Once Phase 6 automatic response failover is enabled, every managed response
requires a fresh role lease epoch and fence. If an instance cannot validate or
renew its lease, its response send path fails closed after the lease expires.
Collection continues unaffected.

This is an explicit availability tradeoff:

- Core/coordination outage: all accounts keep collecting and spooling, but
  managed replies may pause;
- confirmed primary/account failure while Core is healthy: Reserve may acquire
  the role after expiry/revocation and resume replies;
- network partition: a stale primary cannot continue indefinitely and create
  split-brain output.

Lease duration and renewal thresholds are chosen from measured latency and
outage tests. They are not hard-coded in this design.

## Incident and failover state machine

Reserve role state is scoped by role and protected conversation set:

```text
standby
  -> candidate       failure evidence satisfies policy
  -> arming          primary epoch revoked/expiring; readiness checked
  -> active          bounded role lease and fence issued
  -> draining        stop new work; settle in-flight attempts/deliveries
  -> recovering      primary healthy through cooldown/hysteresis
  -> standby         primary receives a newer role epoch

candidate/arming/active -> aborted or incident_manual_review
```

The incident records evidence, policy version, operator actions, role epochs,
leases, invocations, delivery receipts, notifications, and recovery outcome.

### Evidence that may trigger automatic failover

- primary account/runtime is disconnected or offline beyond the reviewed
  threshold;
- required platform-send capability is absent/stale;
- a send attempt returned a definitive platform rejection classified as safe
  to retry through another account;
- the primary role lease expired and health checks confirm Reserve readiness.

### Evidence that does not immediately trigger another send

- platform send timeout or connection loss with unknown completion;
- a model or tool provider failed while the bot account remains healthy;
- one missed heartbeat below the hysteresis threshold;
- a message received by fewer than three collectors;
- Core/PostgreSQL outage, because no safe new role authority can be issued.

Unknown send completion enters reconciliation/manual review. It is not retried
from Reserve merely to improve apparent availability.

## Delivery idempotency and visible failover

Phase 4 delivery intents and receipts are the authority for response failover.
The intent binds source event, logical role, selected instance, role epoch,
fence, rendered payload hash, target conversation, and expiry.

Reserve creates a new delivery attempt under the same logical response only
when policy proves the prior attempt did not complete or explicitly authorizes
a replacement. The audit retains both attempts and explains the visible
account change.

Replies to a Reserve-authored message resolve to the physical Reserve instance
and the logical role it held at send time. Recovery does not rewrite history
to pretend the primary sent it.

## Failure domains and deployment topology

A third account on the same host and network protects against one account or
NapCat/runtime failure, but not host power, kernel, disk, LAN, or upstream
network failure. Real collection HA progressively separates:

1. account credentials and platform sessions;
2. NapCat containers/processes and writable session directories;
3. bridge/runtime processes and spool directories;
4. storage devices where practical;
5. physical host and network/power path for the Reserve collector.

Reserve must not share a writable QQ session directory, token, spool, or
idempotency namespace with either primary. Its Core ingest token is independent
and cannot impersonate another instance. Moving Reserve to another host is the
step that protects against full-machine failure; before then the documented HA
scope is narrower.

Provider redundancy is separate from account redundancy. If Talk's model or
Command's tool provider is the failed component, using another QQ account with
the same failed provider does not restore the capability. Phase 3 provider
health/leases and Phase 5 model routing define those fallbacks.

## Data model slices

Logical persistence additions, numbered only when Phase 6 begins:

- protected conversations and expected collectors;
- instance role capabilities and primary assignments;
- local-spool receipt/watermark summaries and coverage intervals;
- incidents, evidence, transitions, and notifications;
- role leases/epochs/fencing transitions;
- failover delivery linkage and recovery records.

High-volume local spool records remain local until ingested; Core does not copy
the same observation into a second queue table after it is committed. Coverage
summaries are derived/rebuildable where possible, while incidents and lease
transitions are immutable audit authority.

## Recommended implementation packets

### HA-0: collection durability foundation

- Implement and fault-test the durable bridge spool with the existing two
  accounts.
- Add Core receipt/watermark and coverage diagnostics.
- Prove Core/PostgreSQL outage replay, bridge restart replay, duplicate ingest,
  disk quota, corrupt tail, and recovery tooling.
- Do not add the third account or alter response claims.

This packet is authority-neutral and may be pulled forward after Phase 2 if
collection continuity is prioritized over new features. Pulling it forward is
a roadmap decision; it does not happen implicitly from this design.

### 6a: role and incident shadow

- Add logical role/selected-instance decisions, expected collector sets,
  incident policy, and role lease models in shadow.
- Keep current two-account response behavior.
- Simulate third-observer claim sets to prove passive observations do not block
  normal allows.

### 6b: silent Reserve collector

- Deploy the third account with an independent NapCat/session/token/spool.
- Join only explicit protected test conversations first.
- Enable event/heartbeat/coverage reporting; all send and response matchers
  remain disabled.
- Run a stable collection window and controlled numbered-marker tests.

### 6c: failover simulation

- Exercise candidate/arming/active/draining/recovery with no platform sends.
- Inject account, process, network, Core, database, provider, and unknown-send
  failures.
- Verify lease expiry/fencing and no dual role holders.

### 6d: exact-conversation response canary

- Permit Reserve to hold one logical role in one otherwise empty test group.
- Stop the primary in a controlled way, wait for confirmed lease transition,
  and send one idempotent test action.
- Test Command and Talk takeover separately; do not test simultaneous takeover
  in the first release.
- Recover the primary through drain/cooldown and verify no duplicate response.

### 6e: protected-group rollout

- Add conversations one at a time after membership and coverage proof.
- Keep a kill switch that disables Reserve egress while preserving collection.
- Extend stable windows only after every exception is explained.

## Acceptance gates

### Collection continuity

- All protected conversations have the configured minimum fresh collectors.
- Controlled numbered messages survive Core outage, each bridge restart, one
  primary account outage, and Reserve-host/network tests within the claimed
  failure scope.
- Every locally durable record becomes committed, pending, or quarantined;
  none disappears silently.
- Replay creates no duplicate observation or source event.
- Observation redundancy, spool lag/quota, known gaps, and correlation
  conflicts are visible and alertable.
- The report distinguishes group coverage from non-redundant private messages.

### Response safety

- Reserve produces no ordinary output without an active role lease.
- At most one unexpired holder exists per logical role/scope/epoch.
- Stale primary and stale Reserve sends are rejected by fence.
- Unknown platform completion never causes an automatic duplicate send.
- Controlled primary failure produces one response from Reserve within the
  measured recovery objective; recovery produces no second response.
- Core/coordination failure preserves collection but pauses managed egress
  after lease expiry.

### Isolation and operations

- Independent credentials, session directories, spools, and idempotency
  namespaces are verified without exposing secrets.
- Same-host and separate-host failure claims are reported separately.
- Maintenance, alert suppression, manual override, and Reserve-egress kill
  switch are rehearsed.
- A full incident timeline explains detection, lease change, response attempts,
  recovery, and any lost availability.

## Scheduling decision

The current numbered roadmap places full Watchdog/failover in Phase 6. Because
the operator's highest priority is message collection continuity, `HA-0`
durable ingress may reasonably move ahead of Renderer and natural-language
work without enabling new response authority. The third account and automatic
egress failover still wait for the role-lease/fencing and delivery boundaries.

The roadmap has now made that explicit priority choice: the durable spool and
coverage subset of `HA-0` moves into the authority-neutral C0-D collection
packet after Phase 3a and before Phase 3b. This does not authorize the third
account, role failover, egress leases, or any running-service change by itself;
those remain under the Phase 6 gates in this document.
