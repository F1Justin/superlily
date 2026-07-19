# Phase 3：`status.inspect` 故障矩阵与生产演练边界

## 目的

本文件把第三阶段退出门中的“可恢复”拆成可重复、可审查的故障事实。它只验证
Core、Provider SDK、独立 Provider、PostgreSQL 和控制面的执行协议，不规定模型遇到
什么问题应选择什么工具，也不扩大自然语言、群聊发送或工具集合。

截至本批实现开始时，Provider SDK 和真正的 `status.inspect@1.0.2` 已经完成，并已
通过一次无平台发送的生产成功 canary；“下一步开始 Provider SDK/status.inspect”是
历史计划，不再是当前状态。本文件后半同时记录异常路径和稳定窗口现已完成的生产
证据；`0016_confirm_artifacts` 现已完成实现与双数据库回归，当前等待默认关闭的生产
迁移签署。

## 权限边界

所有生产演练统一满足：

- 只调用只读、无网络、无文件、无 artifact、无 secret 的
  `status.inspect@1.0.2`；
- caller 固定为 `admin_api`，canonical conversation 固定为
  `qq:group:1080353942`，不创建平台 response；
- 每个场景使用独立的 Git-bound plan，最多消费 1 条 executable invocation，
  一次只允许一份 plan 为 `active`；
- plan 激活与暂停仍经过 operator/break-glass 控制面、preview、重认证、CAS、
  幂等键和只追加审计；故障驱动器不能自行增加 authority；
- 常驻 status Provider 在手工协议演练期间必须停止，避免它先领取唯一 lease；
  驱动器要求显式 `--provider-stopped-ack`，但该参数不替代进程检查；
- Admin/Provider credential 只通过环境传入，不出现在 argv、计划、日志或证据 JSON；
  驱动器读取后立即从进程环境删除，独立 status worker 又使用显式空白安全环境；
- 每项结束先暂停 plan，再恢复默认 Core 与常驻 Provider。不得删除 invocation、
  attempt、`unknown_completion` 或拒绝事件来“清理”结果。

这组演练不会打开 `enforce`、自然语言 caller、命令迁移或模型工具循环。它验证的是
模型无论将来怎样自主规划，都不能越过的执行底座；因此不会与渐进式披露或
the bitter lesson 冲突。

## 为什么默认配置不能证明 safe retry

`status.inspect@1.0.2` 的 invocation deadline 是创建后 5 秒。生产默认
`tool_lease_seconds=15`，Core 实际写入：

```text
lease_expires_at = min(invocation.deadline_at, database_now + lease_seconds)
```

所以默认 lease 直接截止于 5 秒 deadline。它过期时已经没有剩余时间重新执行，
retry-safe 调用只能终止为 `timed_out`。要真实证明“Provider 中断后安全重试”，该
场景必须使用临时 `tool_lease_seconds=1` 的 canary Core：第一 attempt 约 1 秒过期，
reaper 在 deadline 前重排队，第二 attempt 获得新 fence 并完成。演练结束立即恢复
默认 `ledger_only/15 秒` 配置。

这不是调大重试次数，也不是让 SDK 盲目重发。Provider SDK 对 lease/start/
heartbeat/complete/fail 仍只发送一次；只有 Core 在数据库中明确判定旧 lease 已过期、
descriptor 为 `retry_safe` 且 deadline 尚未到时，才允许产生新 attempt。

## 分层故障矩阵

| 场景 | 生产方式 | 预期账本结果 | 证明点 |
|---|---|---|---|
| Provider 在已开始 attempt 中断 | 1 秒 lease，等待 reaper，第二次领取并真实执行 | attempt 1=`lease_expired`，attempt 2=`succeeded`，invocation=`succeeded` | safe retry 只由 Core 发起；attempt/fence 单调 |
| 旧 worker 与重复完成 | 随上一场景发送旧 fence start/complete，并重复第二次 complete | 三次均 HTTP 409；成功终态不变，拒绝事件追加 | fence、secret 和状态共同封住迟到/重放 |
| 非法输出 | Provider 完成时提交缺字段结果 | invocation/attempt=`failed`，reason=`invalid_output`，output 为空、hash 保留 | schema fail closed，不把错误正文当成功结果 |
| Provider 时钟快/慢 | heartbeat 分别自报 2099 与 1970，再真实完成 | `succeeded`；lease 永不超过数据库 deadline | 自报时间只作诊断，DB 时间是 authority |
| Provider 明确确认取消 | start 后 cancel，heartbeat 观察，Provider 回 `cancelled` | `cancelled/provider_acknowledged_cancellation` | 正常取消不是未知结果 |
| 完成与取消竞态 | worker 先算出结果，Core 记录 cancel 后才收到 complete | `unknown_completion/completion_raced_cancellation`，output 不发布 | 不猜“取消赢”或“完成赢” |
| 取消未确认 | start、cancel 后 Provider 静默到 lease 过期 | `unknown_completion/cancellation_unacknowledged` | 不把失联误记为 cancelled |
| Provider 在领取前不可用 | 首轮 Provider stop/quarantine 证据 | 零 attempt，queued 最终 `timed_out` | 未执行与执行后失联严格分开 |
| Core 中断 | start 后停止/重建 Core，越过 deadline 后恢复 | retry-safe 调用 `timed_out`，旧 attempt=`lease_expired` | 状态在 PostgreSQL，重启后 reaper 收敛 |
| PostgreSQL 中断 | start 后短停数据库，恢复后等 Core/reaper | 同上；C0-D spool 最终 pending/gap=0 | DB 故障不制造第二执行，采集链路可恢复 |

以下内容只由协议/双数据库测试证明，不在本批生产中人为扩大风险：

- 并发 lease 只有一个胜者、Provider quarantine 与 lease 的 PostgreSQL 行锁、
  rollout pause 与 lease 的行锁；生产首轮 stop 已有直接边界证据；
- rate/concurrency、错误 Provider、错误 secret、非法状态和 attempt 事件 append-only；
- 非 `retry_safe` 工具的 `ambiguous_lease_expiry`。当前唯一生产工具本来就是
  `retry_safe`，不能篡改 descriptor 冒充另一类工具；等首个真实非安全重试工具出现
  后再做对应 canary；
- 大规模公平性、饥饿和吞吐压测。本批每个 plan 只有一条调用，不能用扩大调用量的
  方式假装证明调度公平；当前只验证数据库排序、并发唯一性和硬上限。

## 可复用驱动器

正式实现位于
`apps/core/src/superlily_core/phase3_status_fault_driver.py`，源码树入口为
`scripts/phase3_status_fault_driver.py`，安装后的入口为
`superlily-phase3-status-fault`。它支持六个单调用场景：

```text
retry-fence-success
invalid-output
clock-skew-success
cancel-acknowledged
cancel-completion-race
cancel-unacknowledged
```

运行前必须由另一条受审路径完成：备份/恢复验证、计划导入、控制面激活、常驻
Provider 停止和 Core 配置核对。调用形式如下；真实 credential 只进入环境，操作
期间不得开启 shell trace：

```bash
.venv/bin/python scripts/phase3_status_fault_driver.py \
  retry-fence-success \
  --expected-plan-id status-inspect-retry-fence-20260719 \
  --expected-plan-hash 646f19f2d037a8925f98eafdc4f356bd6b0dbc6ba27e1f67288faaa46df5f5b9 \
  --run-id retry-fence-001 \
  --provider-stopped-ack
```

驱动器在创建 invocation 前会重新发布精确 inventory/heartbeat，并验证：Core 处于
`canary`、唯一 active plan ID/version/hash、单次上限和未消费状态精确匹配、lease
已启用、descriptor hash/lifecycle 和 runtime eligibility 精确匹配。它不会激活/暂停
plan 或操作容器；失败时 plan 的
`finally` 回收仍由外层运维编排负责。

输出只包含 invocation ID、终态、reason、精简 transition 和 attempt ID/number/
fence/state/error/output hash。lease secret、bearer、输入、输出正文、控制面 cookie 和
原始 HTTP body 都不输出。

Core 与数据库中断场景由外层运维编排完成，因为把 `docker stop/start` 权力放进
协议驱动器会混淆“执行 Provider 权限”和“宿主机运维权限”。外层只在精确 plan
激活、常驻 Provider 停止、invocation 已 start 后制造一次短中断；恢复后通过 Admin
读取账本，不在故障期间自动重放 complete/fail。

## 每项生产顺序

1. 核对工作树/提交、当前 `0015d` head/no drift、Core/Provider/PostgreSQL/Lily/Nekro
   健康、durable spool 无 pending/quarantine/gap。
2. 在 PostgreSQL 17 做自定义格式备份，验证 0600、SHA-256、`pg_restore --list`，
   并在独立磁盘卷完整恢复；只验证 list 不算恢复演练。
3. 从包含计划文件的精确完整 Git commit 导入为 `reviewed`；核对 plan hash、
   `max_invocations=1`、descriptor rv4、Provider rv3 和未消费。
4. 启动带随机、内存内 operator verifier 的临时 canary Core；一次只激活一份 plan。
5. 停止常驻 status Provider，确认无活动 attempt，再运行精确场景。retry 场景额外
   要求 `tool_lease_seconds=1`；其他场景保持默认 15 秒。
6. 读取 API 与直接 SQL：invocation transition、attempt/fence、attempt event、plan
   counter、control mutation/audit；检查 responses 零新增和无平台发送来源。
7. 无论场景成功、断言失败还是网络异常，都先由 break-glass 暂停 active plan，
   再恢复默认 `ledger_only/global_stop=false/lease_seconds=15` Core、启动 Provider、
   logout/revoke 会话。
8. 最终核对无 active plan、无 leased/running attempt、控制面默认 503、Provider
   heartbeat 新鲜、Lily/Nekro spool 收敛。异常行保留并解释，不直接 SQL 修饰证据。

## 生产验收门

本故障矩阵只有同时满足以下条件才可签署：

- 八份 plan 全部来自完整 Git commit，均消费不超过 1，最终全部 paused；
- 六个驱动器场景得到上表精确终态；旧 fence/重复操作只追加 reject，不改成功行；
- Core 与 PostgreSQL 短中断恢复后无 active attempt，reaper 终态与数据库契约一致；
- 两个预期 `unknown_completion` 有明确 scenario/reason/attempt 证据，不被告警脚本
  当作普通失败自动重试，也不被删除；
- 中断窗口前后的 C0-D watermark 连续，pending/quarantine/gap 归零；
- responses 和 QQ 发送证据为零，descriptor/Provider 仍为 rv4/rv3；
- Core 恢复 `ledger_only`，常驻 Provider 健康，operator/Host/Origin/pepper 清空，
  所有新控制会话显式 logout/revoked；
- SQLite 与 PostgreSQL 17 全量回归、迁移 head/drift 和代码审查通过，生产证据写入
  `PHASE3_ACCEPTANCE.md` 与 `DEPLOYMENT.md`。

## 生产结果与稳定窗口

2026-07-19 07:45–07:46 CST，提交
`7f509e96213a2eefcd9af6fee4aea86115abb71f` 中的八份计划已全部在线执行。计划从
同一完整 commit 导入为 `reviewed/rv1`，每次只激活一份；结束后均为
`paused/rv3`、消费 1/1。六个协议场景与两个基础设施中断的实际终态为：

- safe retry：attempt 1=`lease_expired/fence=1`，attempt 2=`succeeded/fence=2`，
  invocation=`succeeded/provider_completed`；旧 fence 与重复完成留下 3 条
  `reject/attempt_state_conflict`，没有改变成功终态；
- 非法输出=`failed/invalid_output`，快慢 Provider 时钟场景=`succeeded`；
- 明确取消=`cancelled/provider_acknowledged_cancellation`；完成竞态与取消未确认
  分别为 `unknown_completion/completion_raced_cancellation` 和
  `unknown_completion/cancellation_unacknowledged`；
- Core 与 PostgreSQL 各短停一次，恢复后均由 reaper 收敛为 attempt=
  `lease_expired`、invocation=`timed_out/deadline_expired`，没有第二次执行。

直接 SQL 最终得到 14 条 invocation：1 recorded_only、6 timed_out、3 succeeded、
1 failed、1 cancelled、2 unknown_completion；10 个 attempt 和 36 条 attempt event，
无 active plan/invocation/attempt。两条临时控制会话均 logout/revoked；16 份 preview、
16 笔 lifecycle mutation 和 24 条 plan lifecycle event 全部保留。演练窗口内
`responses` 为零新增。

演练前备份为
`/home/justin/backups/superlily/20260719-phase3-fault-matrix/superlily-pre-fault-matrix-7f509e9.dump`，
大小 150,886,660 字节、权限 0600、SHA-256
`8dc4f145066a58bf7a633501934814ff15a59cb9e74642d94e8836c4d4bb20ab`。
它不只通过 `pg_restore --list`，还在独立 PostgreSQL 17 磁盘卷中以零错误完整恢复；
临时容器和卷已删除，主机备份保留。

恢复后的首轮日志暴露出一个不影响账本、但妨碍稳定签署的噪声：Provider 空 lease
退避上限和 Uvicorn keep-alive 都为 5 秒，偶发 `ReadError`。提交 `2b31c6b` 让空
lease 轮询显式关闭连接，真实 start/heartbeat/complete 仍复用连接。修正版 Provider
镜像 `sha256:b14bdcec3ceb921fa07830016620a5648b116e55e142fcde29c7443f25cc1f9b`
上线后跨过完整 5 分钟 inventory 周期，得到 2 份 inventory、11 次 healthy heartbeat、
零 warning/error 和零重启。此时 Core/Provider/PostgreSQL 资源平稳，Lily/Nekro
均 online，两个 spool 均 pending=0、quarantine=0、gap=null；迁移仍为
`0015d (head)` 且无 drift，旧命令 Registry 有 1 份 fresh snapshot、18 条静态规则，
没有 stale snapshot。

## 后续顺序

故障矩阵和 `status.inspect` 稳定窗口已签署，但仍不扩大模型 authority。
`0016_confirm_artifacts` 已完成本地签署，下一步是默认关闭的生产迁移；随后是文本模式
`wolfram.run`，图像输出和 `latex.render` 必须等待内容寻址 artifact 的
reserve/upload/finalize/cleanup 边界。自然语言工具选择仍在 Phase 5。
