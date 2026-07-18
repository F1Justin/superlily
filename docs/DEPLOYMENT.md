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

Phase 3a Provider credentials are a third, unrelated token class. The first
schema deployment kept this map empty, which made both Provider write endpoints
return 401 and left the Registry with zero reported providers. The reviewed
`status.inspect` rollout adds exactly one independently generated mapping and
passes the same value only to the status-provider process:

```dotenv
SUPERLILY_PROVIDER_TOKENS_JSON={"provider-status-primary":"independent-random-token"}
SUPERLILY_STATUS_PROVIDER_TOKEN=independent-random-token
SUPERLILY_PROVIDER_INVENTORY_STALE_SECONDS=600
SUPERLILY_PROVIDER_HEARTBEAT_STALE_SECONDS=90
SUPERLILY_STATUS_PROVIDER_HEARTBEAT_SECONDS=30
SUPERLILY_STATUS_PROVIDER_INVENTORY_SECONDS=300
```

When a reviewed Provider is introduced later, generate a new token that is not
equal to any admin or bot-ingest token, add only its provider-ID mapping, and
create the stable registration through the local
`superlily-tool-registry-admin` command. Do not place Provider tokens in a
descriptor, inventory, heartbeat metadata, logs, browser storage, or exported
evidence.

After the authority commit exists, obtain its canonical hash and use the local
administration CLI against that exact full commit. Both commands create
reviewed/registered state only; neither can activate or execute the tool:

```bash
commit=$(git rev-parse HEAD)
bundle_hash=$(
  .venv/bin/superlily-tool-registry verify-descriptor \
    registry/descriptors/status.inspect/1.0.0.json \
  | python -c 'import json,sys; print(json.load(sys.stdin)["descriptor_hash"])'
)
.venv/bin/superlily-tool-registry-admin import-descriptor \
  registry/descriptors/status.inspect/1.0.0.json \
  --repository . \
  --source-commit "$commit" \
  --bundle-hash "$bundle_hash" \
  --reviewer phase3-status-review
.venv/bin/superlily-tool-registry-admin register-provider \
  registry/providers/provider-status-primary.json \
  --actor phase3-status-review
```

The status-provider container has a read-only root filesystem, no Linux
capabilities, no inbound port and only the shared bus needed to report to Core.
In this slice it repeatedly runs a local structured self-test and publishes
inventory/heartbeat; it cannot accept a lease or send a QQ message. Its
expected effective-state reasons are `inactive_descriptor`,
`budget_unenforceable`, and `execution_off`.

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

Keep the default control/report policy unless a measured deployment justifies
a change: claim and ACK calls use a ten-second per-attempt deadline and two
bounded idempotent attempts, while background event/response ingestion uses a
ten-second deadline and three bounded idempotent attempts for transient
transport, 429, and 5xx failures. They must not be collapsed back to one
sub-second deadline; a request may already be durably committed when the bridge
stops waiting. Core does not commit inside PostgreSQL claim polling loops;
`READ COMMITTED` supplies a fresh snapshot per statement without multiplying
checkpoint fsync waits.

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
   user without a summon (two acknowledged denies and no allow), reply to
   another user with a summon (Nekro owns it), leading other-user `at`, leading
   image/non-text,
   two close Nekro triggers across scheduler tasks, and ordinary messages.
   Separately verify private Lily/Nekro recipient routing.
7. Fault-inject a lost/late deny response, claim-ack failure, Core outage, and
   send timeout. A target cannot gain an exclusive allow without the peer's
   persisted acknowledgement. A send timeout is recorded as
   `completion_status=ambiguous` and is not retried blindly.
8. Record code/image hashes, process starts, instance state, registry hash, and
   reporter counters after these tests pass. Reuse the completed policy-v5
   24-hour window for unchanged invariants and run `docs/policy_v6_backtest.sql`
   over its stored events; do not impose another fixed 24-hour delay for this
   Core-only policy delta.
9. Review post-deployment claim/ACK/response rows and failure counters, then
   sign `docs/ACCEPTANCE.md` before beginning Phase 3.
10. Roll back by setting both bridge claim flags false or Core mode `off`. No
   token or database rollback is required.

## 5. Rollback

- Lily: remove `plugins.lily_core_bridge` from the explicit plugin list and
  restart Lily.
- Nekro: disable/remove `Superlily.core_bridge`, remove the bus override, and
  restart Nekro.
- Core: stop its Compose project. Neither bot depends on it for responses.

## 6. Phase 3 deployment boundary

The first `0012_tool_registry` production deployment completed on 2026-07-18
after the Phase 2 signature with `SUPERLILY_PROVIDER_TOKENS_JSON={}`, no
descriptor, and execution `off`. The next authorized Phase 3a slice adds only
the reviewed real `status.inspect` authority, one stable Provider credential,
and its reporting-only runtime. Descriptor lifecycle remains `reviewed`, hard
wall-time remains honestly unsupported, and Core still has no invocation,
attempt, lease, execution, or natural-language route. Follow
`PHASE3_ACCEPTANCE.md` and `PHASE3_TOOL_REGISTRY.md`. The future control panel
described in `CONTROL_PLANE.md` remains read-only until its own authentication,
authorization, preview, audit, and mutation gates pass.

This reporting-only slice deployed on 2026-07-18 from commit
`c48aaa18e35d99ab6468a683329311586c7f1518`. The imported descriptor hash is
`65af3c28c09b250b3418269416841fa980fae9cfb8ffcb87c6df5305f6fbd62c`;
its lifecycle is `reviewed`. The expected admin state is one fresh healthy
Provider but zero active/eligible tools, with reasons `inactive_descriptor`,
`budget_unenforceable`, and `execution_off`. Roll back the runtime by stopping
only `status-provider` and removing its token mapping on the next Core recreate;
the immutable reviewed descriptor/Provider audit rows may remain. No schema
downgrade is involved.

The pre-rollout backup is
`/home/justin/backups/superlily/20260718-phase3a-status/superlily-pre-phase3a-status-c48aaa1.dump`
(mode `0600`, SHA-256
`763f2e33906040a3da3962406d62be6d7b7d448af8c7d09166a2f9e0909741b1`).
该报告切片上线时，生产 migration head 仍为
`0013_collection_reliability`。后续 Phase 3b 已按预定编号从
`0014_tool_invocations` 开始，见第 8 节。

## 7. C0-D durable ingress rollout and rollback

C0-D1 through C0-D3 deployed on 2026-07-18 with bridge `0.4.0`; C0-D4 then
deployed the same day with bridge `0.5.0`. Core migration head remains
`0013_collection_reliability`; Lily and Nekro own independent SQLite
`synchronous=FULL` spools:

- Lily: `/home/justin/lily/data/superlily-core/ingress-spool.sqlite3`;
- Nekro: `/home/justin/nekro/plugin_data/Superlily.core_bridge/ingress-spool.sqlite3`.

Each parent directory must be `0700`; database, `-wal`, and `-shm` files must
all be `0600`. A bridge appends before Core I/O, retains strict pending order,
and compacts only after an exact matching receipt. Never delete or replace a
spool merely to clear an alert. First preserve it, inspect pending/quarantine
state and reconcile its sequence range with `/v1/ingress/status` and
`/v1/ingress/watermarks`.

The durable recovery directory is
`/home/justin/backups/superlily/20260718-c0d` (mode `0700`). The pre-rollout
PostgreSQL backup is `superlily-pre-c0d-20260718T115705Z.dump` with SHA-256
`685521e8b28d9903bd5d26a2307f7cae67d669e95f34b83925321308ef2ba872`.
The old Lily package is archived at
`lily-core-bridge-pre-c0d-20260718T115705Z.tar` with SHA-256
`fc2eac8695c54d31c3c8d6975b1be472b355300b76c544adf72216c3296fad05`;
the old Nekro package is
`nekro-superlily-bridge-pre-c0d-20260718T115705Z.tar` with SHA-256
`949938cb7f8c74e64e02bc8691602a6d7ff87662815bd699581a99831740e4b8`.
All three files are mode `0600`.

The C0-D4 bridge-only rollback directory is
`/home/justin/backups/superlily/20260718-c0d4` (mode `0700`). Its 0.4.0 source
archives are `lily-core-bridge-0.4.0-d8ed047-parent.tar` (SHA-256
`94e65adff3aa26f01ffa64c3fe91dd0502302f83499da3a94a87353bcc20b4ea`)
and `nekro-superlily-bridge-0.4.0-d8ed047-parent.tar` (SHA-256
`8d9831e6018a43b5505ce9a73855ba7566ffe83fb3440062108071b2d819fc04`);
both are mode `0600`. Restore only the affected 0.4.0 package and restart that
bot to roll back action mapping; do not touch either durable spool.

Prefer a bridge-only rollback: preserve both spool directories, restore bridge
0.4.0 for C0-D4 (or 0.3.x for the earlier durable-ingress rollout), restart
only the affected bot, and leave Core at 0013 because both old wire contracts
remain compatible. If Core itself must return to 0012, first
stop new durable submissions or restore both old bridges, prove both spools
have zero pending records, take another database backup, and only then run the
0013 downgrade and rebuild Core from commit `4069d9d`. Downgrading destroys the
C0-D tables, so it is not the first response to a bridge incident.

OneBot reconnect backlog is not repaired by rewriting timestamps. Preserve
adapter `occurred_at`, bridge `captured_at`, Core `received_at`, and receipt
`committed_at` separately; use native message identity, idempotency key and
spool sequence for replay/order diagnostics.

C0-D5 was signed complete on 2026-07-18 after bounded production Core and
PostgreSQL outage drills. The Core drill replayed Lily sequences `1508-1522`
and Nekro `200-201`; the PostgreSQL drill replayed Lily `1544-1545` and Nekro
`204`. Every record retained its original hash, received an exact receipt and
closed without a watermark gap. The drills deliberately remain visible in the
cumulative replay-failure counters; an empty current `last_error`, zero
pending/quarantine and matching local/Core watermarks distinguish recovered
evidence from an active incident. Exact times and behavior checks are in
`C0D_ACCEPTANCE.md`.

## 8. Phase 3b `ledger_only` 上线与回滚

2026-07-19，Core 从提交 `846d93d` 构建为镜像
`sha256:ef9abe52d9df2f6f03701b76474afa5d02d751f702a3623b3c0d4e91f9d432fc`，
只重建 `lily-core` 后自动将生产库从 `0013_collection_reliability`
升级到 `0014_tool_invocations`。当前必须保持：

- `SUPERLILY_TOOL_EXECUTION_MODE=ledger_only`；
- `SUPERLILY_TOOL_GLOBAL_STOP=false`，但开关快照必须进账本；
- descriptor lifecycle 仍为 `reviewed`；
- 无 `tool_attempts` 表、无 lease/start/complete 路由；
- Provider 只能报告 inventory/heartbeat，不能创建 invocation。

上线前备份位于
`/home/justin/backups/superlily/20260719-phase3b-ledger`（目录权限 `0700`）：

- `superlily-pre-phase3b-ledger-846d93d.dump`：149,035,505 字节，
  SHA-256 `fd812d0c63af2807b77f3200c0f0b4ccd4830181d344d7dac0089a2c5adfef62`；
- `lily-core-bridge-pre-restart.tgz`：SHA-256
  `38e702c6452e7e630c9ac1eac9ba08be7dfff8e0b53804c2f260aa1fdba1c8f0`；
- `nekro-superlily-bridge-0.5.0.tgz`：SHA-256
  `945f8e30e6429e9d7629e72b803afccca801f3e6b66ede9eed085c516f4f3137`。

三个文件均为 `0600`。PostgreSQL 备份已通过 `pg_restore --list` 和隔离库
实际恢复。bridge 0.5.1 只增加后台 worker 监督、延时自恢复和心跳
异常可见性，不改变 claim、命令或回复语义。部署时只重启 Lily 和 Nekro；
NapCat、PostgreSQL 和 Provider 未重启。

第一回滚手段是将 execution mode 改回 `off` 并只重建 Core，这会禁止新账本行，
但保留不可变审计记录。如果 bridge 出现回归，只恢复对应的备份包并重启
该 bot，不删除 durable spool。只有确认必须破坏性撤回 schema 时，才在再次备份后
停止新调用并 downgrade 到 `0013_collection_reliability`；这不是首选事故处置。

## 9. `0015_tool_attempts` 与执行 Provider 上线记录

2026-07-19 02:35 CST，提交 `dd9c375` 的最终 Core 与 status Provider 分别部署为：

- Core 镜像 `sha256:3a5cf91b314e5a1bf79bf24266f572b0ba8bb7a806cecd8842f3c2e44d3d7d57`；
- Provider 镜像 `sha256:7b47646823c24041f9c3e34481ae0496d37c2cccbb67c0fc40b93c073d66f13f`；
- Core Compose 配置哈希 `bf9cdc55b6133d8a35a16a53eb7f77a6b0ae7123f7935db3aaadb4ee6085d04e`；
- Provider Compose 配置哈希 `c0b31124572f6ec6d8735fcaa7c1b34262f7d620e6443a71f3c07e23d9b959d3`。

上线前 PostgreSQL 自定义格式备份为
`/home/justin/backups/superlily/20260719-phase3b-attempts/superlily-pre-phase3b-attempts-820be5e.dump`，
大小 149,930,965 字节、权限 `0600`、SHA-256
`ab93d71e7ded563170fefcffa29309e598e8b611a6351bb05ddbe27d6c5b653e`。
`pg_restore --list` 通过；同一 PostgreSQL 17 镜像中的隔离恢复保持
`0014_tool_invocations`，并核对 1 条 invocation、1 份 descriptor、385,837 条
source event、417,544 条 observation、5,935 条 receipt、89,662 条 claim 与
16,815 条 response，随后停止临时恢复容器。

Core 启动日志记录 `0014_tool_invocations -> 0015_tool_attempts`；最终
`alembic current` 为 head，`alembic check` 无 drift。生产环境继续保持：

- `SUPERLILY_TOOL_EXECUTION_MODE=ledger_only`；
- global stop 为 false；canary/enforce scope 均为空；
- 旧 `status.inspect@1.0.0` 与新 `status.inspect@1.0.1` 均为 `reviewed`；
- `1.0.1` 从精确 Git 对象 `820be5ed369d7ef932caaa79e4959605e1eeebee`
  导入，descriptor SHA-256 为
  `398fb49dfff2cc76822e68afa305af2a8aee3aa4f4c50a375320f13175117911`；
- 原 `recorded_only` invocation 保持不变；attempt、attempt event、active attempt
  均为 0；经过认证的 lease 请求返回 204。

Provider 已从 `report` 切到 `serve`，继续使用只读 root、空 capability、
`no-new-privileges`、无发布端口的容器边界。最新 inventory 报告实现哈希
`396798fafb161e13e9348de11d3c29b50068532ec25042536f9fe05a75382c78`，
hard wall-time/output-bytes；heartbeat 为 healthy、最大并发 1、
`spawn_hard_deadline`。无工作轮询从 0.25 秒退避到 5 秒，只过滤成功的空 lease
204 日志，真实 lease 和错误仍保留。

切换后的第一个观察窗口出现一次 `ReadError` 不明确响应；Provider 没有盲目重试
任何工作，数据库仍为零 attempt，随后在无容器重启的情况下恢复健康 heartbeat，
后续窗口无重复异常。Lily 与 Nekro 均保持 `online` 且心跳新鲜；普通事件、claim
和 Core health 在替换前后持续成功。

本次上线只签署“可执行 schema/Provider 在 `ledger_only` 下安全空转”。没有激活
descriptor，也没有切换 canary。ADR 0005 要求 activation、suspension、Provider
quarantine 和 canary 变更必须先通过角色/会话、重认证、CAS、幂等、审计和回滚
测试；在该治理门完成前，不允许用直接 SQL 绕过它。

回滚优先将 Provider 恢复为旧 reporting-only 镜像或停止 Provider，并把 Core 模式
保持/退回 `ledger_only` 或 `off`。只有无 active attempt、另做新备份并同步回退
应用时，才考虑 downgrade 到 `0014_tool_invocations`；不得删除 invocation、attempt
或 append-only 事件来伪造回滚。

## 10. 最小控制面 M0 默认禁用上线记录

2026-07-19 03:21 CST，提交 `5e2e2997495bbb9e3a84f96beb7fd0fbb3ab838e`
只重建并替换 Core，生产迁移从 `0015_tool_attempts` 线性升级到
`0015a_control_plane_auth`。Core 镜像为
`sha256:9d4470d72edcf2b1d61525e5d040fd86f76c3680fdaeb9a6f7a308ef927c2501`，
容器环境配置单向哈希为
`e2932ebe551338fe62e4233a9440f1289b646cebfcbb0ec9d28a5e8b2c5e5cc5`。
PostgreSQL、Provider、Lily、Nekro 与 NapCat 均未因本次迁移重启。

上线前自定义格式备份位于
`/home/justin/backups/superlily/20260719-phase3-control-m0/superlily-pre-control-m0-5e2e299.dump`，
大小 150,140,201 字节，SHA-256
`aa2e6dda601fcbb8b6df3412e6e5d407459b396dd3815c466cc074da0b7f6c71`；
目录权限为 `0700`，root 所有的 dump 权限为 `0600`。`pg_restore --list` 通过，
并在同一 PostgreSQL 17 容器的隔离数据库实际恢复出 `0015_tool_attempts`、386,124
条 source event、2 个 descriptor、1 个 invocation 和 0 attempt。验证数据库与
容器临时 dump 已删除，主机备份保留。

生产 `alembic current` 为 `0015a_control_plane_auth (head)`，`alembic check` 无
drift；4 张控制面表和 3 个 PostgreSQL append-only trigger 存在，session、login
attempt、mutation、audit event 均为 0。生产配置确认 operator、Host/Origin 与 audit
pepper 均为空，控制面登录稳定返回带 `no-store`、`nosniff`、`no-referrer` 和 CSP
的 503。因此 M0 上线只签署“默认不可用的会话/审计底座”，没有产生任何 operator
authority。

工具执行继续为 `ledger_only`、global stop=false；`status.inspect@1.0.0/1.0.1`
仍为 `reviewed`，原 invocation 仍为 1 条 `recorded_only`，attempt/attempt event/
active attempt 均为 0。切换后 Lily/Nekro 均为 online，心跳年龄 17/18 秒；Provider
为 healthy、0/1 并发、心跳年龄 11 秒。

第一回滚手段是保持 operator 配置为空并回退应用镜像；这不会改变工具热路径。只有
确认应用必须回到旧 schema、控制面四表仍为空且已经另做新备份时，才可将 Core 与
数据库一起 downgrade 到 `0015_tool_attempts`。一旦 M1 以后产生 mutation/audit，
不得用 downgrade 删除证据，必须使用新的反向 mutation 作为回滚。
