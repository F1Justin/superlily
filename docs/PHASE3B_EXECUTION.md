# Phase 3b：可恢复执行账本与 `status.inspect` 执行边界

## 这一步提升了什么权限

`0015_tool_attempts` 把 `0014` 的“只记录工具提案”扩展成可恢复的 Provider 拉取协议。它允许经过审阅、精确命中上线范围的调用进入队列，并允许指定 Provider 领取短期 lease、开始、续租、完成或失败。它没有开放自然语言 caller，也没有让 Provider 获得平台发送、Registry 管理或 Core 管理权限。

生产上线 `0015` 时先继续保持 `ledger_only`。此时 schema 和路由存在，但 lease 接口只返回无工作，`tool_attempts` 必须保持零新增。只有生产迁移、Provider 报告和三个独立 stop 的证据都成立后，才允许单独评审一个精确 canary。

## 四种执行模式

- `off`：拒绝新的调用账本；既有幂等请求仍可读取原记录。
- `ledger_only`：校验并冻结调用证据，但只落成 `recorded_only` 或 `rejected`，绝不排队和发 lease。
- `canary`：只有命中 `tool_id + descriptor_version + descriptor_hash + platform:type:id + caller + provider_id` 全部字段的请求才能排队。
- `enforce`：使用独立的、同样精确且另行审阅的 allowlist；不会复用 canary 范围，也不支持通配符。

`canary` 或 `enforce` 没有非空精确范围时，Core 启动即失败。相同执行目标不能同时指向两个 Provider。当前认证面只允许 `command` 和 `admin_api`；`natural_language` 仍无入口。

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

Core 后台 reaper 只在 `canary` 或 `enforce` 中工作；单轮异常只记日志，不拖垮 API。过期 attempt 的处理按副作用和 deadline 保守决定：

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

## `status.inspect@1.0.1` 的硬边界

`status.inspect@1.0.1` 是第一份可执行候选。它与已审阅的 `1.0.0` 语义相同，只因实际 `spawn` 子进程的总峰值 RSS 将内存预算从 64 MiB 调整为 256 MiB；不可变 descriptor 因此必须升版本，不能就地改写 `1.0.0`。其 descriptor SHA-256 为 `398fb49dfff2cc76822e68afa305af2a8aee3aa4f4c50a375320f13175117911`。

执行器采用父进程监督、每次调用 `spawn` 一个子进程的结构：

- 父进程持有 Provider token 和 lease secret；启动执行模式前会从自身环境删除 token；
- 子进程只收到 descriptor 字节、实现哈希和结构化输入，启动后清空环境；
- 子进程不收到 Provider token、lease secret、bot token、平台发送接口或 Core 管理接口；
- 父进程强制 wall-time，超时或取消时 terminate、必要时 kill，并等待回收；
- 父进程校验子进程返回的精确整数 usage、输出 schema 和规范化输出字节数；
- Provider 当前串行执行，heartbeat 宣告最大并发为 1，即使 descriptor 上限更高也不会并行领取。
- 无工作时轮询间隔从 0.25 秒指数退避到 5 秒；HTTPX 与 Core 只隐藏成功的空 lease 204 日志，真实 200 lease 和全部错误仍保留。

这一边界足以承载当前固定的只读 `status.inspect` 实现，但它不是通用敌对代码沙箱。清空环境和不传能力在结构上隔离了秘密与平台发送；对未来会读取文件、联网、创建子进程或执行任意模型代码的工具，仍需独立的操作系统级 sandbox/cgroup/seccomp/网络策略。不能把这个 `multiprocessing` 监督器直接当作 Wolfram、TeX 或通用 Python runner 的安全证明。

## 三个独立停止开关

任何一个开关都必须独立阻止新 lease：

1. `SUPERLILY_TOOL_GLOBAL_STOP=true`；
2. 将精确 descriptor version 置为 `suspended`；
3. 将指定 Provider 置为 `quarantined`。

收窄或删除精确 canary/enforce scope 也会阻止后续领取。急停不删除账本；已经执行到外部世界的状态不能靠删行回滚，因此在故障时先停止新 authority，再调查 active attempt 与 `unknown_completion`。

## 上线顺序

1. 在同版本 PostgreSQL 上做自定义格式备份，并完成 `pg_restore --list` 与隔离恢复。
2. 保持 `SUPERLILY_TOOL_EXECUTION_MODE=ledger_only`、两个 scope 均为 `[]`，构建并替换 Core。
3. 验证 Alembic 为 `0015_tool_attempts` head、无 drift、lease 路由对 Provider 返回 204、attempt/event 表为零新增。
4. 从精确 Git 对象导入 `status.inspect@1.0.1` 为 `reviewed`，不得自动激活。
5. 替换 status Provider 为 `serve` 模式；验证它只报告 hard wall-time/output-bytes、实现哈希和健康心跳，且不发布端口。
6. 至少观察一个 inventory/heartbeat 周期；确认 `ledger_only` 下没有 lease、attempt 或旧命令行为变化。
7. 另行评审 descriptor 激活、一个精确范围和一个无平台发送的 `admin_api` canary。未经这一步不得切换执行模式。

首个 canary 前还需在生产边界演练：global stop、descriptor suspension、Provider quarantine、scope withdrawal、Core/Provider 中断、过期 lease 与恢复。单元测试证明状态机正确，不替代真实容器和真实 PostgreSQL 的操作证据。

## 回滚

回滚按权限从小到大进行：

1. 删除精确 scope 或开启 global stop，阻止新 lease；
2. suspension 精确 descriptor 或 quarantine Provider；
3. 将模式退回 `ledger_only` 或 `off` 并只重建 Core；
4. 停止 status Provider；
5. 只有确认没有 active attempt、已另做备份且应用版本也回退时，才 downgrade 到 `0014_tool_invocations`。

不要为回滚删除 invocation、attempt 或 append-only 事件。schema downgrade 是最后手段，不是工具异常时的第一反应。

## 实现期验证证据

截至 2026-07-19，SQLite 与 PostgreSQL 17 全量套件各 313 项通过。覆盖范围包括四种模式、精确 canary/enforce、三个 stop、并发领取、单活动 lease、单调 fence、secret/Provider 绑定、迟到与重放、取消竞态、预算取消、非法输出、append-only trigger、reaper、管理 CLI 的真实模式回报、空闲轮询退避/日志保真与真实 `status.inspect` 子进程端到端路径。

这些结果授权部署“仍为 `ledger_only` 的 0015 底座”，不等于已经签署生产 canary，也不等于 Phase 3b/3c 整体完成。

`0015` 的生产 `ledger_only` 签署已于 2026-07-19 02:35 CST 完成：head/no
drift、Provider hard budget/健康 heartbeat、认证 lease=204、零 attempt 与备份实际
恢复均通过。精确镜像、配置和备份证据见 `DEPLOYMENT.md` 第 9 节。descriptor
activation 与 canary 仍受 ADR 0005 的 mutation 治理门约束。
