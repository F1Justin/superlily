# Phase 3：最小控制面 mutation 设计

## 目标与非目标

本设计只解决首个精确工具 canary 前缺失的 authority 治理门。目标是让“谁以什么
角色、基于哪个旧版本、看过什么 preview、为什么把哪个精确资源改成什么状态”成为
可验证事实。

本包不提供 descriptor 在线编辑、任意 SQL、secret 查看、群聊消息浏览、完整 Web
Admin、自然语言 tool calling 或写操作工具。控制面不可参与工具执行热路径。

## 当前实现状态

2026-07-19，M0 已完成代码、双数据库验收和默认禁用的生产部署。它新增服务端短
会话、离线 scrypt verifier 生成器、精确 Host/Origin/JSON 边界、Secure/HttpOnly/
SameSite cookie、内存态 CSRF、数据库时间过期、再认证/退出 CAS、登录限速、scrypt
并发上限、脱敏 422、安全响应头和只追加登录/审计证据。

同日 M1 descriptor lifecycle 已完成实现、审查与发布前回归。新增
`0015b_descriptor_mutations`、持久 preview、单调资源版本、reviewer 权限、短时
preview、apply 新鲜再认证、CAS、幂等重放/冲突、运行时重新计算、接受/拒绝审计，
以及数据库层 descriptor authority、lifecycle event 和 preview 不可变约束。首包只
允许 `reviewed -> active`、`active -> suspended`、`suspended -> active`；任何直接
改 lifecycle 但没有同事务匹配 event 的写入都会被数据库拒绝。

SQLite 全量为 334 项通过、1 项 PostgreSQL 专用迁移测试跳过；PostgreSQL 17 分段
全量 335 项通过。两种数据库均完成 fresh upgrade、downgrade/re-upgrade、
`alembic check`、并发单一 CAS 胜者、角色/CSRF/再认证、preview 过期/漂移、幂等、
限速、secret 扫描和只追加证据验证。

M1 已于 2026-07-19 04:04 CST 默认禁用部署。生产为
`0015b_descriptor_mutations` head 且无 drift；operator/Host/Origin/pepper 仍为空，
5 张控制面表均为 0，preview 路由带安全响应头返回 503。两个 descriptor 仍为
`reviewed/resource_version=1`，工具继续为 `ledger_only`、1 条 `recorded_only`
invocation、零 attempt。

M2 Provider quarantine 也已完成实现、审查与发布前回归。新增
`0015c_provider_quarantine`、Provider 单调资源版本、security_admin preview/apply、
恢复时 inventory/heartbeat/implementation 重新验证，以及 Provider authority 与
lifecycle event 的数据库不可变约束。lease 与 quarantine 使用同一 Provider 行锁，
保证 quarantine 接受后不再创建新 lease；quarantined Provider 仍可上报新鲜 runtime
作为恢复证据。SQLite 全量 341 项通过、2 项 PostgreSQL 专用测试跳过；PostgreSQL
17 分段全量 343 项通过。M2 已于 2026-07-19 04:52–04:54 CST 默认禁用部署：生产为
`0015c_provider_quarantine` head 且无 drift，三版 descriptor 均为
`reviewed/resource_version=1`，Provider 为 `active/resource_version=1`，控制面五表
全零，工具仍为 `ledger_only` 且零 attempt。`status.inspect@1.0.2` 已按完整 Git commit
导入并由 Provider 精确报告，但没有激活。

M3 Git-bound 精确 rollout plan 已完成实现与双数据库关键回归。新增
`0015d_rollout_plans`、严格 plan/item 合同、Git-bound reviewed 导入、operator
preview/apply/pause、break-glass pause-only、单活动计划、原子调用上限和数据库
不可变约束。环境 scope 不再是 authority，`SUPERLILY_TOOL_EXECUTION_MODE` 只是
`off/ledger_only/canary` 权限上限，首包明确拒绝 `enforce`。调用创建和 lease 都会
重新验证同一个 active plan；无计划、暂停、过期、漂移或额度耗尽均安全降级成
`recorded_only`。真实生产 plan、descriptor 激活和 canary 仍未签署。

最终发布前全量回归为：SQLite 370 项通过、4 项 PostgreSQL 专用场景跳过；
PostgreSQL 17 为 374 项全部通过。它包含 fresh upgrade、downgrade/re-upgrade、
`alembic check`、严格合同、Git-bound 导入、operator/break-glass 角色矩阵、preview
漂移、CAS/幂等、无计划安全降级、资源版本阻断、调用上限唯一胜者、pause/lease
行锁、不可变 trigger 和旧功能全量回归。编译检查与 `git diff --check` 也通过。

M3 已于 2026-07-19 06:01–06:04 CST 默认禁用部署到生产 `0015d`。备份在一次
2 GiB tmpfs 空间不足后，改用空的临时磁盘卷完成零错误实际恢复；head/no drift
通过。四张 rollout 表全零、数据库 guard 完整，operator/Host/Origin/pepper 仍空；
Registry 显示无 active plan/lease，preview=503，真实 Provider lease=204，探测后
仍为 1 条 `recorded_only`、零 attempt/event 和零控制面记录。真实 authority 未开放。

## 分包顺序

### M0：会话与审计底座

新增 `0015a_control_plane_auth`：

- `control_plane_sessions`：只保存 session token/CSRF token 的哈希、operator、角色、
  issued/expiry/last_reauth/revoked 和资源版本；
- `control_plane_login_attempts`：按 operator 与加盐 client fingerprint 记录接受/拒绝，
  支持有界限速；
- `control_plane_mutations`：保存幂等键、request hash、目标、expected version、
  preview hash、before/after hash、outcome 与安全原因；
- `control_plane_audit_events`：所有 session 与 mutation 事件的 append-only 时间线。

SQLite/PostgreSQL 都用 trigger 禁止 mutation/audit 的 UPDATE 与 DELETE。session 本身
允许 CAS revoke/reauth/expiry 更新，但每次变化另追加 audit event。

operator 只从环境中的版本化 JSON 读取，credential 使用 scrypt 参数、salt 与 hash，
不保存明文。未配置 allowlisted Host、Origin、operator 或 audit pepper 时，控制面登录
返回不可用；既有 admin bearer 不能兑换成 reviewer/security_admin session。

### M1：descriptor lifecycle

reviewer 先请求精确 `tool_id + version + descriptor_hash + desired lifecycle` 的 preview。
Core 读取当前 lifecycle event sequence 作为 resource version，并计算：

- 当前 immutable authority 与来源 Git commit；
- 当前 Provider inventory/heartbeat/implementation/budget；
- mutation 前后的 desired/reported/effective 差异；
- 会增加或减少的权限和稳定 reason code；
- canonical preview hash。

apply 必须带相同 preview hash、expected sequence、原因与幂等键，并通过新鲜重认证。
首包只允许 `reviewed -> active`、`active -> suspended`、`suspended -> active`。过期
preview、hash 不同、sequence 已变或 runtime 不再满足条件都 fail closed 并审计。

### M2：Provider quarantine

security_admin 对精确 Provider 做同样的 preview/CAS 流程。首包只允许
`active -> quarantined` 与 `quarantined -> active`，不在线旋转或显示 credential。
quarantine 必须立即阻止新 lease；恢复前重新验证 inventory、heartbeat 与实现哈希。

### M3：精确 rollout plan

canary 不接受 UI 临时拼出的 scope。Git 中新增受限 rollout plan，固定：

- plan ID/version/source commit/canonical hash/reviewer；
- `canary` 或回退到 `ledger_only`，首包不开放 `enforce`；
- 每项精确 tool/version/hash/conversation/caller/provider；
- 生效与硬过期时间、最大调用数、回滚目标和 reason；
- descriptor 与 Provider 的 expected resource version。

Git-bound CLI 只能 import 为 `reviewed`。operator 可在新鲜重认证后 apply/pause 已审阅
plan；Core 从数据库读取 active plan，不依赖单个进程内存。环境 global stop 与 `off`
始终可以进一步降低权限，不能被数据库 plan 覆盖。

## HTTP 安全边界

- session cookie 名使用 `__Host-` 前缀，固定 `Secure; HttpOnly; SameSite=Strict; Path=/`；
- CSRF token 只在登录/轮换响应中出现，前端仅保存在内存，并通过专用 header 提交；
- login、reauth、preview 和 apply 都要求精确 allowlisted Host/Origin 与 JSON
  Content-Type；不接受 query token、form fallback 或跨域通配符；
- session 短时过期，危险 mutation 要求更短的 reauth freshness；logout/revoke 使用
  CAS，旧 cookie 不能复活；
- 密码、session/CSRF token、admin/provider/bot bearer、lease secret 不进入 URL、
  日志、数据库审计、preview、导出或错误正文；
- 错误响应使用稳定 reason code，不回显请求 body、哈希材料或原始异常。
- 所有控制面响应，包括认证与请求校验失败，都使用 `no-store`、`nosniff`、
  `no-referrer` 和 `default-src 'none'; frame-ancestors 'none'`。

## 并发、幂等与审计

mutation 的唯一键是 `(operator_id, operation, Idempotency-Key)`。相同 request hash
重放返回原 outcome；不同 request hash 返回 `409` 并追加拒绝审计。资源更新使用
`WHERE current_version = expected_version` 或等价行锁/CAS；并发只有一个接受者。

preview 本身不保留可执行 secret。apply 时 Core 重新计算 preview。增权操作中任何
authority、runtime、heartbeat、plan expiry 或 global stop 变化都会使旧 preview
失效；plan pause 是降权，只要精确 plan 和 resource version 未变，运行中计数变化
不能阻止暂停，审计会同时保存原 preview 和重算 hash。

审计 evidence 只保存有界结构和 canonical hash。接受记录按顺序包含 request、before、
preview、after；拒绝记录包含稳定拒绝原因和当时资源版本。rollback 是一条方向相反的
新 mutation，不删除旧记录。

## 生产启用门

1. M0–M3 在 SQLite/PostgreSQL 与 migration 往返全部通过；
2. 控制面未配置时生产行为与当前 `ledger_only` 完全相同；
3. 配置独立 operator credential，证明不等于 admin/provider/ingest secret；
4. 备份并实际恢复生产 PostgreSQL；
5. 先部署会话/read/preview，mutation 仍禁用；
6. 依次演练 descriptor suspension、Provider quarantine、scope pause 和 global stop；
7. 只激活 `status.inspect@1.0.2` 与一个 reviewed rollout plan；
8. 用 `admin_api` 创建一次无平台发送调用，验证完整 attempt 证据后立即 pause；
9. 解释所有拒绝、异常和 `unknown_completion`，再决定是否进入稳定窗口。

任何一步失败都退回 `ledger_only`。只要控制面治理没有签署，`0016` artifact、
Wolfram、LaTeX 和自然语言工具循环都不能被用来绕过首个 status canary。
