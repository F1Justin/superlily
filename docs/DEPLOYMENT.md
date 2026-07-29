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

## 11. Descriptor lifecycle M1 默认禁用上线记录

2026-07-19 04:04 CST，提交 `2700929160d0eb7e123167697fec7d76b1dd885b` 只重建
并替换 Core，生产迁移从 `0015a_control_plane_auth` 线性升级到
`0015b_descriptor_mutations`。Core 镜像为
`sha256:2d5b9db4769d97d1c442ef8cfd153a0c324004c91ff55779359ac249dafa7d5a`，
镜像内 `pip check` 通过；容器配置环境单向哈希为
`bd5ff0c09ef5fca00f112635f40b3a0391337acdbd08537872a43bcda938ec0a`。
PostgreSQL、status Provider、Nekro 与 NapCat 的启动时间均未改变。

上线前自定义格式备份位于
`/home/justin/backups/superlily/20260719-phase3-control-m1/superlily-pre-control-m1-2700929.dump`，
大小 150,237,499 字节、root 所有、权限 `0600`、SHA-256
`ee98a1fb52b5eb03af7fc18866bfdc889dbe72704b8e59bfb7ae3ab33bf224c9`；
目录权限为 `0700`。`pg_restore --list` 通过，并在独立 PostgreSQL 17 临时容器
实际恢复出 `0015a_control_plane_auth`、386,273 条 source event、2 个 descriptor、
1 个 invocation、0 attempt，以及全零的 session/mutation/audit 表。临时恢复容器和
容器内 dump 已删除，主机备份保留。

生产启动日志记录 `0015a_control_plane_auth -> 0015b_descriptor_mutations`；
`alembic current` 为 head，`alembic check` 无 drift。`control_plane_previews` 与
descriptor `resource_version` 已存在，login/mutation/audit/preview、lifecycle event
和 descriptor authority 共 6 个 PostgreSQL trigger 存在。5 张控制面表均为 0；
两个 descriptor 仍为 `reviewed/resource_version=1`。

生产 operator、Host、Origin 与 audit pepper 均为空；preview=60 秒、mutation
限额=10/60 秒。M1 preview 路由返回带 `no-store`、`nosniff`、`no-referrer` 和 CSP
的 503。工具执行保持 `ledger_only`、global stop=false；既有 invocation 仍只有
1 条 `recorded_only`，attempt 与 attempt event 均为 0。替换后 Lily/Nekro 均为
online，Provider 为 healthy、0/1 并发。

第一回滚手段是保持 operator 配置为空并回退应用镜像；工具热路径不受 M1 路由影响。
只有确认 M1 五张控制面表均为空、descriptor 资源版本仍为 1、已经另做新备份并同步
回退应用时，才可 downgrade 到 `0015a_control_plane_auth`。一旦产生任何 preview、
mutation、audit 或 lifecycle event，不得以 downgrade 删除证据；必须使用新的反向
mutation 降低 authority。

## 12. Provider quarantine M2 默认禁用上线记录

2026-07-19 04:52–04:54 CST，提交
`1f12100a48df189b7829751af97a383173038d7f` 的 Core 与 status Provider 完成替换，
生产迁移从 `0015b_descriptor_mutations` 线性升级到
`0015c_provider_quarantine`。Core 与 Provider 镜像分别为
`sha256:83e338743719da8d5534a76322792a8f06fcbd4cf758625c26ff5395a8d51504` 和
`sha256:db13bb712ea72c3edf729a053d51b696e5d070131ff0eb262ce4d12636dcec8d`，
两张镜像内 `pip check` 均通过；Compose 配置哈希分别为
`815ce6f44d709d7032a1c6e92d95a999c031fd8a0ee20218b3d8bec188ee685d` 和
`ea06bcdd92005d0eb2d284c87383c031fb1cccfc83631736f2b9ddc1d9b2b05f`。
PostgreSQL 没有重建。

上线前自定义格式备份位于
`/home/justin/backups/superlily/20260719-phase3-control-m2/superlily-pre-control-m2-1f12100.dump`，
大小 150,346,845 字节、权限 `0600`、SHA-256
`02a8e7591f64935c8dc2c80d94367115ef2662006d2ad1ce276c5065ed67b3c9`；
备份目录权限为 `0700`。除 `pg_restore --list` 外，还在独立 PostgreSQL 17 容器中
实际恢复出 `0015b_descriptor_mutations`、386,440 条 source event、2 个 descriptor、
1 个 Provider、1 个 invocation、0 attempt，以及全零的 5 张控制面表。一次性容器和
容器内副本已经删除，主机备份保留。

生产启动日志明确记录 `0015b_descriptor_mutations -> 0015c_provider_quarantine`；
`alembic current` 为 head，`alembic check` 无 drift。随后通过本机 Git-bound CLI 从
上述完整提交导入 `status.inspect@1.0.2`，descriptor hash 为
`0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd`，结果为
`reviewed/resource_version=1`，没有激活。三版 descriptor 均保持 reviewed；Provider
`provider-status-primary` 保持 `active/resource_version=1`，仅有原始 sequence=1
lifecycle event，没有发生 quarantine/restore mutation。

新 Provider 的最新 inventory 精确报告 `1.0.2`、上述 descriptor hash、implementation
hash `156aaa422b4a1dd5290f31312512526866ba2826f1f04b318084c2bb166f4aac`、
协议 `superlily-provider-pull-v1` 和 hard wall-time/output-bytes；heartbeat 为 healthy、
0/1 并发。控制面 4 个只追加 trigger、descriptor 2 个 trigger 和 Provider 2 个 trigger
均存在。operator 与 audit pepper 未配置，Host/Origin 为空数组；Provider preview
路由带 `no-store`、`no-referrer`、`nosniff` 和 CSP 返回 503。探测后 5 张控制面表仍
为 0。执行模式保持 `ledger_only`、global stop=false、canary/enforce scopes 均为空；
既有 invocation 仍为 1 条，attempt 与 attempt event 均为 0。

第一回滚手段是保持控制面默认关闭，并将 Provider 先退回旧镜像、Core 再退回旧镜像；
`0015c` schema 可暂时保留，已导入的 `1.0.2` authority 也不得删除。只有确认没有任何
Provider preview/mutation/audit、新 lifecycle event 或资源版本变化，已经另做新备份，
且应用已同步回退时，才可考虑 downgrade 到 `0015b`。一旦产生治理证据，只能使用
新的反向 mutation 降低 authority，禁止靠删表或 downgrade 抹除历史。

## 13. Git-bound rollout plan M3 默认禁用上线记录

2026-07-19 06:01–06:04 CST，提交
`8f2547362c722a4ff1eb4c612f71c383e268cb3c` 只重建并替换 Core，生产迁移从
`0015c_provider_quarantine` 线性升级到 `0015d_rollout_plans`。Core 镜像为
`sha256:5de28375836bc342840a9a5e8ddbf5f5d9aaf269221f8bac6364e1b6c78a8e7f`，
镜像内 `pip check` 通过；Core Compose 配置哈希为
`b86d57acf3739921dc4631253841d09d44911a4e7f62d0318f30cc077c30d9b0`。
未重建的 Provider 配置哈希仍为
`ea06bcdd92005d0eb2d284c87383c031fb1cccfc83631736f2b9ddc1d9b2b05f`，
镜像仍为
`sha256:db13bb712ea72c3edf729a053d51b696e5d070131ff0eb262ce4d12636dcec8d`。
PostgreSQL 与 Provider 的容器启动时间分别保持
`2026-07-18T13:38:21Z` 和 `2026-07-18T20:54:20Z`，没有随 Core 重启。

上线前自定义格式备份位于
`/home/justin/backups/superlily/20260719-phase3-control-m3/superlily-pre-control-m3-8f25473.dump`，
大小 150,464,738 字节、权限 `0600`、SHA-256
`25f4333b811773051126653ee235de485621b25211c3996010054daca32b252a`；
`pg_restore --list` 通过。第一次隔离恢复使用 2 GiB tmpfs，数据导入后在验证两张大表
外键时因 PostgreSQL 临时空间耗尽而失败；该一次性副本已删除，没有触碰生产库。
随后从空库改用自动清理的 Docker 临时磁盘卷，并以 `--exit-on-error` 零错误恢复。
恢复结果为 `0015c_provider_quarantine`、386,650 条 source event、418,421 条
observation、6,812 条 receipt、3 个 descriptor、1 个 Provider、1 条 invocation、
0 attempt/event 和全零的 5 张控制面表。成功的一次性容器及临时卷已删除，主机备份
保留；生产 PostgreSQL 容器内临时 dump 副本也已删除。

生产启动日志明确记录
`0015c_provider_quarantine -> 0015d_rollout_plans`；`alembic current` 为 head，
`alembic check` 无 drift。四张 rollout 表、plan item/event/counter 均为 0；四个
rollout trigger 和三个 PostgreSQL guard function 均存在。operator、Host、Origin 与
audit pepper 仍未配置，执行上限保持 `ledger_only`、global stop=false。Admin
Registry API 返回 rollout plans=0、active=0、`active_rollout_plan=null` 和
`leases_enabled=false`；M3 preview 返回带 `no-store`、`no-referrer`、`nosniff` 和
CSP 的 503。真实 Provider 身份的 lease 探测返回 204/空正文，探测后仍只有原来的
1 条 `recorded_only` invocation，attempt/event 和五张控制面表合计均为 0。

三版 `status.inspect` descriptor 均为 `reviewed/resource_version=1`；Provider 保持
`active/resource_version=1`。最新 runtime 仍精确报告 `1.0.2` descriptor hash、
implementation hash
`156aaa422b4a1dd5290f31312512526866ba2826f1f04b318084c2bb166f4aac`、
hard wall-time/output-bytes、healthy 和 0/1 并发。Core 替换后 Lily/Nekro 分别为
online，心跳年龄 24/27 秒。因此本次只签署 M3 默认禁用底座，不导入或激活任何
rollout plan，不激活 descriptor，也不签署 canary。

第一回滚手段仍是保持 `ledger_only` 或进一步降到 `off`，无需改 schema。若必须回退
到不认识 `0015d` 的旧 Core 镜像，不能直接替换：旧 Alembic 不认识新 revision。
只有确认四张 rollout 表全零、没有任何 M3 preview/mutation/audit、已经另做新备份时，
才可先用当前镜像 downgrade 到 `0015c`，再替换旧 Core。一旦导入过 plan 或产生 M3
治理证据，就不得用 downgrade 抹除；应保持兼容应用并通过 plan pause/global stop
降低 authority。

## 14. 首批 Git-bound 计划与单次 `status.inspect` 生产 canary

2026-07-19 06:14 CST，提交
`cb9a0c920727265e941804228d41fac17462802d` 中的四份计划通过 Git-bound
CLI 导入生产为 `reviewed/resource_version=1`。导入后四张 rollout 表分别为
4 个 plan、4 个 item、4 个初始 lifecycle event 和 4 个 counter；四个 counter
均为 0，没有 active plan。每份计划最多调用 1 次，精确绑定
`status.inspect@1.0.2`、`admin_api`、`qq:group:1080353942`、
`provider-status-primary` 和预期资源版本。

开放任何短时 authority 前，又取了一份包含这四份计划的新备份：

- 路径：
  `/home/justin/backups/superlily/20260719-phase3-first-canary/superlily-pre-first-canary-cb9a0c9.dump`；
- 大小：150,554,824 字节；权限 `0600`；
- SHA-256：`b5fe18892f7da8741b7db590547c6f9b4fb2347bb197fafbf6ee8d99a65b917c`。

`pg_restore --list` 通过后，备份在一个独立 PostgreSQL 17 磁盘卷中以
`--exit-on-error` 零错误完整恢复。恢复库为 `0015d_rollout_plans`，包含
386,759 条 source event、418,532 条 observation、6,923 条 receipt、3 个 descriptor、
1 个 Provider、1 条 invocation、0 attempt/event、4 个 plan/item/event/counter。四份计划
恢复后仍为 `reviewed/rv1`、消费 0；`1.0.2` descriptor 为 `reviewed/rv1`，
Provider 为 `active/rv1`。源库在备份后多出的 4 条持续采集记录不属于恢复
丢失。临时容器、卷和生产容器内的 dump 副本已删除，主机备份保留。

06:25–06:26 CST 的临时控制面使用四个相互独立的随机口令角色：
reviewer、security_admin、operator 和 break_glass。明文口令只存在于演练进程
内存和本机回环 HTTP body；配置中只传递 scrypt verifier 和随机 audit pepper，
未写入 `.env`、Git 或文档。控制面证据包含 4 次成功登录、13 次成功重认证、
13 份 preview 和 13 笔接受 mutation；没有被拒绝的登录或 mutation。

生产操作顺序与结果为：

1. reviewer 将 `status.inspect@1.0.2` 从 `reviewed/rv1` 激活到
   `active/rv2`；
2. global-stop plan 激活后排入 1 条调用，Core 以
   `SUPERLILY_TOOL_GLOBAL_STOP=true` 短时重建；在 deadline 前手工 lease
   返回 204，attempt=0，计划随后暂停；
3. descriptor-stop plan 排入 1 条调用，reviewer 将 descriptor 置为
   `suspended/rv3`；deadline 前 lease=204/attempt=0，然后恢复
   `active/rv4` 并暂停计划；
4. provider-stop plan 排入 1 条调用，security_admin 将 Provider 置为
   `quarantined/rv2`；deadline 前 lease=204/attempt=0，Provider 在 quarantine 中
   重新上报健康证据后恢复 `active/rv3`，计划暂停；
5. success plan 排入 1 条调用并重启 Provider，产生唯一 attempt/fence=1，
   完成 `lease -> start -> complete`；结果为 `provider_runtime/status=ok`，
   wall 371 ms、CPU 351 ms、峰值内存 51,187,712 bytes、输入 28 bytes、
   输出 299 bytes、artifact 0 bytes，随后计划暂停。

三条被 stop 保护的调用之后均由 reaper 按契约终止为
`queued -> timed_out/deadline_expired`，始终没有 attempt。演练脚本的最后验收
曾错把此终态写为 `expired`，因此在主体成功后以非零码退出；这是
演练断言偏差，不是生产状态机偏差。`finally` 仍然完成 Core 默认配置恢复和
Provider 启动。

验收后四份计划均为 `paused/resource_version=3`、消费 1/1，无 active
plan。Registry 为 `mode=ledger_only`、`global_stop=false`、
`active_rollout_plan=null`、`leases_enabled=false`；descriptor 为 `active/rv4`，Provider 为
`active/rv3`、healthy。Core 临时 operator/Host/Origin/pepper 全部清空，登录路由再次
返回带 `no-store`、`no-referrer`、`nosniff` 和 CSP 的 503。演练窗口中
`responses` 表零新增，也没有 `qq:admin_api:*` 关联 response；两个 bot 仍为
online、心跳新鲜。首轮脚本因上述终态断言偏差没有执行到 logout；
这四个会话在控制面保持默认关闭期间于 06:40:11 CST 全部过期，且在
下一次使用不同 operator ID 的控制面配置前已验证无未过期旧会话。

### rollout plan pause 独立生产证明

为避免把“单测和间接空转”写成 plan pause 的直接生产证据，提交
`26d5cee3d30d4829ad04273de3b359abc489eb60` 又增加了第五份单次计划。
计划哈希为
`f040be10438d0aa2c3b8c244dd82ea749bf345f6079586e0efdbd83484ca4a27`，
精确绑定当时的 descriptor rv4 和 Provider rv3，导入只得到
`reviewed/rv1`、消费 0。

该计划激活前再次创建了包含首轮全部不可变证据的备份：

- 路径：
  `/home/justin/backups/superlily/20260719-phase3-rollout-pause/superlily-pre-rollout-pause-26d5cee.dump`；
- 大小：150,643,090 字节；权限 `0600`；
- SHA-256：`65f10a778c8208e6437122b8e469dbac429b09bd578dac9e69ce494d212b4e02`。

它在独立 PostgreSQL 17 磁盘卷中以 `--exit-on-error` 完整恢复为
`0015d_rollout_plans`，包含 386,862 条 source event、418,635 条 observation、
7,026 条 receipt、3 个 descriptor、1 个 Provider、5 条 invocation、1 个 succeeded
attempt、3 个 attempt event、5 份 plan/item/counter、13 个 plan lifecycle event、
4 个 session、13 笔 mutation 和 43 条 control audit。前四份计划仍为
`paused/rv3`、消费 1，第五份仍为 `reviewed/rv1`、消费 0；调用终态为
1 条 recorded_only、3 条 timed_out 和 1 条 succeeded。临时容器/卷与容器内副本
已删除，主机备份保留。

06:43 CST，两个新的 operator/break-glass ID 登录后，operator 将第五份计划
激活为 rv2。Provider 先停止以避免抢跑；Core 排队一条 deadline=5 秒的调用后，
break-glass 立即将计划暂停为 rv3。在 deadline 前使用真实 Provider 凭据手工
lease，结果为 204、invocation 仍为 queued、attempt=0。Provider 重启后也没有
领取，调用由 reaper 终止为 `timed_out/deadline_expired`。脚本以零码结束，
两个新会话均显式 logout/revoked，Core 随后回到默认配置。

最终控制面累计 6 次接受登录、15 次接受重认证、15 份 preview、
15 笔接受 mutation 和 2 次接受 logout，没有被拒绝的登录或 mutation。
六个 session 中 2 个已 revoked、4 个已过期，无未过期且未撤销会话。五份计划
现均为 `paused/rv3`、消费 1/1；生产 invocation 终态为 1 条 recorded_only、
4 条 timed_out 和 1 条 succeeded，只有成功 canary 存在那 1 个 attempt。Core
容器不含 Provider token；最终 ledger-only 空 lease 由 Provider 容器以独立凭据发起，
返回 204/空正文。

回滚首选始终是暂停 active plan 或将 Core 恢复 `ledger_only`。现在已存在
plan、preview、mutation、lifecycle、invocation 和 attempt 的只追加生产证据，因此
不再允许 downgrade 到 `0015c` 或删表伪造回滚。进入下一次故障演练前必须
创建新的 Git-reviewed 单次计划，不得重置本批 counter 或重开已暂停的权限。

## 15. 第二批故障矩阵、恢复与稳定窗口

2026-07-19 07:45–07:46 CST，提交
`7f509e96213a2eefcd9af6fee4aea86115abb71f` 中的八份一次性 plan 已从完整 commit
导入并逐份执行。每份精确绑定 `status.inspect@1.0.2`、
`qq:group:1080353942`、`admin_api`、`provider-status-primary`、descriptor rv4、
Provider rv3 和最多 1 次调用。外层编排只使用本机回环控制面；随机 operator 密码、
break-glass 密码和 audit pepper 只存在内存，未写入 argv、`.env`、日志或 Git。

开放 authority 前的备份为：

- 路径：
  `/home/justin/backups/superlily/20260719-phase3-fault-matrix/superlily-pre-fault-matrix-7f509e9.dump`；
- 大小：150,886,660 字节；权限 0600；
- SHA-256：`8dc4f145066a58bf7a633501934814ff15a59cb9e74642d94e8836c4d4bb20ab`。

`pg_restore --list` 通过后，它在独立 PostgreSQL 17 磁盘卷中零错误恢复出
`0015d`、387,209 条 source event、419,006 条 observation、7,397 条 receipt、
16,899 条 response、3 个 descriptor、1 个 Provider、5 份 paused plan、6 条
invocation、1 个 attempt、3 条 attempt event、6 个控制会话、15 笔 mutation 和
53 条 audit。临时恢复容器和卷已删除，备份保留。

生产结果如下：

1. 1 秒 lease 的 safe retry 先回收 fence 1，再由 Core 发出 fence 2 并成功；旧
   worker start/complete 与重复完成共 3 次均以 409 拒绝并留下 attempt event；
2. 非法 output 终止为 failed，2099/1970 Provider 时间不能延长 DB deadline；
3. 取消确认终止为 cancelled，取消/完成竞态和取消未确认均保守终止为
   unknown_completion；
4. Core 与 PostgreSQL 各在 running attempt 后短停约 6.25 秒，恢复后均由 reaper
   记录 `lease_expired -> timed_out/deadline_expired`，没有第二次执行；
5. 每项 finally 都成功暂停 plan；两个临时会话显式 logout，Core 恢复默认配置，
   常驻 Provider 重启。

最终 13 份计划全部 `paused/rv3`、消费 1/1。14 条 invocation 分布为
recorded_only=1、timed_out=6、succeeded=3、failed=1、cancelled=1、
unknown_completion=2；10 个 attempt、36 条 attempt event，无 active
plan/invocation/attempt。第二批控制面留下 16 份 preview、16 笔接受 mutation、
24 条 plan lifecycle event、2 次接受 login/logout 和 18 次接受 reauth；两个会话
均 revoked，无未过期未撤销会话。07:45:30–07:46:15 CST 的 `responses` 增量为 0。

恢复后日志曾每 5 秒整数倍间隔偶发空 lease `ReadError`。定位为 idle poll 与
Uvicorn keep-alive 同为 5 秒的连接回收竞态；提交 `2b31c6b` 使 lease 轮询发送
`Connection: close`，真实执行请求保持连接复用。只重建 Provider 后的镜像为
`sha256:b14bdcec3ceb921fa07830016620a5648b116e55e142fcde29c7443f25cc1f9b`；
Core 镜像仍为 `sha256:5de28375836bc342840a9a5e8ddbf5f5d9aaf269221f8bac6364e1b6c78a8e7f`，
没有随构建重建。

修正版稳定窗口跨过完整 5 分钟 inventory 周期，数据库收到 2 个 inventory snapshot、
11 个 healthy heartbeat；Provider 日志零 warning/error、重启数 0。最终
Core/Provider/PostgreSQL 内存约 76/34/121 MiB，CPU 空闲；`0015d (head)` 且
`alembic check` 无新操作。Core 为 `ledger_only/global_stop=false/lease=15`，无
operator/Host/Origin/pepper，控制登录仍为带安全头 503。Lily/Nekro 在线且 spool
均为 healthy/reconciled、pending=0、quarantine=0、gap=null。命令 Registry 快照
fresh，18 条静态规则全部加载、stale snapshot=0；旧 `/wf`、`/tex` 等命令实现未被
切换或删除。

## 16. `0016` 确认与 Artifact 默认关闭上线

2026-07-19 09:36–09:45 CST，以提交
`cd41026520b4ab88ab7c21bd13b0abd7cae2defd` 完成确认挑战与内容寻址 Artifact
基础设施的默认关闭生产签署。本次不导入新 descriptor/plan，不启用自然语言 caller，
不执行工具，也不发送平台消息。

迁移前备份为：

- 路径：
  `/home/justin/backups/superlily/20260719-phase3-confirm-artifacts/superlily-pre-confirm-artifacts-cd41026.dump`；
- 大小：151,402,854 字节；目录权限 0700、文件权限 0600；
- SHA-256：`0ceaa7f4f9b7ca2e4538b9ec9e4d981d2f4a8223e6bcbc9faa8d8ca53bec0962`。

`pg_restore --list` 通过后，备份在无端口暴露、独立磁盘卷的 PostgreSQL 17 容器中
零错误恢复。恢复库为 `0015d_rollout_plans`，包含 387,909 条 source event、419,795
条 observation、8,186 条 receipt、3 个 descriptor、1 个 Provider、14 条 invocation、
10 个 attempt 和 13 份 plan；13 份 plan 全部 paused、总消费 13。confirmation 与
artifact 表当时不存在，证明这是迁移前回滚点。临时恢复容器、容器内副本和临时卷已
删除，主机备份保留。

新镜像与 Compose 配置身份如下：

- Core 镜像：
  `sha256:4a3f9143887f27ed0afd9219cca10f649a1423efa689f67a549733ce0c6760e7`，
  config hash `3275f443aaec0939f772de7551aebbc11a54d70dd1edf739cc948cd8a1da5dcd`；
- status Provider 镜像：
  `sha256:7cfd227d244d3e0ef6b59918aacadddb7fbcaa2d2bddf2128766f1be7d860994`，
  config hash `ea06bcdd92005d0eb2d284c87383c031fb1cccfc83631736f2b9ddc1d9b2b05f`；
- 两个镜像的 `pip check` 均为 `No broken requirements found`。

只滚动 Core 和 status Provider。Core 启动日志显示线性执行
`0015d_rollout_plans -> 0016_confirm_artifacts`，随后健康；PostgreSQL 启动时间仍为
`2026-07-18T23:46:06.121900322Z`，restart count=0，证明数据库未重启。最终
`alembic current` 为 `0016_confirm_artifacts (head)`，`alembic check` 报告无新操作。

迁移后的数据库签署为：

- `tool_confirmations`、`tool_confirmation_events`、`tool_artifacts`、
  `tool_artifact_events` 均为 0；
- confirmation/artifact 当前行保护触发器 2 个、事件禁止变更触发器 2 个、保护函数
  2 个；
- 原有 invocation=14、attempt=10、plan=13、active plan=0、paused plan=13、
  consumed invocation=13，均与迁移前一致。

运行态仍为 `ledger_only/global_stop=false`，`active_rollout_plan=null`、
`leases_enabled=false`。`SUPERLILY_ARTIFACT_ROOT` 和
`SUPERLILY_ARTIFACT_SECRET_PEPPER` 均未配置，所以 `artifact_enabled=false`；
Compose 只创建预备卷 `deploy_superlily_artifacts`，容器内挂载点属主为 65532:65532、
权限 0700。Registry 仍有 3 版 `status.inspect`，只有 `1.0.2` active/eligible；Provider
为 active，inventory 与 heartbeat 新鲜健康。

C0-D 在滚动期间继续收敛：签署时 source event=387,994、observation=419,881、
platform action=412、receipt=8,272；两个 collector watermark 的最大
`seen-contiguous` 差为 0。Lily 与 Nekro 都 online，两个 SQLite FULL spool 均
healthy、pending=0、quarantine=0、last_error=null；命令 Registry 快照新鲜。Core、
Provider 均 restart count=0，启动/运行日志无 warning/error。

当前首选回滚仍是保持 `ledger_only`、不创建新 plan、让 Artifact 配置继续为空；这不
需要回退 schema。因为 `0016` 四张新表仍为空，若确认应用版本也必须回退，可在先做
新的生产备份后切回旧 Core，再降级到 `0015d`。不得在表产生确认或 Artifact 证据后
删除账本来冒充回滚。

## 17. 文本 Wolfram Provider 与首次生产 canary

2026-07-19 10:14–10:50 CST，提交
`d695213ae4193ebb45e48e44221925135340ad16` 中的
`wolfram.run@1.0.0`、独立 Provider、合同、测试和 ADR 完成发布；单次 canary plan
来自后续完整提交 `95ad5c56dd669be40f0905341fe9a725f163a7c7`。本包没有数据库
迁移，不启用 artifact、图片、自然语言 caller 或平台发送。

发布前证据为 SQLite 455 通过、4 跳过，PostgreSQL 17 为 459 通过；Core 与
Wolfram Provider 镜像 `pip check` 均为 `No broken requirements found`。最终镜像：

- Core：`sha256:cea5d1496a3828ec4ef9afc96ef043fdaaf10754289c60f1d26318cd26a25efc`；
- Wolfram Provider：
  `sha256:77917f9b842924055f42216ab6ffdda6ee5a9d94f4b1313755d99dbee21578b2`；
- 既有 worker 保持
  `sha256:9bc73c09d6728be9cc13cea92760dd4b5b6066d6acd5480d8e6b1af11463bb77`，
  Wolfram 15.0.0，未重建、未重启。

Provider 容器以 uid/gid 1000、只读 rootfs、`cap_drop=ALL`、
`no-new-privileges` 和只读 socket mount 运行。worker identity 为
`edaed08c24d55e213f2d005c7a758c46f3ec76641ae2389e74bf0ce13e2ce030`；
implementation hash 为
`32996c572eb8f364463666e0126a35b77efa21ae03fe29d710bfa7377645a241`；
descriptor hash 为
`aa6e9b1c930406bab11500de6c7653219aa9e8b831ee5fc7d08b1ab3d239ddaa`。
无网络、只读 rootfs、空 capability 的最终镜像探针返回 `2+2 -> 4`。

上线先为 `provider-wolfram-primary` 生成独立随机 credential，并原子加入 Core token
map；没有复用 status、ingest、admin 或 bot token，也没有在输出、Git 或文档中记录
明文。Core 先以 `ledger_only` 重建，新 Provider 注册为 `active/rv1`，descriptor 从
完整 Git commit 导入为 `reviewed/rv1`。Provider 上报的 inventory hash 为
`2bb912a8ebe92aa70c11d6843fb89c85fc1c2497d60380e9c939ab829217b775`，
四项 required budget 均为 hard，heartbeat 健康，worker uid=1000；此时 effective
reason 只有 `inactive_descriptor`，active plan/attempt 均为 0。

临时启用 localhost-only 控制面时出现两项 fail-closed 运维发现：

1. scrypt verifier 含 `$`，放入 Compose `.env` 必须写成 `$$`，否则 Compose 会把
   哈希片段当环境变量展开；
2. `SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON` 只接受精确 HTTPS origin，不能配置本机
   HTTP origin。

两次错误都在 Core 加载 Settings 时拒绝启动，尚未接受 login、preview、mutation 或
tool invocation；PostgreSQL 和 worker 未重启，Provider 只留下 02:32–02:34 UTC 的
预期断连 warning。修正后 Core 恢复健康。后续所有控制请求使用
`control.superlily.local` 精确 Host、`https://control.superlily.local` 精确 Origin、
短会话、CSRF、重新认证、server preview、CAS、理由和幂等键。

reviewer 将 descriptor 从 `reviewed/rv1` 激活为 `active/rv2`。随后导入的
`wolfram-text-success-20260719@1.0.0` plan hash 为
`3daf4eee0fb0be8915f85e43b70a40d31f7f65289e3aae6310c210ee01631c75`，
精确绑定 `admin_api + qq:group:1080353942 + wolfram.run@1.0.0 +
provider-wolfram-primary + descriptor rv2 + Provider rv1`，最多一次。Core 先在无
active plan 时切到 `canary`，确认 attempt 总数仍为 10，再由 operator 把计划激活。

唯一 canary invocation 为 `27614162-8c70-42e3-af5a-db3f72a2a55e`：

- 只提交精确表达式 `2+2`，状态依次为
  `proposed -> queued -> leased -> running -> succeeded`；
- attempt `61eb40af-fb26-4c11-9ff0-7d12a1ae0829`，attempt number=1、fence=1，
  实现与 inventory hash 精确匹配；
- 结果为 `{"kind":"text","text":"4"}`，wall=8 ms、input=20 bytes、
  output=26 bytes、artifact=0；
- attempt event 只有接受的 `lease/start/complete`，没有重试、取消、不确定完成或
  非法输出；`tool_confirmations` 与 `tool_artifacts` 仍为 0；
- 旧 `/wf` 的现有 `data_source.evaluate` 随后在不经过 QQ 发送的串行对比中同样返回
  文本 `4`，没有图片或音频。

canary 后 plan 立即由 operator 暂停为 `paused/rv3`，counter 为 1/1；Core 恢复
`ledger_only`，active plan/attempt 均为 0。临时 reviewer 的 1 个会话和 operator 的
2 个会话全部 revoked；operator/Host/Origin/pepper 已清空，0600 临时明文 credential
文件已销毁且不可恢复。descriptor 保持 `active/rv2`、Provider 保持 `active/rv1`，
这不构成执行 authority；重新执行仍需要新的完整 Git-bound plan。

最终 `0016_confirm_artifacts (head)` 且 `alembic check` 无新操作；PostgreSQL 启动
时间仍为 `2026-07-18T23:46:06.121900322Z`、restart=0。C0-D 最终 source event、
observation、platform action、receipt 分别为 388,480、420,457、435、8,848，两个
watermark 的 `seen-contiguous` 差为 0。

02:44:44–02:49:44 UTC 的稳定窗口跨过完整 300 秒 inventory 周期：两份 inventory
hash 均为 `2bb912a8ebe92aa70c11d6843fb89c85fc1c2497d60380e9c939ab829217b775`，
期间 10 次 heartbeat 全部 healthy 且只引用该 hash；Wolfram Provider 日志零新增，
Core、Provider、worker 与 PostgreSQL restart count 均为 0。至此文本
`wolfram.run@1.0.0` 生产迁移完成签署。

## 18. LaTeX Artifact Provider 与第三阶段退出

2026-07-19 11:29–11:50 CST，`latex.render@1.0.0` 完成生产上线。实现来自提交
`7a4e3ba76d6d69574538c09991913278562e9a87`，Provider 进程安全收尾来自
`3df6537`，单次 rollout plan 来自完整提交
`0d4c1bfe43d916c7acf06307ea6a88f715759d3e`。descriptor、Provider、worker、
artifact 与 canary 的详细字段见 `PHASE3_LATEX_RENDER.md`。

启用 artifact store 前制作的生产备份位于
`/home/justin/backups/superlily/20260719-phase3-latex-artifact/`，大小
152,117,402 字节，SHA-256 为
`881cf9aa7a634768ac42056744fa9b265e675d54edc58431b1ba989b7eeea8b2`。它已在
无端口暴露的独立 PostgreSQL 17 磁盘卷中完整恢复到 `0016_confirm_artifacts`；
恢复后 source event=388,819、invocation=15、attempt=11、artifact=0、plan=14，
临时容器和卷随后删除，备份保留且权限为 0600。

最终镜像与运行身份为：

- Core：`sha256:0450f2d9742bcbc69d73e354adf5cd4ebb60e4c4a001a06b225fb0842b89ee86`；
- LaTeX Provider：
  `sha256:cc2ec3b8d73c64f17f12d400dedb903422fb2e7df003757952e3cbddbedb72fc`；
- LaTeX worker：
  `sha256:845faf7b8caecf17540c1933a9a764b5c13865b5f57597a741aa27b3d75b69bc`；
- worker identity：
  `5fec6df87bbfda7666c2e47018763d080e2b99049cea57d24e3ad4bc160e848a`；
- Provider implementation hash：
  `26a473b53cb3291c91fa049ed8fc15316d8c44e6d91a9bfa790f6a314d1357c3`；
- inventory hash：
  `b4ad3081c6f2cd3bb4b4006125eb0088b455d29f7f2866433496ca455f4f2f4b`。

worker 实际为 network none、只读 rootfs、uid/gid 1000、cap drop ALL、
NoNewPrivs、1 GiB、1 CPU、128 PIDs、单并发和私有 tmpfs/socket。Provider token 与
其他 token 独立，worker 不持有任何 credential。Provider 启动后发现 `httpx` 默认
INFO 会记录每次 lease URL；最终实现将该 logger 降为 WARNING，并在配置加载后从
进程环境移除 token。修正镜像无轮询日志洪泛，Core 短暂重建时只保留一条不含 URL、
正文或 credential 的保守 ConnectError warning。

reviewer 将 descriptor 激活为 `active/rv2`；operator 激活的
`latex-artifact-success-20260719@1.0.0` plan hash 为
`d09f39c5fad1a45953bee32e0e2cbccad113967394e318ff6040460f2ecf4694`，精确绑定
`admin_api + qq:group:1080353942 + provider-latex-primary + descriptor rv2 +
Provider rv1`，最多一次。唯一 invocation
`a5138434-2b51-4b3a-98bd-810bfb51afc5` 和 attempt
`65a0cd4e-b8f9-4c38-9d7e-dcebd16fc8d1` 以 fence=1 成功，没有重试、取消或不确定
完成。usage 为 wall=1,245 ms、input=23 bytes、output=235 bytes、artifact=34,883
bytes。

artifact `982810cd-ece3-41e0-af04-e9575e5a847f` 依次记录 reserve、upload_start、
upload_complete、finalize、reference 五个事件。数据库与 0600 私有对象均证明 PNG 为
34,883 字节、2048×499，SHA-256 为
`4ad21ef65944d745782a87c7970bd56d9ce846ebda45be1f95d457d5bd1fdfce`；它保持
finalized/referenced 且未删除。没有 response 与 canary source/idempotency 关联。
旧 `tex2pic` 不经过 QQ 发送的串行对比同样成功生成 PNG，旧 `/tex` 未修改。

plan 随即暂停为 `paused/rv3`、1/1；Core 恢复 `ledger_only`，active plan/attempt
为 0。临时 control operator、Host、Origin 和 pepper 清空，登录返回 503，0600
明文 credential 文件销毁。临时控制面关闭后的 03:44:57–03:54:28 UTC 共 20 次
heartbeat 全部 healthy，03:44:57 与 03:49:58 两份 inventory hash 一致；该窗口内
Provider/worker 零新增日志，Core、Provider、worker 与 PostgreSQL 均零重启、零
OOM。PostgreSQL 启动时间仍为
`2026-07-18T23:46:06.121900322Z`、restart=0。

最终 SQLite 全量 463 项通过、4 项跳过，隔离 PostgreSQL 17 全量 467 项通过；三个
相关镜像 `pip check` 无破损依赖，`0016_confirm_artifacts (head)` 且
`alembic check` 无新操作。生产总账为 invocation=16、attempt=12、artifact=1、
confirmation=0、plan=15、active plan=0、active attempt=0。至此第三阶段三个代表性
工具全部使用公共 descriptor/invocation/provider/artifact 协议，第三阶段完成签署。

## 19. Phase 5a/5b 默认关闭部署与无发送验收手册

本节是尚未执行的生产手册，不是签署记录。2026-07-29 的只读审计显示生产 Core
健康、仍在 `0019_phase4_planning`；`SUPERLILY_AGENT_MODE` 未设置，等价于 `off`。
工具执行配置虽为 `canary`，但 `active_rollout_plan=null`、
`leases_enabled=false`，所以没有当前工具执行 authority。生产尚未迁移到
`0023_agent_model_routes`，也没有模型 Provider token。

### 19.1 部署前门

部署提交必须是已推送的完整 Git commit，工作树干净。发布记录需要冻结：

- commit、Core/Wolfram Provider 镜像 ID 与 config hash；
- SQLite 与 PostgreSQL 17 全量测试结果；
- `wolfram.run@1.1.0` descriptor hash
  `ec3375907804f588d765ed643b9c8481eb2d4a578924a614652cca64d0414da4`；
- `deepseek-v4-pro@1.0.0` profile hash
  `948f9b7cd20394f0607d1bb347f776e80f5b5e307c381223b8a40d3bf735bec3`；
- 迁移前 PostgreSQL 物理/逻辑备份、SHA-256、权限，以及隔离 PostgreSQL 17
  的完整恢复、`alembic current`、关键表计数和零恢复错误。

模型 token 必须独立随机生成，不得与 admin、ingest、执行 Provider、Renderer、
artifact 或 bot token 重用。DeepSeek API key 不进入 Core；验收驱动只从调用进程环境
读取并立即移除。不得把 token/key 写入 Git、命令输出、账本文本或验收文档。

### 19.2 默认 `off` 部署

第一次滚动只部署 Core，显式保持：

```text
SUPERLILY_AGENT_MODE=off
SUPERLILY_MODEL_PROVIDER_TOKENS_JSON={}
```

Core 启动时线性执行 `0019_phase4_planning -> 0020_agent_runs ->
0021_agent_tool_callers -> 0022_agent_tool_loops -> 0023_agent_model_routes`。健康后必须
验证：

1. `alembic current` 为 `0023_agent_model_routes (head)`，`alembic check`
   无 drift；
2. Agent 新表、trigger/function 与 CHECK 均存在，新表为空；
3. 原有 event/receipt/descriptor/Provider/invocation/artifact/delivery 计数没有丢失；
4. `/health/ready` 正常，创建 AgentRun 返回禁用冲突；
5. Registry 仍无 active rollout，lease 关闭，三个既有 Provider 健康；
6. Nekro/Lily 采集、spool、水位和确定性命令路径不依赖任何模型健康。

这一小节完成后仍没有模型请求、工具调用或平台发送。

### 19.3 5a 真实模型 shadow

从同一完整 commit 导入 Git-bound model profile。Core 加入一份独立
`deepseek-v4-pro` model token 后，以 `SUPERLILY_AGENT_MODE=shadow` 只滚动 Core。
DeepSeek key 由验收进程从既有受限配置读取，不复制进 Core 环境。

使用 `superlily-phase5-acceptance shadow`（或源码树
`scripts/phase5_acceptance_driver.py shadow`）执行一次探针。调用方必须提供完整
commit、唯一 run ID、四份彼此独立的环境 credential，并显式传入
`--no-platform-send-ack`。驱动硬限制 loopback Core、固定
`system:system:phase5-acceptance` 会话，不激活 Registry 资源，也不输出 prompt、
模型正文或凭据。

通过条件：

- 1 次真实模型 attempt，终态 `shadow_complete`；
- profile/commit/hash、usage、reason 和事件链完整；
- `tool_invocation_count=0`、`delivery_intent_count=0`；
- QQ/NapCat、Renderer 与公开群发送路径均未触达；
- 模型失败、超时或非法 JSON 只产生审计终态，不影响命令路径。

完成后先回到 `SUPERLILY_AGENT_MODE=off`，再进入 5b 准备。

### 19.4 5b 单次 Wolfram loop

5b 只开放 `wolfram.run@1.1.0`。先从完整 commit 导入 descriptor，并让
Wolfram Provider 精确上报 1.1.0 inventory；reviewer 激活 descriptor 后，读取真实
descriptor/Provider resource version。随后才生成一份新的 Git-bound rollout plan：

```text
mode=canary
max_invocations=1
rollback_mode=ledger_only
canonical_conversation=system:system:phase5-acceptance
caller=agent
tool=wolfram.run@1.1.0
provider=provider-wolfram-primary
expected_descriptor_resource_version=<observed exact value>
expected_provider_resource_version=<observed exact value>
```

计划必须在有界未来窗口内、单独 commit/push，并按完整 commit/hash 导入；导入只得
`reviewed`。Core 显式切到 `SUPERLILY_AGENT_MODE=bounded_readonly`，工具执行保持
`canary`。operator 激活唯一计划后，运行
`superlily-phase5-acceptance bounded-wolfram`，同时传入 exact plan ID/hash 和
`--no-platform-send-ack`。

驱动会在创建 run 前验证：唯一 active plan、未消费、一次额度、自然语言 caller
已启用、descriptor exact/active/eligible。随后模型只能提议精确 `2+2`，Core 创建
一条 `caller=agent` invocation，常驻 Wolfram Provider 完成计算；有来源、分级、
限长和 `untrusted=true` 的结果再进入一次 continuation。通过条件：

- AgentRun 只有一个 valid `wolfram.run@1.1.0` proposal；
- invocation 只走 `provider-wolfram-primary`，一次 lease/fence 后 `succeeded`；
- loop 依次 `tool_pending -> result_ready -> complete`；
- continuation 不能再调用工具，最终 `tool_invocation_count=1`；
- AgentRun、loop、Renderer 和平台总计 `delivery_intent_count=0`。

无论成功或失败，计划都立即暂停并核对 counter；随后 Core 回到
`SUPERLILY_AGENT_MODE=off`，工具执行回到 `ledger_only`。最终必须为
`active_rollout_plan=null`、`leases_enabled=false`、active attempt=0，且不存在任何
公开群 response/delivery 与探针关联。

### 19.5 稳定窗口与签署

回落后至少观察 30 分钟，并跨过多个 300 秒 inventory 周期。窗口内记录：

- Core、PostgreSQL、三个执行 Provider、Wolfram worker、Lily、Nekro 的 restart/OOM；
- inventory/heartbeat 一致性、spool pending/quarantine/gap 和采集水位；
- active plan/attempt、AgentRun、tool invocation、delivery intent 的最终计数；
- Core/Provider warning/error，以及确定性命令 Registry freshness。

签署文档必须分别声明 5a 与 5b 的 commit、数据库 head、真实 run/loop/invocation、
model request、usage/cost、plan counter、回落状态与稳定窗口。它只能签署
planner-only shadow 和单次只读 Wolfram loop；不得暗示 5c 写权限、历史检索、
长期模型自治或公开群自动回复已经开放。

生产操作员已经禁止向公开群主动发送合成测试内容。本手册没有任何公开群发送步骤；
若未来需要 UI/群聊可见验证，必须另行取得当次明确授权。
