# 第三阶段验收清单

## Status and entrance gate

Phase 3a completed after its recorded Phase 2 entrance prerequisite. Its
Registry schema/read surface, real reviewed `status.inspect` authority, shared
Provider SDK, stable Provider identity, and reporting-only runtime are deployed.
The accepted ADRs, tests, and production record are evidence for these narrow
checked items only; invocation, lease, artifact, tool execution, and
natural-language authority remain unchecked until their own evidence exists.

- [x] `ACCEPTANCE.md` contains the signed policy-v6 Phase 2 controlled samples,
  reused policy-v5 24-hour audit plus policy-v6 counterfactual replay,
  acknowledged claim coordination, response attribution, SQLite/PostgreSQL
  tests, `0011_claim_ack` migration/drift, backup and rollback.
- [x] Phase 3 ADRs approve descriptor/JCS authority, provider identity,
  transitions/recovery, artifact storage, credentials and control-plane auth.

## 3a: authority and effective registry

- [x] Git-reviewed descriptor bundles are the only authority source; import
  stores immutable canonical bytes/hash, commit, lifecycle and reviewer audit.
- [x] The restricted JSON Schema profile rejects duplicate keys, non-finite
  numbers, remote/dynamic refs, cycles, unknown/unsafe keywords and all size,
  depth, item and expansion-limit violations.
- [x] Shared JCS golden vectors produce identical canonical bytes/hash in Core,
  CLI and provider SDK; semantic Wolfram/LaTeX whitespace is preserved.
- [x] Migration `0012_tool_registry` passes SQLite/PostgreSQL fresh upgrade,
  downgrade/re-upgrade, concurrency and production drift tests.
- [x] Provider identity/credential is separate from bot ingest/admin identity;
  inventory snapshots are immutable/hash-verified and heartbeat freshness is
  separate.
- [x] Desired, reported and effective state plus stable ineligibility reasons
  are independently testable. Unknown/stale/mismatched inventory or heartbeat
  never grants authority.
- [x] First deployment has zero active descriptors and execution `off`; runtime
  discovery alone cannot create an invocation or lease.

### Zero-authority production deployment evidence

On 2026-07-18 at 14:21 CST, commit `164c81b` was deployed as Core image
`sha256:ae1686707cec1c2b6f1ebe11be16698218ebca1b59a7c892e4e64c3b8efb298d`
with Compose config hash
`a81571e3303bcb033a53a3ef9b3cb4766f41f0c50c7e3a283c33691fc159e5ff`.
Before migration, PostgreSQL 17.10 was backed up to
`/home/justin/backups/superlily/superlily-pre-phase3a-20260718-141630.dump`
(141,659,973 bytes, SHA-256
`5e2d87098245cd4b3ae9bb4087d2034a3730a72b77ede2056fcbf459eccff199`)
and restored successfully into an isolated PostgreSQL 17 container at
`0011_claim_ack`; the restored key-table counts were internally consistent.

The production startup log records `0011_claim_ack -> 0012_tool_registry`.
`alembic current` reports `0012_tool_registry`, `alembic check` reports no
drift, and all eight Registry tables contain zero rows. The running environment
has zero Provider tokens; the admin view reports zero descriptors, active
descriptors, eligible tools, providers and healthy inventories with execution
`off`. Both Provider write surfaces reject unauthenticated probes, no
invocation/attempt/lease table or route exists, and `status.inspect` remains
404 because the golden vector was not imported. PostgreSQL, Lily, Nekro and
NapCat were not restarted; both bot instances remained online, their runtime
command snapshot remained fresh, and legacy event/claim/heartbeat ingestion
continued successfully immediately after the Core-only replacement.

### Real authority and reporting-only Provider evidence

On 2026-07-18 at 22:43 CST, commit
`c48aaa18e35d99ab6468a683329311586c7f1518` deployed the first real authority:
`status.inspect@1.0.0`, descriptor SHA-256
`65af3c28c09b250b3418269416841fa980fae9cfb8ffcb87c6df5305f6fbd62c`.
The Core image is
`sha256:010209464fb4105c33bf430b07ee5a56ff19884a3b6f97cccb17ab83b985aed5`
and the reporting-only Provider image is
`sha256:e1650313c1708b07442867aefef905a6cfa7123154d852bec6f6e5f539636d3a`.
Before mutation, PostgreSQL was backed up in custom format to
`/home/justin/backups/superlily/20260718-phase3a-status/superlily-pre-phase3a-status-c48aaa1.dump`
(147,741,882 bytes, mode `0600`, SHA-256
`763f2e33906040a3da3962406d62be6d7b7d448af8c7d09166a2f9e0909741b1`);
`pg_restore --list` read the archive successfully.

The descriptor was loaded from that exact Git object, imported as `reviewed`
and never activated. Provider `provider-status-primary` has an unrelated
environment credential and an active stable registration. The container has a
read-only root, drops all capabilities, publishes no port, receives no admin or
bot token, and has no invocation/lease client. The installed implementation
self-test and its output schema passed in that container. SQLite and PostgreSQL
each passed all 254 tests; the focused descriptor/SDK/Core suite passed all 36.

After five minutes the Provider had independently created two immutable
inventory observations and eleven heartbeats, proving the 300-second inventory
refresh is distinct from 30-second health. Neither authority bytes nor
heartbeat metadata contains its bearer credential. The admin view reports one
descriptor, zero active descriptors, zero eligible tools, one Provider, one
fresh inventory and one healthy Provider. Runtime reasons are exactly
`budget_unenforceable`; effective reasons are exactly `inactive_descriptor`,
`budget_unenforceable`, and `execution_off`, because the later hard wall-time
lease executor does not exist. `POST /v1/tool-invocations` remains 404,
execution mode is `off`, and invocation endpoints, leases, and natural-language
callers are all false.

该 Phase 3a 报告切片完成时，生产仍为 `0013_collection_reliability`，
`alembic check` 无 drift，且尚未添加 Phase 3b 表。当时只重建 Core 并启动
新 Provider；PostgreSQL、Lily、Nekro 和 NapCat 均未重启。旧 claim/event 持续
写入，但 Lily 的普通心跳与命令快照早在 22:14 CST 就已停止刷新，早于
22:42 的 Core 替换，因此没有把该问题误归因于 Phase 3a 上线。后续 bridge
0.5.1 已在 `0014` 上线前补上 worker 监督与心跳自恢复，恢复证据见下节。

### `0014_tool_invocations` 生产证据

2026-07-19，提交 `846d93d` 对应的 Core 镜像
`sha256:ef9abe52d9df2f6f03701b76474afa5d02d751f702a3623b3c0d4e91f9d432fc`
完成部署。上线前 PostgreSQL 自定义格式备份为
`/home/justin/backups/superlily/20260719-phase3b-ledger/superlily-pre-phase3b-ledger-846d93d.dump`，
大小 149,035,505 字节，权限 `0600`，SHA-256 为
`fd812d0c63af2807b77f3200c0f0b4ccd4830181d344d7dac0089a2c5adfef62`。
`pg_restore --list` 和隔离数据库实际恢复均通过，恢复库保持
`0013_collection_reliability` 并在验证后删除。

生产启动日志明确记录 `0013_collection_reliability -> 0014_tool_invocations`；
`alembic current` 为 head，`alembic check` 无 drift。真实
`status.inspect` 提案 `9144f816-6814-43e5-84f5-63dcf869e63f`
只产生一条 `recorded_only` invocation 和 `propose -> record_only` 两条 transition；
相同幂等键重放返回 HTTP 200、原 invocation 及 `duplicate=true`。Provider 凭据
创建调用返回 401 且零落行。生产没有 `tool_attempts` 表，
`/v1/tool-executions/lease` 返回 404，policy snapshot 明确为
`queue_created=false` 和 `lease_created=false`。描述符仍为 `reviewed`，
Provider 镜像与启动时间未改变。

同日 Lily 与 Nekro bridge 部署 0.5.1 并只重启两个 bot 运行时。
Core 心跳显示两个实例均为 `online`，普通 reporter 和 durable-spool reporter
均为 `running`，重启数和心跳失败数均为零。完整 SQLite 与 PostgreSQL 17
套件各自 279 项全部通过。

## 3b: invocation and execution safety

### `0015_tool_attempts` 实现期证据

2026-07-19，`0015_tool_attempts`、Provider execution SDK 和
`status.inspect@1.0.1` 独立子进程执行器完成。SQLite 与 PostgreSQL 17 全量套件
各 313 项通过；测试覆盖四种模式、canary/enforce 独立精确范围、三个 stop、
并发领取唯一 lease、单调 fence、secret/Provider 绑定、DB-time 续租与 reaper、
取消竞态、迟到/重复完成、预算取消、非法输出、attempt 事件只追加，以及真实
Provider SDK -> Core -> 子进程 -> Core 的成功路径。

`status.inspect@1.0.1` descriptor SHA-256 为
`398fb49dfff2cc76822e68afa305af2a8aee3aa4f4c50a375320f13175117911`。
版本升级只改变不可变版本号与按真实 `spawn` 进程峰值测得的内存预算；执行子进程
不接收 lease secret、Provider token、bot token 或平台发送能力。完整边界和诚实的
非通用沙箱限制见 `PHASE3B_EXECUTION.md`。

这些是实现与发布前证据。生产仍需先以 `ledger_only` 部署并证明零 attempt，随后
另行签署 descriptor 激活、精确 canary、停止开关和中断恢复演练。

### `0015` 生产 `ledger_only` 证据

2026-07-19 02:35 CST，`0015_tool_attempts` 与 `status.inspect@1.0.1` 执行
Provider 已按 `ledger_only` 部署。Core/Provider 镜像分别为
`sha256:3a5cf91b314e5a1bf79bf24266f572b0ba8bb7a806cecd8842f3c2e44d3d7d57`
与
`sha256:7b47646823c24041f9c3e34481ae0496d37c2cccbb67c0fc40b93c073d66f13f`。
备份、隔离恢复、完整镜像/配置哈希和回滚顺序见 `DEPLOYMENT.md` 第 9 节。

生产为 `0015_tool_attempts` head 且无 drift；`1.0.1` descriptor 仍为
`reviewed`，Provider 报告 hard wall-time/output-bytes、健康 heartbeat 和最大并发
1。认证 lease 返回 204，attempt/event/active attempt 均为 0，原
`recorded_only` invocation 未改变。Lily/Nekro 心跳与正常 event/claim 流量持续。

因此“执行底座在 `ledger_only` 下安全空转”已签署。精确 canary 仍未签署：ADR
0005 禁止在角色/会话、重认证、CAS、幂等、append-only 审计和回滚测试通过前执行
activation/suspension/quarantine/canary mutation。直接 SQL 不可作为绕过方案。

### 最小控制面 M0 发布前证据

2026-07-19，ADR 0008 的 M0 会话与审计底座完成。`0015a_control_plane_auth` 在线性
迁移史中接在 `0015_tool_attempts` 后，不占用已经冻结给 confirmation/artifact 的
`0016`。operator authority 只来自严格版本化环境配置；默认空配置使控制面返回
503，既有 admin/provider/ingest bearer 均不能兑换控制会话。

SQLite 全量 323 项通过、1 项 PostgreSQL 专用迁移测试跳过；PostgreSQL 17 全量
324 项通过。覆盖 Secure/HttpOnly/SameSite cookie、内存态 CSRF、精确 Host/Origin/
Content-Type、脱敏 422、CSP/no-store、数据库时间过期、配置撤权、登录限速、scrypt
并发上限、再认证/退出 CAS、并发旧 CSRF 最多一次、secret 不落审计，以及
login/mutation/audit 三表的 UPDATE/DELETE 拒绝。两种数据库均通过迁移往返和
`alembic check`；PostgreSQL 另验证了真实 function/trigger 的创建与清除。

M0 没有 descriptor lifecycle、Provider quarantine 或 rollout mutation 端点，不能
单独解除 canary 门。下一包是 M1 的 server-computed preview、角色授权、资源版本
CAS、幂等 apply、接受/拒绝审计与回滚测试。

### 最小控制面 M0 生产默认禁用证据

2026-07-19 03:21 CST，提交 `5e2e299` 的 Core 镜像
`sha256:9d4470d72edcf2b1d61525e5d040fd86f76c3680fdaeb9a6f7a308ef927c2501`
上线。生产为 `0015a_control_plane_auth` head 且无 drift；4 张控制面表、3 个
append-only trigger 存在，所有新表均为 0 行。operator、Host/Origin 和 audit
pepper 均未配置，登录端点返回带完整安全头的 503。

备份目录、150,140,201 字节 dump、SHA-256、隔离实际恢复结果、镜像/配置哈希与
回滚约束见 `DEPLOYMENT.md` 第 10 节。工具模式保持 `ledger_only`；两个 descriptor
仍为 `reviewed`，原 invocation 仍为 `recorded_only`，attempt/event 均为 0；两个
bot 与 Provider 心跳新鲜。由此只签署 M0 默认禁用上线，不签署任何 mutation 或
canary authority。

### Descriptor lifecycle M1 发布前证据

2026-07-19，ADR 0009 与 `0015b_descriptor_mutations` 完成实现、审查和发布前回归。
M1 增加服务端 canonical preview、持久 preview/hash、descriptor 单调资源版本、
reviewer 角色、新鲜再认证、短时过期、apply CAS、幂等重放/冲突、runtime drift
重算和接受/拒绝审计。首包只允许 `reviewed -> active`、`active -> suspended`、
`suspended -> active`；回滚是一条新的反向 mutation，不删除旧证据。

数据库同时禁止 descriptor authority 改写和删除、lifecycle event/preview 的更新和
删除，并要求每次 lifecycle/resource version 更新恰好加一且同事务已有匹配 event。
测试覆盖默认禁用、精确目标、角色、CSRF、新鲜再认证、过期、runtime/global stop/
Provider 漂移、preview 与 mutation 独立限速、幂等、并发单一 CAS 胜者、直接 SQL
绕过拒绝和 secret 不落 preview/result/audit。SQLite 全量 334 项通过、1 项
PostgreSQL 专用迁移测试跳过；PostgreSQL 17 分段合计 335 项通过。两种数据库的迁移
往返与 drift 检查均通过。

这只签署 M1 发布前实现，不单独签署生产启用。M1 默认禁用上线证据见下一节；M2
Provider quarantine、M3 reviewed rollout plan 和精确 canary 仍是后续门禁。

### Descriptor lifecycle M1 生产默认禁用证据

2026-07-19 04:04 CST，提交 `2700929160d0eb7e123167697fec7d76b1dd885b` 只重建
并替换 Core。新镜像为
`sha256:2d5b9db4769d97d1c442ef8cfd153a0c324004c91ff55779359ac249dafa7d5a`，
配置环境单向哈希为
`bd5ff0c09ef5fca00f112635f40b3a0391337acdbd08537872a43bcda938ec0a`。
PostgreSQL、Provider、Nekro 与 NapCat 的容器启动时间均未改变；Lily/Nekro 随后
继续上报 online 心跳。

上线前备份为
`/home/justin/backups/superlily/20260719-phase3-control-m1/superlily-pre-control-m1-2700929.dump`，
大小 150,237,499 字节、权限 `0600`、SHA-256
`ee98a1fb52b5eb03af7fc18866bfdc889dbe72704b8e59bfb7ae3ab33bf224c9`。
`pg_restore --list` 通过，并在独立 PostgreSQL 17 临时容器实际恢复出
`0015a_control_plane_auth`、386,273 条 source event、2 个 descriptor、1 个
invocation、0 attempt 和全零 M0 控制面表；临时容器已删除，主机备份保留。

生产启动日志明确记录 `0015a_control_plane_auth -> 0015b_descriptor_mutations`；
`alembic current` 为 head，`alembic check` 无 drift。5 张控制面表均为 0，6 个
只追加/authority 触发器存在。operator、Host、Origin 和 audit pepper 仍为空，M1
preview 路由返回带 `no-store`、`nosniff`、`no-referrer` 与 CSP 的 503。两个
descriptor 仍为 `reviewed/resource_version=1`，既有 invocation 仍为 1 条
`recorded_only`，attempt/event 均为 0。验收时 Lily/Nekro 为 online、心跳年龄
28/29 秒；Provider 为 healthy、0/1 并发、心跳年龄 20 秒。

因此只签署 M1 默认禁用上线，不签署任何真实 descriptor mutation 或 canary。

### Provider quarantine M2 发布前证据

2026-07-19，ADR 0010 与 `0015c_provider_quarantine` 完成实现、审查和发布前回归。
M2 为 Provider 增加单调资源版本、security_admin 服务端 preview、新鲜再认证、CAS、
幂等与只追加证据。首包只允许 `active -> quarantined` 和
`quarantined -> active`；quarantine 可在 runtime 不健康时降低 authority，恢复则必须
重新证明 credential active、inventory/heartbeat 新鲜、heartbeat healthy、两者
inventory hash 一致、协议允许且存在明确 implementation hash。

Provider 稳定注册字段、Provider 行和 lifecycle event 受数据库 trigger 保护。lease
路径与 quarantine apply 先锁同一 Provider 行；PostgreSQL 并发测试证明，等待中的
lease 在 quarantine 提交后看到新状态并返回空，未创建 attempt。quarantined Provider
仍可用独立 credential 上报 inventory/heartbeat；重复注册比较初始 lifecycle event，
不会因当前处于 quarantine 而误报 authority 冲突。

同轮审查修正了 `status.inspect@1.0.1` 的 canary 前边界：旧 256 MiB 预算在全量进程
中稳定被约 263 MiB 峰值超过，且旧 spawn 只能在进入子进程后清空环境。新的不可变
`status.inspect@1.0.2` 使用 320 MiB 诚实预算和创建时只含安全 `PYTHONPATH` 的独立
worker；stdin/stdout 有界、父进程硬超时，worker 源码进入 implementation hash。
descriptor/implementation SHA-256 分别为
`0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd` 和
`156aaa422b4a1dd5290f31312512526866ba2826f1f04b318084c2bb166f4aac`。

SQLite 全量 341 项通过、2 项 PostgreSQL 专用测试跳过；PostgreSQL 17 分段合计
343 项通过。测试覆盖默认禁用、角色/CSRF/再认证、quarantine/restore、runtime
漂移与恢复 blocker、幂等、并发 CAS、preview/mutation 限速、直接 SQL 拒绝、secret
不落证据、quarantine 期间上报和 lease 行锁。两种数据库迁移往返与 drift 均通过。
这些是发布前证据；M2 默认禁用生产签署见下一节，M3 rollout plan 仍未完成。

### Provider quarantine M2 生产默认禁用证据

2026-07-19 04:52–04:54 CST，提交
`1f12100a48df189b7829751af97a383173038d7f` 已部署。上线前备份
`/home/justin/backups/superlily/20260719-phase3-control-m2/superlily-pre-control-m2-1f12100.dump`
为 150,346,845 字节，SHA-256 为
`02a8e7591f64935c8dc2c80d94367115ef2662006d2ad1ce276c5065ed67b3c9`；它已在独立
PostgreSQL 17 中实际恢复出 `0015b`、386,440 条 source event 和预期的工具/控制面
计数。临时容器已删除，主机备份保留。

生产启动日志记录 `0015b_descriptor_mutations -> 0015c_provider_quarantine`，head/no
drift 均通过。Core 与 Provider 镜像分别为
`sha256:83e338743719da8d5534a76322792a8f06fcbd4cf758625c26ff5395a8d51504` 和
`sha256:db13bb712ea72c3edf729a053d51b696e5d070131ff0eb262ce4d12636dcec8d`，
镜像依赖检查通过。Git-bound 导入的 `status.inspect@1.0.2` 与旧两版均为
`reviewed/resource_version=1`；Provider 为 `active/resource_version=1`，没有新增
lifecycle mutation。最新 inventory/heartbeat 精确匹配 `1.0.2` 的 descriptor 与
implementation hash、hard budgets、healthy 和 0/1 并发。

8 个控制面/descriptor/Provider 只追加或 authority trigger 均存在。operator/pepper
未配置，Host/Origin 为空数组；M2 preview 返回带安全响应头的 503。探测后 5 张控制面
表仍全零。生产继续为 `ledger_only`、global stop=false、空 canary/enforce scopes、
1 条既有 invocation、0 attempt/event。因此只签署 M2 默认禁用上线，不签署真实
Provider mutation、descriptor activation 或 canary；M3 仍是下一 authority 门。

- [x] `off`, `ledger_only`, exact `canary`, and reviewed `enforce` semantics are
  tested. Canary binds tool/version/hash, conversation, caller and provider;
  enforce uses its own exact allowlist.
- [x] Global stop, per-tool/version suspension and provider quarantine each
  independently prevent new leases in contract/API tests; production drills
  remain part of the unchecked operations gate below.
- [x] Migration `0014_tool_invocations` and every legal/illegal transition,
  idempotent create, cancellation, deadline and append-only invariant pass both
  databases. `ledger_only` creates no executable lease.
- [x] Migration `0015_tool_attempts` proves one active lease, monotonically new
  fences, provider/attempt-secret binding and DB-time authority.
- [x] Duplicate, replayed, late and stale-fence start/heartbeat/complete/fail are
  rejected without mutating the current invocation and remain auditable.
- [ ] Restart, reaper crash, lease expiry, cancellation race, provider/Core
  outage, clock skew, invalid output, safe retry, unknown completion and queue
  starvation have deterministic tested outcomes.
- [ ] Input/output, rate, concurrency, wall-time, CPU, memory and byte budgets
  are enforced or make the provider ineligible; best-effort is never labeled
  hard.
- [ ] Network, filesystem, subprocess, secret, sandbox, remote-fetch and
  artifact permissions are machine-readable, non-escalatable arguments.

## Artifacts and first tools

- [ ] Migration `0016_tool_confirmations_artifacts` passes both databases and
  reservation/upload/finalize state is immutable/idempotent.
- [ ] Upload secrets are one-use/provider-attempt-fence-bound. Core enforces
  expiry, count, bytes, MIME/hash/dimensions and quarantine before atomic
  finalize; late/failed/orphan cleanup is tested.
- [ ] `status.inspect` passes registry with execution off, then ledger-only,
  exact canary, fault/rollback, stable evidence and old-command compatibility.
- [ ] Text-only `wolfram.run` passes worker recovery and resource/error gates;
  image output waits for finalized artifacts.
- [ ] `latex.render` accepts only finalized content-addressed artifacts and
  passes malicious TeX, timeout, MIME/hash/size, cleanup and renderer-boundary
  tests.
- [ ] No provider sends a platform message; command parsing, invocation,
  execution, result, rendering and delivery remain separately observable.
- [ ] Existing command paths remain rollback until per-tool shadow/canary
  equivalence, latency, errors, budgets and evidence window are signed.

## 控制面、安全与运维

- [x] M0 服务端短会话、Secure cookie、CSRF/Origin/Host/内容类型、数据库时间过期、
  再认证/退出 CAS、限速、CSP、脱敏校验错误和只追加会话审计在两种数据库通过。
- [x] M1 reviewer descriptor lifecycle 的服务端 preview、CAS、幂等、反向回滚、
  runtime 重算和只追加证据在两种数据库通过，并已完成生产默认禁用签署。
- [x] M2 security_admin Provider quarantine/restore、runtime 恢复门、lease 行锁、
  数据库 authority trigger 与默认禁用生产部署均已签署。
- [ ] 只读面板的 desired/reported/effective/actual 数量和 reason code 与直接 API/SQL
  证据一致；第三阶段不在线编辑 descriptor 内容。
- [ ] M3 的 operator/break-glass 权限矩阵、Git-bound rollout plan、敏感读取审计和
  有界导出测试在任何 canary authority 启用前通过。
- [ ] 浏览器存储、日志、URL、工具输入/结果、artifact 和导出证据中均无 bearer token；
  Provider/bot/admin credential 相互独立，并完成轮换/撤权测试。
- [ ] 记录生产备份/恢复、head/drift、镜像/提交/config 哈希、停止开关、
  Provider/Core/数据库故障和回滚演练。

## Phase 3 exit

- [ ] `status.inspect`, `wolfram.run`, and `latex.render` use the common
  descriptor/invocation/provider/artifact protocol with stable signed canaries.
- [ ] Natural-language callers remain disabled; no write/admin tool is enabled.
- [ ] All exceptional rows and security/retention findings are explained, docs
  and code are committed, and the operator signs the Phase 3 evidence record.
