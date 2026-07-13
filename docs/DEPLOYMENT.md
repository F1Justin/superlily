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
secret is required. Claim settings default to a fully disabled state:

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
2. Confirm a fresh `/v1/command-registry/runtime` snapshot and review every
   uncovered trigger. Uncovered triggers may remain, but they must force
   abstention rather than enforcement.
3. Run `/v1/decisions/outcomes` and controlled reply/command cases in shadow.
4. Set Core to `shadow`, enable claim requests on both bridges, and verify
   `/v1/claims/summary` records decisions with zero enforced rows.
5. Set one exact `qq:group:<id>` key in the canary JSON and switch Core to
   `canary`; every other conversation remains fail-open.
6. Verify a Lily command, explicit Nekro summon, reply to each bot, ordinary
   message, and simulated Core outage. Only the non-target response path may be
   suppressed; ordinary messages and outages retain existing behavior.
7. Roll back by setting both bridge claim flags false or Core mode `off`. No
   token or database rollback is required.

## 5. Rollback

- Lily: remove `plugins.lily_core_bridge` from the explicit plugin list and
  restart Lily.
- Nekro: disable/remove `Superlily.core_bridge`, remove the bus override, and
  restart Nekro.
- Core: stop its Compose project. Neither bot depends on it for responses.
