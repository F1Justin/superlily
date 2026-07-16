# Deployment

## 1. Core

Copy `.env.example` to `.env`, replace every placeholder with an independently
generated secret, then validate and start:

```bash
sudo docker compose --env-file .env -f deploy/compose.yml config
sudo docker compose --env-file .env -f deploy/compose.yml up -d --build
curl http://127.0.0.1:8765/health/live
curl http://127.0.0.1:8765/health/ready
```

The production Python base is pinned by image digest and runtime/build
dependencies are constrained by `deploy/constraints.txt`. Update that file
only together with the complete SQLite/PostgreSQL suite and a rebuilt-image
`pip check`; broad ranges in `pyproject.toml` remain the library compatibility
contract, not the production resolver input.
Test-only packages are independently pinned in
`deploy/test-constraints.txt`.

The PostgreSQL 17 image is also digest-pinned (currently PostgreSQL 17.10).
Update that digest deliberately, verify the release/migration notes and backup,
then re-run the PostgreSQL suite and restore check. Core has a Compose health
check backed by `/health/ready`; `running` without `healthy` is not deployment
success.

The Compose project creates `superlily_bus` and publishes Core only on host
loopback.

Command shadow decisions read `apps/core/config/command_registry.toml` by
default. Set `SUPERLILY_COMMAND_REGISTRY_PATH` only when you intentionally want
Core to read another registry file. A bad registry must not affect Lily/Nekro
message handling; Core degrades decision metadata instead of becoming a control
plane in Phase 2b.

Runtime registry snapshots use the existing Lily ingest token; no additional
secret is required. Group policy is explicit and models target availability:

- `command_only`: Lily commands are available; Nekro conversation is disabled;
- `conversation_only`: Nekro conversation is enabled; Lily is not in the group;
- `full`: both Lily commands and Nekro conversation are enabled; and
- `observe_only`: neither target is enabled, though collection may continue.

Groups default to `command_only`. Private conversations remain `full` because
this switch is group-scoped.

```dotenv
SUPERLILY_GROUP_DEFAULT_MODE=command_only
SUPERLILY_GROUP_MODES_JSON={"qq:group:708309706":"full","qq:group:1085969238":"conversation_only"}
```

Changing Lily membership, Nekro channel `is_active`, and this map are one
operational change. Determine the mode from the intersection of Lily's live
`get_group_list` and Nekro's channel activation state, not from recent message
traffic: observation proves collection, not permission to converse. Decision
features record the effective mode for audit. Claim settings default to a
fully disabled state:

```dotenv
SUPERLILY_CLAIM_MODE=off
SUPERLILY_CLAIM_CANARY_CONVERSATIONS_JSON=[]
SUPERLILY_CLAIM_MINIMUM_CONFIDENCE=85
SUPERLILY_CLAIM_REQUIRED_OBSERVATIONS=2
SUPERLILY_CLAIM_COALESCE_MILLISECONDS=200
```

## 2. Lily bridge

The existing Lily process runs in the `nb` tmux session managed by the enabled
user unit `tmux-nb.service`. Capture the current tmux log and one known-good
command response before changing it.

Do not treat `Ctrl-C` inside tmux as a permanent stop: ending the inner process
causes the session to exit and the systemd unit automatically creates a new
`nb` session. When the bridge is ready, follow
`bridges/lily_nonebot/README.md`, perform one controlled restart through the
existing supervisor, and watch both the unit and the new tmux pane until Lily
is healthy again. Core failure must not change command behavior.

Keep the default timeout split unless a measured deployment justifies a
change: claim calls are bounded at three seconds and fail open, while background
event/response ingestion uses five seconds and three bounded idempotent attempts
for transient transport, 429, and 5xx failures. They must not be collapsed back
to one sub-second deadline; a request may already be durably committed when the
bridge stops waiting.

When bridge claims are enabled, one incoming message first uses
`POST /v1/claims/evaluate`, which also ingests the event. It does not enqueue a
second normal event request after a successful claim response. If claim
evaluation fails or times out, the bridge enqueues the normal event report and
continues the legacy path. An enforced Lily deny is installed in its
event-scoped outbound guard and then acknowledged through
`POST /v1/claims/{claim_id}/ack`; acknowledgement failure does not undo local
suppression, but it prevents Core from granting a peer an exclusive allow.
Nekro returns `BLOCK_TRIGGER` and installs an exact-source OneBot outbound API
guard for both the active event and a later scheduler task. It records any
same-event send attempted before its callback and acknowledges only when the
event matches, no prior send exists, and the guard is installed. Missing
context, prior output, or ACK failure prevents an exclusive peer allow.

## 3. Nekro bridge

Pin `kromiose/nekro-agent` to the currently validated digest before adding the
bridge:

```text
kromiose/nekro-agent@sha256:88193fa55c4501d3378f5511430bcf32071597d24b880762e65087f66fbf264b
```

Copy the plugin as described in `bridges/nekro/README.md`, join
`superlily_bus` with the provided Compose override, and restart Nekro once.

## 4. Phase 2c canary sequence

Do not jump directly from shadow to enforcement.

1. Deploy Core and both bridge versions with bridge claims disabled.
   Back up PostgreSQL, apply `0011_claim_ack`, and verify `alembic current` and
   `alembic check` before enabling claims.
2. Confirm a fresh `/v1/command-registry/runtime` snapshot and review every
   uncovered trigger. Uncovered triggers may remain, but they must force
   abstention rather than enforcement.
3. Run `/v1/decisions/outcomes` and controlled reply/command cases in shadow.
4. Set Core to `shadow`, enable claim requests on both bridges, and verify
   `/v1/claims/summary` records decisions with zero enforced rows.
5. Set one exact `qq:group:<id>` key in the canary JSON and switch Core to
   `canary`; every other conversation remains fail-open.
6. In test group `708309706`, verify a Lily command, explicit Nekro summon,
   reply to each bot with and without QQ's decorative `at`, reply to another
   user with/without a summon, leading other-user `at`, leading image/non-text,
   two close Nekro triggers across scheduler tasks, and ordinary messages.
   Separately verify private Lily/Nekro recipient routing.
7. Fault-inject a lost/late deny response, claim-ack failure, Core outage, and
   send timeout. A target cannot gain an exclusive allow without the peer's
   persisted acknowledgement. A send timeout is recorded as
   `completion_status=ambiguous` and is not retried blindly.
8. Record code/image hashes, process starts, instance state, registry hash, and
   reporter counters only after these tests pass. That timestamp starts a new
   uninterrupted 24-hour policy-v5 acceptance window; no pre-deployment hour
   counts.
9. Run `docs/phase2_final_audit.sql` with explicit `window_start` and
   `window_end`, review every exception, and sign `docs/ACCEPTANCE.md` before
   beginning Phase 3.
10. Roll back by setting both bridge claim flags false or Core mode `off`. No
   token or database rollback is required.

## 5. Rollback

- Lily: remove `plugins.lily_core_bridge` from the explicit plugin list and
  restart Lily.
- Nekro: disable/remove `Superlily.core_bridge`, remove the bus override, and
  restart Nekro.
- Core: stop its Compose project. Neither bot depends on it for responses.

## 6. Phase 3 deployment boundary

Phase 3 has not started. After the Phase 2 signature, follow
`PHASE3_ACCEPTANCE.md` and `PHASE3_TOOL_REGISTRY.md`. Deploy descriptor/runtime
registry work with zero active descriptors and execution `off`; the future
control panel described in `CONTROL_PLANE.md` is read-only until its own
authentication, authorization, preview, audit, and mutation gates pass.
