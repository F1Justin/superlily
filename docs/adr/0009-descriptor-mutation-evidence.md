# ADR 0009：descriptor mutation 的持久 preview 与版本证据

- 状态：accepted
- 日期：2026-07-19

## 背景

ADR 0008 的 M0 已作为 `0015a_control_plane_auth` 部署。M1 如果只复用可变的
`tool_descriptors.lifecycle`，就无法在 SQLite 与 PostgreSQL 同时证明旧 preview、
并发 apply 和直接 SQL 视图的一致性；如果把 preview 完全交给客户端，又无法由
数据库时间裁定过期。

已经部署的 `0015a` 不得修改。ADR 0007 冻结给 confirmation/artifact 的 `0016`
也不得被抢占。

## 决定

1. 新增线性迁移 `0015b_descriptor_mutations`，其 down revision 为
   `0015a_control_plane_auth`；未来 `0016_confirm_artifacts` 接在第三阶段
   最后一个 `0015x` 治理迁移之后。
2. `tool_descriptors` 增加单调 `resource_version`。每个 lifecycle 更新必须恰好加一，
   并且同一事务已追加匹配 sequence、before/after 的 lifecycle event。
3. descriptor authority 字段和 descriptor 行禁止 UPDATE/DELETE；只允许首包定义的
   `reviewed -> active`、`active -> suspended`、`suspended -> active`。lifecycle event
   禁止 UPDATE/DELETE。
4. 新增 append-only `control_plane_previews`，保存 session、actor、operation、精确
   target、request hash、expected version、canonical preview/hash 和数据库过期时间。
   apply 必须引用同一 session 的未过期 preview，并重新计算当前 preview hash。
5. preview 不授予 authority。只有 reviewer 的新鲜再认证 session、CSRF、精确
   Host/Origin、幂等键、bounded reason、expected version 和相同 preview 同时成立，
   才能提交 lifecycle mutation。
6. 接受与拒绝都写入 `control_plane_mutations` 和 append-only audit；幂等键复用但
   request hash 不同只追加冲突审计，不创建第二条同键 mutation。

## 后果

M1 可以在不开放 descriptor 编辑、不依赖 Web UI、也不修改工具热路径的情况下独立
验收。`reviewed -> active` 的安全回退是新的 `active -> suspended` mutation，而不是
删除 activation 或退回 `reviewed`。M1 默认禁用部署后仍不等于 canary；M2 Provider
quarantine 与 M3 reviewed rollout plan 尚未完成时，工具执行继续保持
`ledger_only`。

## 必需证据

- 两种数据库的 preview expiry、角色、新鲜再认证、CSRF、CAS、并发、幂等重放/
  冲突、runtime drift 与 secret 扫描；
- descriptor authority/lifecycle event/preview/mutation/audit 的 UPDATE/DELETE 拒绝；
- activation、suspension、restore 的 before/after/API/SQL 一致和反向 mutation；
- 默认空 operator 配置的生产迁移、备份恢复、head/drift、零 mutation 和零 attempt。
