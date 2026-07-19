# ADR 0008：第三阶段最小 mutation 治理门

- 状态：accepted
- 日期：2026-07-19

## 背景

`0015_tool_attempts` 与 `status.inspect@1.0.1` 已在生产以 `ledger_only` 安全
空转，但 descriptor 仍为 `reviewed`。ADR 0005 明确禁止在角色、服务端会话、
重认证、CAS、幂等、审计和回滚证据缺失时执行 activation、suspension、Provider
quarantine 或 canary mutation。直接 SQL 虽然技术上可行，却会绕过这条已接受的
authority 边界。

ADR 0007 已将 `0016_confirm_artifacts` 固定给 confirmation/artifact，
不能为赶 canary 重用或改名。

## 决定

1. 在 `0015_tool_attempts` 与未来 `0016_confirm_artifacts` 之间插入唯一
   Alembic revision `0015a_control_plane_auth`。这不是改写已应用的 `0015`，也不占用
   已冻结的 `0016`；未来 `0016` 以 `0015a_control_plane_auth` 为 down revision。
2. 控制面按最小权限分包上线，不先造完整 Web Admin：
   - M0：服务端短会话、角色、Secure/HttpOnly/SameSite cookie、CSRF、精确
     Origin/Host、登录限速、短期重认证和只追加审计；默认无 operator 配置即禁用。
   - M1：reviewer 可对精确 descriptor lifecycle 做 preview/CAS/idempotent mutation；
     descriptor 内容仍只能来自 Git-bound import。
   - M2：security_admin 可对精确 Provider 做 quarantine/restore；凭据值不可见。
   - M3：Git-bound reviewed rollout plan 与 operator apply/pause；canary 范围继续精确
     绑定 tool/version/hash/conversation/caller/provider，不允许自由文本或通配符。
3. mutation 请求必须包含 bounded reason、Idempotency-Key、expected resource
   version 和 server-computed preview hash；重放相同请求返回原结果，复用键但改变
   请求返回冲突。
4. 接受和拒绝的 mutation 都写入 append-only audit，保存 before/preview/after 的
   canonical hash、actor、role、session、目标、原因和 outcome；不保存密码、cookie、
   CSRF、Bearer token、lease secret 或原始异常。
5. global stop 仍保留环境级紧急上限；控制面或面板不可成为 ingestion、claim、lease
   或 emergency stop 的 correctness 依赖。
6. 首个生产 canary 必须等 M0–M3、双数据库测试、备份恢复和三种独立 stop 的真实
   演练都通过；禁止用直接 SQL 代替任何一步。

## 后果

第三阶段的下一包是 authority 治理，不是 Agent tool loop，也不是通用 Web Admin。
只读 Registry/API 继续可用；没有配置 operator credential 时，新 session/mutation
表即使存在也不会开放 mutation 权限。

`0016_confirm_artifacts` 的名称和职责保持不变，只把 down revision 接到
`0015a_control_plane_auth`。若 M0–M3 尚未签署，`status.inspect` 继续
`ledger_only`，不会因执行器已经存在而自动激活。

## 必需证据

- SQLite/PostgreSQL fresh upgrade、downgrade/re-upgrade、append-only trigger 和
  Alembic drift；
- cookie/CSRF/Origin/Host/content-type/session expiry/reauth/rate-limit 测试；
- 每个角色的允许/拒绝矩阵，以及跨角色、旧 preview、旧 version、重复/冲突
  Idempotency-Key 测试；
- before/after/API/直接 SQL 视图一致，secret 扫描为空；
- Core/Provider/数据库中断、并发 mutation、回滚 mutation 与审计不可变测试；
- 生产备份恢复、镜像/配置哈希、默认禁用和零意外 lease 证据。
