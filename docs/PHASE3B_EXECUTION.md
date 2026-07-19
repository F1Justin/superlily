# Phase 3b：可恢复执行账本与 `status.inspect` 执行边界

## 这一步提升了什么权限

`0015_tool_attempts` 把 `0014` 的“只记录工具提案”扩展成可恢复的 Provider 拉取协议。它允许经过审阅、精确命中上线范围的调用进入队列，并允许指定 Provider 领取短期 lease、开始、续租、完成或失败。它没有开放自然语言 caller，也没有让 Provider 获得平台发送、Registry 管理或 Core 管理权限。

生产上线 `0015` 时先继续保持 `ledger_only`。此时 schema 和路由存在，但 lease 接口只返回无工作，`tool_attempts` 必须保持零新增。只有生产迁移、Provider 报告和三个独立 stop 的证据都成立后，才允许单独评审一个精确 canary。

## 三档执行权限上限

- `off`：拒绝新的调用账本；既有幂等请求仍可读取原记录。
- `ledger_only`：校验并冻结调用证据，但只落成 `recorded_only` 或 `rejected`，绝不排队和发 lease。
- `canary`：它只允许 Core 查询数据库中的 active Git-bound rollout plan；只有命中
  `tool_id + descriptor_version + descriptor_hash + platform:type:id + caller + provider_id`
  全部字段、资源版本和硬上限的请求才能排队。

`SUPERLILY_TOOL_EXECUTION_MODE` 只是上限，环境变量不再承载精确 scope。非空旧
canary/enforce scope 会使 Core 启动失败；`enforce` 在 M3 首包中也明确拒绝。没有
active plan 时，`canary` 请求只会安全落成 `recorded_only`，不会排队。相同执行目标
不能同时指向两个 Provider。当前认证面只允许 `command` 和 `admin_api`；
`natural_language` 仍无入口。

## 调用、attempt 与 lease

调用先冻结 descriptor、input、principal、capability、policy、预算、权限和选中的实现哈希。Provider 只能领取同时满足下列条件的最早队列项：

1. 当前模式和精确范围仍允许；
2. global stop 未开启；
3. descriptor 仍为 `active`；
4. Provider 注册、凭据、inventory、heartbeat、实现哈希和容量仍有效；
5. 工具与 Provider 并发上限均未达到；
6. invocation deadline 尚未过去。

每次领取会创建一个 attempt。数据库部分唯一索引保证同一 invocation 最多只有一个 `leased` 或 `running` attempt；`attempt_number` 与 `fencing_token` 单调递增。Core 只保存随机 lease secret 的 SHA-256，明文只在成功领取响应中出现一次。之后的 start、heartbeat、complete 和 fail 必须同时匹配 Provider 身份、attempt、fence 和 secret。

Provider 上报时间只作诊断，lease、续租和 deadline 一律使用数据库时间。重复、迟到、旧 fence、错误 secret、错误 Provider 或错误状态的操作会返回冲突，并追加一条拒绝事件，不会改写当前成功路径。

## 状态恢复与不确定性

Core 后台 reaper 只在 `canary` 中工作；单轮异常只记日志，不拖垮 API。过期 attempt 的处理按副作用和 deadline 保守决定：

| 情况 | 处理 |
|---|---|
| `retry_safe` 且 deadline 未到 | 当前 attempt 标记 `lease_expired`，invocation 重新排队；下次领取获得新 attempt 和新 fence |
| `retry_safe` 但 deadline 已到 | 终止为 `timed_out` |
| 非安全重试的执行失联 | 终止为 `unknown_completion`，不猜测成功或失败 |
| 取消已发出但 Provider 未确认 | 终止为 `unknown_completion` |
| Provider 明确以 `cancelled` 确认取消 | 终止为 `cancelled` |
| 完成或普通失败与取消竞态 | 终止为 `unknown_completion` |

Provider SDK 对领取、开始、心跳、完成和失败都是单次网络操作，不对可能已经被 Core 接收的操作做盲目自动重试。网络响应不明确时，Provider 停止当前动作并让 lease/reaper 收敛；状态改变工具尤其不能因“没收到响应”而再次执行。

## 预算、输出与事件证据

Core 在排队前校验输入 schema 和精确输入字节数，在完成时再次校验输出 schema、规范化输入/输出字节数与 Provider 上报 usage。超预算、usage 不一致或非法输出都不能成为 `succeeded`，非法输出正文也不会作为成功结果保存。

`tool_attempt_events` 记录每次接受或拒绝的 lease/start/heartbeat/complete/fail/cancel/reap 证据。SQLite 与 PostgreSQL 都用数据库 trigger 禁止 UPDATE 和 DELETE。attempt secret、Bearer token 和原始异常不进入这些事件。

当前 descriptor 的 CPU、内存等字段会参与资格、heartbeat 取消和完成校验；只有 Provider 明确报告为 `hard` 的必需预算才会让 runtime eligible。不能硬执行的预算不得冒充 `hard`。

## `status.inspect@1.0.1` 的历史边界与 `1.0.2` 修正

`status.inspect@1.0.1` 是第一份可执行候选。它与已审阅的 `1.0.0` 语义相同，只因实际 `spawn` 子进程的总峰值 RSS 将内存预算从 64 MiB 调整为 256 MiB；不可变 descriptor 因此必须升版本，不能就地改写 `1.0.0`。其 descriptor SHA-256 为 `398fb49dfff2cc76822e68afa305af2a8aee3aa4f4c50a375320f13175117911`。

`1.0.1` 在 `ledger_only` 生产签署后、真实 canary 前的全量回归中暴露出两个边界：
完整测试进程下 worker 峰值稳定达到约 263 MiB，256 MiB 申报没有裕量；
`multiprocessing spawn` 只能在进入 target 后清空环境，不能严格证明进程创建之初就
没有 Provider/admin/bot secret。因此没有放宽断言或就地改写不可变 authority，而是
新增 `status.inspect@1.0.2`。

`1.0.2` 仍与 `1.0.0/1.0.1` 保持相同输入、输出、权限和行为，只把申报内存预算调整
为 320 MiB，并切换到独立 Python worker。descriptor SHA-256 为
`0cd74138941492d37651d9640d1528bf337bf94b643e76fc0f59585feaec77cd`；worker 源码也
纳入 implementation hash，当前实现哈希为
`156aaa422b4a1dd5290f31312512526866ba2826f1f04b318084c2bb166f4aac`。

修正后的执行器采用以下边界：

- 父进程持有 Provider token 和 lease secret，但创建 worker 时显式构造只含安全
  `PYTHONPATH` 的新环境，不继承父进程环境；
- worker 只通过有界 stdin 收到 descriptor 字节、实现哈希和结构化输入；
- 子进程不收到 Provider token、lease secret、bot token、平台发送接口或 Core 管理接口；
- 父进程强制 wall-time，超时或取消时 terminate、必要时 kill，并等待回收；
- stdout 传输硬限 64 KiB，stderr 丢弃；父进程校验环境安全标记、精确整数 usage、
  输出 schema 和规范化输出字节数；
- Provider 当前串行执行，heartbeat 宣告最大并发为 1，即使 descriptor 上限更高也不会并行领取。
- 无工作时轮询间隔从 0.25 秒指数退避到 5 秒；HTTPX 与 Core 只隐藏成功的空 lease 204 日志，真实 200 lease 和全部错误仍保留。

这一边界足以承载当前固定的只读 `status.inspect` 实现，但它不是通用敌对代码沙箱。
创建时使用安全环境且不传能力，在结构上隔离了秘密与平台发送；对未来会读取文件、
联网、创建子进程或执行任意模型代码的工具，仍需独立的操作系统级
sandbox/cgroup/seccomp/网络策略。不能把这个独立进程监督器直接当作 Wolfram、TeX
或通用 Python runner 的安全证明。

## 四个独立停止开关

任何一个开关都必须独立阻止新 lease：

1. `SUPERLILY_TOOL_GLOBAL_STOP=true`；
2. 将精确 descriptor version 置为 `suspended`；
3. 将指定 Provider 置为 `quarantined`；
4. 将精确 active rollout plan 置为 `paused`。

四者任一都独立阻止后续领取。plan pause 与 lease 使用同一 plan 行锁，暂停接受后
不会创建新 attempt；暂停前已经执行到外部世界的状态不能靠删行回滚，因此在故障时
先停止新 authority，再调查 active attempt 与 `unknown_completion`。

## 上线顺序

1. 在同版本 PostgreSQL 上做自定义格式备份，并完成 `pg_restore --list` 与隔离恢复。
2. 保持 `SUPERLILY_TOOL_EXECUTION_MODE=ledger_only`，不配置旧环境 scope，构建并替换 Core。
3. 验证 Alembic 为 `0015_tool_attempts` head、无 drift、lease 路由对 Provider 返回 204、attempt/event 表为零新增。
4. 从精确 Git 对象导入 `status.inspect@1.0.2` 为 `reviewed`，不得自动激活；
   `1.0.0/1.0.1` 继续作为不可变历史 authority。
5. 替换 status Provider 为 `serve` 模式；验证它只报告 hard wall-time/output-bytes、实现哈希和健康心跳，且不发布端口。
6. 至少观察一个 inventory/heartbeat 周期；确认 `ledger_only` 下没有 lease、attempt 或旧命令行为变化。
7. 另行评审 descriptor 激活、一份最长 24 小时且带调用上限的 Git-bound plan，以及
   一个无平台发送的 `admin_api` canary。未经这一步不得切换执行上限。

首个 canary 前还需在生产边界演练：global stop、descriptor suspension、Provider
quarantine、rollout plan pause、Core/Provider 中断、过期 lease 与恢复。单元测试证明
状态机正确，不替代真实容器和真实 PostgreSQL 的操作证据。

## 回滚

回滚按权限从小到大进行：

1. pause 精确 rollout plan 或开启 global stop，阻止新 lease；
2. suspension 精确 descriptor 或 quarantine Provider；
3. 将模式退回 `ledger_only` 或 `off` 并只重建 Core；
4. 停止 status Provider；
5. 只有确认没有 active attempt、已另做备份且应用版本也回退时，才 downgrade 到 `0014_tool_invocations`。

不要为回滚删除 invocation、attempt 或 append-only 事件。schema downgrade 是最后手段，不是工具异常时的第一反应。

## 实现期验证证据

M3 前的 `0015` 切片曾在 SQLite 与 PostgreSQL 17 全量套件各通过 313 项，覆盖当时
四种模式原型、精确环境 scope 和三个 stop。M3 已用 Git-bound plan 替代环境 scope、
关闭 `enforce`，并增加 plan pause、原子调用上限与 pause/lease 并发回归；当前最终
全量数量以 `PHASE3_ACCEPTANCE.md` 的 M3 证据为准。

这些结果授权部署“仍为 `ledger_only` 的 0015 底座”，不等于已经签署生产 canary，也不等于 Phase 3b/3c 整体完成。

`0015` 的生产 `ledger_only` 签署已于 2026-07-19 02:35 CST 完成：head/no
drift、Provider hard budget/健康 heartbeat、认证 lease=204、零 attempt 与备份实际
恢复均通过。精确镜像、配置和备份证据见 `DEPLOYMENT.md` 第 9 节。descriptor
activation 与 canary 仍受 ADR 0005 的 mutation 治理门约束。

## 首次生产 stop/canary 证据

2026-07-19 06:25–06:26 CST，ADR 0005/0008/0009/0010/0011 的治理门已按
真实控制面使用，没有直接 SQL 改状态。首批 Git-reviewed plan 各自精确绑定
`status.inspect@1.0.2`、`admin_api`、`qq:group:1080353942`、
`provider-status-primary`、descriptor/Provider 资源版本和最多 1 次调用。
每份计划均经 operator 激活，证明结束后立即由 break-glass 暂停。前四份
于 06:25–06:26 证明三个停止面和成功 canary；第五份于 06:43 单独证明
rollout plan pause。

真实结果如下：

- global stop、descriptor suspension 和 Provider quarantine 分别在各自调用
  deadline 前使 Provider lease 返回 204，调用仍在 `queued`，attempt 为 0；
- 独立 rollout-pause 计划在 Provider 停止期间先排队、随后暂停，在 deadline
  前手工 lease 同样为 204/queued/attempt=0；Provider 重启后也没有领取；
- 这四条队列没有被删除或伪造回滚，而是由生产 reaper 按已有契约记录
  `queued -> timed_out / deadline_expired`；
- 成功 canary 只有一个 attempt 和 fence=1，完整转移为
  `propose -> queue -> lease -> start -> complete_success`；
- 输出为 `provider_runtime/status=ok`，descriptor hash 和 implementation hash 与
  reviewed authority 精确一致；实测 wall time 371 ms、CPU 351 ms、峰值内存
  51,187,712 bytes、输入 28 bytes、输出 299 bytes、artifact 0 bytes；
- canary 窗口内 `responses` 表零新增，也没有任何
  `qq:admin_api:*` 触发来源的 response；Provider 仍没有平台发送能力。

首轮演练脚本最后曾把三条未执行队列误写为应当终止于 `expired`，因而在
主体证明全部完成后返回了非零退出码。此偏差没有改变生产状态：单测、
本文“状态恢复与不确定性”矩阵和数据库转移均明确规定 queued deadline 是
`timed_out`。`finally` 已将 Core 恢复为 `ledger_only`，Provider 保持运行。
第五份脚本已按 `timed_out` 契约验收并以零码退出，两个新会话均显式
logout/revoked；首轮四个未 logout 会话在再次开启控制面前已全部过期。五份计划
现均为 `paused/rv3`、消费数 1/1，无 active plan/lease；descriptor
为 `active/rv4`，Provider 为 `active/rv3`。

这一证据签署了四个独立 stop 和首次只读 canary，不等于该时点的 Phase 3b
整体完成。过期 lease、Core/Provider 中断、旧 fence、重复完成、取消竞态、
safe retry 和 `unknown_completion` 在该时点仍待生产故障矩阵；在此之前不扩大
conversation、caller 或工具集合。

## 第二批故障矩阵实施包

第二批不再重复实现 Provider SDK 或 `status.inspect`，而是验证它们在异常条件下的
真实收敛。详细矩阵、短 lease 数学、八份单调用 plan、正式驱动器、Core/数据库中断
边界和验收门见 `PHASE3_FAULT_DRILLS.md`。

实现新增明确回归：第二 fence 成功后旧 worker 与重复完成仍被拒绝；完成与取消竞态
进入 `unknown_completion`；取消未确认在 lease 过期后进入
`cancellation_unacknowledged`；Provider 自报 2099/1970 时间均不能延长数据库
deadline。生产 authority 仍是一次只激活一份、每份最多 1 次的 Git-bound plan，
驱动器本身不能激活计划或管理容器。

提交前完整回归为 SQLite 395 项通过、4 项 PostgreSQL 专用场景跳过；同一提交在
一次性 PostgreSQL 17.10 上为 399 项全部通过。13 份已提交/待提交的 status 单调用
authority 均由生产合同逐份解析，并统一验证精确工具、hash、conversation、caller、
Provider、`max_invocations=1` 和 `rollback_mode=ledger_only`。

## 第二批生产故障矩阵与稳定签署

2026-07-19 07:45–07:46 CST，八份来自提交 `7f509e9` 的单调用计划逐份经过
operator 激活和 break-glass 暂停，没有直接 SQL mutation。safe retry 真实产生
fence 1 的 `lease_expired` 与 fence 2 的 `succeeded`；三次旧 fence/重复完成均只追加
reject。非法输出、快慢时钟、三种取消路径、Core 中断和 PostgreSQL 中断分别得到
`PHASE3_FAULT_DRILLS.md` 规定的精确终态，两条不确定结果保留为
`unknown_completion`，没有删除或自动重试。

最终八份计划全部 `paused/rv3`、消费 1/1；全库 14 条 invocation 的分布为
recorded_only=1、timed_out=6、succeeded=3、failed=1、cancelled=1、
unknown_completion=2，无 active plan/invocation/attempt。16 次计划变更、16 份
preview、24 条 lifecycle event 和两个已撤销会话完整留存；故障窗口内 response
零新增。Core 回到 `ledger_only/global_stop=false/lease=15`，控制面默认 503。

稳定观察最初发现空 lease 的 5 秒退避与服务端 5 秒 keep-alive 边界偶发 ReadError。
SDK 提交 `2b31c6b` 将空轮询连接隔离后，SQLite 仍为 395 通过、4 跳过，PostgreSQL
17 为 399 通过。修正版 Provider 跨过完整 inventory 周期，2 次 inventory、11 次
healthy heartbeat、零日志异常和零重启；C0-D 两条 spool 也都重新收敛为
pending/quarantine/gap=0。由此 `status.inspect` 的故障/回滚与稳定窗口签署完成，
后续 `0016_confirm_artifacts` 已完成实现与双数据库全量回归；生产默认关闭签署后，
下一实现包转为文本模式 `wolfram.run`。
