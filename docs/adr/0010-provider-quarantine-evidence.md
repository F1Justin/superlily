# ADR 0010：Provider quarantine 的并发边界与恢复证据

- 状态：accepted
- 日期：2026-07-19

## 背景

M1 已把 descriptor lifecycle 收进服务端 preview、CAS、幂等与只追加证据，但
Provider 仍可被应用代码或直接 SQL 改写 lifecycle。若 quarantine 与 lease 领取并发，
只在 Registry 视图里检查一次状态会留下竞态：旧事务可能在 quarantine 接受后才创建
新 lease。另一方面，quarantined Provider 仍需要上报 inventory/heartbeat，才能为
恢复提供新鲜证据，不能把 quarantine 等同于 credential 撤销。

已经部署的 `0015b_descriptor_mutations` 不得修改；`0016` 继续保留给
confirmation/artifact。

## 决定

1. 新增线性迁移 `0015c_provider_quarantine`，为 `tool_providers` 增加单调
   `resource_version`，并保护稳定注册字段、lifecycle event 与 Provider 行不被删改。
2. 首包只允许 `active -> quarantined` 与 `quarantined -> active`。quarantine 是降低
   authority，可以在 runtime 不健康时执行；恢复是增加 authority，必须重新验证：
   credential active、最新 inventory 新鲜、最新 heartbeat 新鲜且 healthy、两者
   inventory hash 一致、协议仍在 allowlist，inventory 至少包含一个带明确
   implementation hash 的条目。
3. 只有 `security_admin` 可 preview/apply。apply 必须使用同一 session 的未过期
   canonical preview、新鲜再认证、expected resource version、精确幂等键和有界原因，
   并在提交前重新计算 runtime。
4. quarantined Provider 可继续用独立 Provider credential 上报 inventory/heartbeat，
   但不得领取新 lease。credential revoke 是另一条 authority，不由本包在线修改。
5. lease 路径必须先锁定精确 Provider 行并确认 lifecycle=active；quarantine apply 使用
   同一行锁。这样接受 quarantine 前已经取得锁的 lease 可以先完成，接受之后不能再
   产生新 lease。M2 不伪装成正在运行 attempt 的强制终止器。
6. preview 明确列出 Provider authority、最新 runtime、受影响工具及 before/after；
   接受和拒绝均写入既有 mutation/audit 账本。恢复是一条新 mutation，不删除
   quarantine 证据。

## 后果

Provider quarantine 成为独立于 global stop 和 descriptor suspension 的第三个可审计
停止开关。它只控制新 lease，不改变 descriptor authority、不展示或旋转 credential，
也不单独授权 canary。M3 reviewed rollout plan 完成前，生产继续保持 `ledger_only`。

## 必需证据

- 两种数据库的 quarantine/restore、角色、CSRF、再认证、preview expiry/runtime drift、
  CAS、幂等、限速、secret 扫描和直接 SQL 拒绝；
- lease 与 quarantine 的并发测试，证明 quarantine 接受后不创建新 lease；
- quarantined 状态仍可接收认证 inventory/heartbeat，恢复必须使用新鲜且相互绑定的
  runtime；
- 默认空 operator 配置的生产迁移、备份恢复、head/drift、零 mutation 和零 attempt。
