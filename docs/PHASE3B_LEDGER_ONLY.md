# Phase 3b：调用账本与 `ledger_only` 边界

## 本切片解决什么问题

`0014_tool_invocations` 先回答“谁在什么上下文下，针对哪一份精确工具权威，提出了什么结构化调用；当时为什么只能记录或必须拒绝”。它不回答“由哪个 Provider 执行”，也不创建 queue、attempt、lease、fence、确认记录或 artifact。

这一步刻意把工具提案与工具执行分开。模型、命令或管理员入口即使能够生成合法提案，也不能因为账本已经存在就获得执行权。

## 线上模式

- `off`：调用创建被拒绝，不新增账本行；已经存在的同幂等请求仍可安全重放读取。
- `ledger_only`：经过精确 descriptor/hash 与输入 schema 校验的请求进入账本，但最终只会成为 `recorded_only` 或 `rejected`。
- `canary`、`enforce`：`0014` 的配置层明确拒绝这两种模式；它们必须等 `0015_tool_attempts` 的 lease、fence、Provider 绑定和硬预算执行器通过验收后才能出现。

无论当前模式如何，`0014` 都没有 Provider lease 路由，`leases_enabled=false`，自然语言 caller 也没有认证入口。

## 身份与 caller

调用身份从 bearer credential 推导，客户端不能自报 caller：

| credential | 固定 caller | 账本主体 |
|---|---|---|
| Lily/Nekro ingest token | `command` | 对应的 `instance_id` |
| Core admin token | `admin_api` | `core-admin` |
| Provider token | 无 | 拒绝 |

命令 caller 只能读取和取消自己创建的调用；管理员可以读取所有调用。共享 admin token 还不能区分具体操作员，因此本切片不开放 descriptor 激活、Provider quarantine、canary 或其他危险控制面变更。

`principal` 中的平台、sender、conversation、roles、source event、decision、claim 和 entry 只是经过认证 caller 提交并冻结的事实快照，不会反过来扩大权限。当前只允许公开工具通过权限门；`trusted`、群管理员、超级用户和服务权限要等正式身份策略。

## 创建与幂等

创建请求必须固定：

- `tool_id`、descriptor version 与 descriptor SHA-256；
- 满足受限 JSON Schema 的结构化输入；
- principal 与 capability 快照；
- 8–256 字节的 `Idempotency-Key`。

Core 使用共享 RFC 8785 canonicalization 计算 request、input、principal、capability、policy 与 transition evidence 的哈希。同一认证主体复用同一幂等键和相同请求时返回原 invocation；复用键但改变任意请求内容时返回 `409`。并发重复创建依靠数据库唯一约束收敛成一条 invocation 和两条初始 transition。

无法归一化、descriptor 不存在、hash 不匹配或输入 schema 不合法的请求在进入账本前拒绝，避免把未经审阅的任意输入保存成看似有效的工具调用。caller、权限或能力门不通过时，请求本身已经合法，因此账本会记录 `proposed -> rejected` 及稳定原因。

合法的 `ledger_only` 请求记录为 `proposed -> recorded_only`。当前 descriptor 未激活、Provider 不健康、预算不完整或 global stop 等执行资格原因仍会进入 policy snapshot，但不会丢掉影子提案。policy 明确写入：

- `eligible_if_execution_enabled`：若未来处于可执行模式，当前资格门是否通过；
- `queue_created=false`；
- `lease_created=false`。

## 状态机与持久化

共享契约枚举完整状态与事件，并对全部 `(event, from, to)` 组合做接受/拒绝矩阵测试。终态没有出边。`0014` 的公开创建只产生 `rejected` 或 `recorded_only`，其余状态为后续 migration 的同一状态机预留，并供取消、deadline 和恢复测试使用。

`tool_invocations` 保存当前状态和不可变请求快照；`tool_invocation_transitions` 按 invocation/sequence 追加历史。SQLite 使用两个触发器禁止 transition 的 UPDATE/DELETE；PostgreSQL 使用一个触发函数和 trigger 实现同一约束。升降级会正确创建和清理这些对象，不留下孤立函数。

状态更新使用 invocation 当前 state 与 transition sequence 的 compare-and-swap。同一 queued invocation 的并发取消只有一个请求能成功；另一个看到终态后返回 `409`。终态不能被取消或改写。

## 数据库时间与 reaper

created、policy evaluated、deadline、terminal 和 transition 时间以数据库 `CURRENT_TIMESTAMP` 为权威。调用方不能提交 deadline。受限批量 reaper 按 deadline/created 排序并在 PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`：

- `awaiting_confirmation -> expired`；
- `queued -> timed_out`；
- 过期 `proposed -> rejected`；
- `leased`、`running`、`cancel_requested` 或 `lease_expired` 在没有 attempt/fence 证据时保守进入 `unknown_completion`。

它不会把状态不明的执行伪装成成功、失败或已取消。

## HTTP 表面

- `POST /v1/tool-invocations`：认证创建或幂等重放；
- `GET /v1/tool-invocations/{invocation_id}`：caller-scoped 或管理员读取；
- `POST /v1/tool-invocations/{invocation_id}/cancel`：只允许合法的非终态转换。

没有 `/v1/tool-executions/lease`、start、heartbeat、complete 或 fail 路由。Provider SDK 仍只有 inventory/heartbeat 报告能力。

## 进入 `0015` 前的门

只有以下证据同时成立，才能开始可执行切片：

1. Lily/Nekro reporter 与 heartbeat 的监督、自恢复和线上新鲜度稳定；
2. SQLite 与 PostgreSQL 的契约、API、并发、append-only、reaper 和 migration 往返全部通过；
3. 生产备份、`0014` 升级、drift、真实 `recorded_only` 行和零 queue/lease 证据完成；
4. descriptor 仍未因 runtime discovery 自动激活；
5. `0015` 另行实现 attempt secret、单活动 lease、单调 fence、DB-time expiry、硬 wall-time/bytes 预算与三个独立 stop，不能复用 `0014` 的“只记账”进程假装执行。

## 2026-07-19 生产签署

上述前四项已形成生产证据：Core 已到 `0014_tool_invocations` head 且无
schema drift；真实 `status.inspect` 提案只产生 `propose -> record_only`；生产没有
attempt 表与 lease 路由；幂等重放、Provider 越权拒绝和 descriptor 仍为 `reviewed`
均经过验证。Lily/Nekro bridge 0.5.1 心跳显示两个 reporter worker 均在运行、
重启数与异常数均为零。SQLite 与 PostgreSQL 17 全量套件各 279 项通过。

因此可以开始 `0015` 的设计与实现，但这不等于授权任何工具执行；在
attempt/lease/fence/硬预算及独立 stop 全部验收前，`canary` 和 `enforce` 仍必须被
配置层拒绝。

## 后续状态

2026-07-19，`0015_tool_attempts`、Provider execution SDK 与
`status.inspect@1.0.1` 硬边界执行器已经完成实现，并在 SQLite/PostgreSQL 17 各
通过 313 项测试。`0014` 本文继续作为“当时生产只有账本、没有执行面”的历史签署，
不回写成后来能力的说明。

`0015` 的模式、精确范围、lease/fence/secret、恢复语义、执行器限制、部署与回滚
见 `PHASE3B_EXECUTION.md`。生产升级仍先保持 `ledger_only`；schema 与路由存在不
代表 descriptor 已激活或 canary 已授权。
