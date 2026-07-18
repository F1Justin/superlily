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
各 311 项通过；测试覆盖四种模式、canary/enforce 独立精确范围、三个 stop、
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

## Control panel, security, and operations

- [ ] Read-only panel desired/reported/effective/actual counts and reason codes
  match direct API/SQL evidence; descriptor content is not edited in Phase 3.
- [ ] Auditor/operator/reviewer/security-admin/break-glass boundaries, short
  server-side sessions, CSRF/Origin, reauth, CAS, idempotency, preview, CSP,
  redaction, export and append-only audit tests pass before any mutation.
- [ ] No bearer token is in browser storage, logs, URLs, tool inputs/results,
  artifacts or exported evidence. Provider/bot/admin credentials are distinct
  and rotation/revocation is tested.
- [ ] Production backup/restore, head/drift, image/commit/config hashes,
  kill-switch drill, provider/Core/database outage and rollback are recorded.

## Phase 3 exit

- [ ] `status.inspect`, `wolfram.run`, and `latex.render` use the common
  descriptor/invocation/provider/artifact protocol with stable signed canaries.
- [ ] Natural-language callers remain disabled; no write/admin tool is enabled.
- [ ] All exceptional rows and security/retention findings are explained, docs
  and code are committed, and the operator signs the Phase 3 evidence record.
