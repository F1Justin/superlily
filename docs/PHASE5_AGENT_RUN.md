# Phase 5 AgentRun 实施合同

本文是 Phase 5 的实现权威，细化 `FUTURE_PHASES_DESIGN.md` 的自然语言规划边界。
在本文被后续 ADR 取代前，模型输出始终只是请求，不是 Core authority。

## 当前切片

当前实现仅为 **5a planner-only shadow**。数据库 head 为 `0020_agent_runs`，运行时
默认 `SUPERLILY_AGENT_MODE=off`。即使显式切到 `shadow`：

- `AgentRun` 的 `tool_invocation_count` 与 `delivery_intent_count` 由数据库约束固定为
  0；
- 模型 Provider 只能读取分配给自己的有界 planner input，并提交一次终态 attempt；
- Core 只校验和记录 answer/tool proposal，不创建 `ToolInvocation`、Renderer 请求或
  delivery intent；
- 现有 `InvocationIdentity.caller`、rollout caller、descriptor
  `natural_language` 门和三个执行 Provider 均不改变；
- 命令路径不读取模型健康状态，不等待模型响应。

5a 还不是生产签署。模型 profile、凭据、shadow 开关、评分样本、双库证据和稳定窗口
必须分别完成审查后，才能进入默认禁用部署。

## Authority 边界

`AgentRun` 只能由现有 `admin_api` 身份创建。模型 Provider 使用独立
`SUPERLILY_MODEL_PROVIDER_TOKENS_JSON` 身份；该 token 不得与 ingest、admin、
执行 Provider、render backend 或 artifact secret 重用。模型 Provider 没有创建 run、
选择别人的 run、调用工具或发送消息的接口。

5a 不引入 `caller=agent`。`tool_registry.py` 对 `natural_language=true` 和
`allowed_callers=agent` 的拒绝、`auth.py` 的 `command|admin_api` 联合类型、数据库
caller CHECK，以及各 Provider 的二次 descriptor 校验继续有效。解禁这些边界属于
5b，必须由新迁移和精确 Git-bound rollout plan 同时完成，不能靠绕过校验实现。

## 0020 账本

`0020_agent_runs` 新增：

- `agent_model_profiles`：Git 审阅的模型数据处理、结构化输出、上下文、价格和健康
  profile；不可更新或删除。
- `agent_runs`：冻结 source event、principal、context recipe/version/hash、eligible
  tool summary、预算和模型 profile；只允许受事件约束的状态迁移。
- `agent_run_events`：状态变化的追加式证据。
- `agent_run_attempts`：每次模型请求的终态结果、usage、成本和安全错误；失败重试新增
  一行，不覆盖旧 attempt。
- `agent_tool_proposals`：逐项记录工具、参数 hash、校验结论和安全理由，但不执行。

SQLite trigger 与 PostgreSQL trigger function 都禁止修改/删除证据、提升运行权限、
跳跃状态或在没有匹配事件时更新状态。5a 状态机为：

```text
context_ready -> model_running -> context_ready
                              -> shadow_complete
                              -> failed | timed_out | budget_exhausted | cancelled
```

第一次箭头是开始一个 attempt；回到 `context_ready` 只表示预算内可重试。终态不可变。

## 上下文配方

Core 在 run 创建时自行构造 `phase5-context-v1`，调用方不能上传 prompt/context。
快照包含：

1. 固定 system/safety policy 与 policy/prompt version；
2. 当前规范化消息和已解析 reply graph；
3. 同一 canonical conversation 的有界最近消息窗口；
4. sender、conversation、观测角色和 capability 摘要；
5. 仅 Registry 当前判为 `effective_eligible`、public、`none|read|compute` 的工具摘要。

工具摘要只包含工具/descriptor 身份、标题、说明、side effect、permission、input
schema hash 和顶层字段摘要，不包含完整 schema，也不披露不合格工具。所有消息仍被
当作不可信数据，不能改变 policy、principal、工具集合或预算。recipe version 和完整
canonical context hash 都存入 run。

## 模型 Provider profile

模型 profile 与执行 Provider authority 分离，但采用相同的 Git-review 原则。不可变
profile 至少绑定：

- provider ID 与 profile version/hash；
- 数据本地性、保留期和允许的数据分级；
- `json_schema|tool_calls|json_object` 结构化输出协议；
- context/output token 上限；
- USD microunit 定价快照；
- `superlily-model-provider-v1` 健康协议。

Core 按冻结价格复算 attempt 成本；不匹配即拒绝。当前不绑定某个商业 LLM SDK：
模型厂商、endpoint、认证和 failover 尚未选定，先提交 vendor-neutral 数据面可避免
把未经审阅的厂商依赖变成默认 authority。接入首个真实 Provider 时，SDK 及锁定版本
必须同时进入 `pyproject.toml` 和 `deploy/constraints.txt`。

## API 与幂等

- `POST /v1/agent-runs`：admin 创建一份 shadow run。
- `GET /v1/agent-runs/{run_id}`：admin 查看账本。
- `GET /v1/agent-runs/{run_id}/planner-input`：只有被绑定的模型 Provider 可读。
- `POST /v1/agent-runs/{run_id}/attempts`：同一 Provider 回报终态 attempt。

创建和回报均要求幂等键；同键同内容返回既有记录，同键异内容返回冲突。planner input
明确携带 `tool_execution_authority=false` 与 `delivery_authority=false`。

## 预算与 shadow 评分

5a 强制工具调用、串行深度、并行扇出、result bytes、artifact bytes 为 0；其余预算
覆盖模型 attempt/turn、tool proposal 数、wall time、input/output/total token、成本
及输入输出字节。预算耗尽必须终止或拒绝，不能自动扩容。

Core 对每个 proposal 记录 `valid`、`invalid_arguments`、`forbidden_tool` 或
`duplicate_loop`。离线评分再把这些证据与确定性命令路由标签组合，计算 false call、
missed call、wrong tool、参数无效、禁用工具请求和路由分歧。评分不得反向触发工具或
平台消息。

## 5b 解禁清单

进入 5b 前必须另行完成：

1. 新合同与迁移同时放宽 `caller=agent`、rollout item caller CHECK 和
   `InvocationIdentity`，并把 descriptor hard fail 改为 Phase 5 门控。
2. 三个 Provider 接受 `agent` 前仍各自复验 descriptor、lease、fence、预算和精确
   rollout authority。
3. 一份新 Git-bound plan 只允许一个 exact conversation、一个工具、一个 descriptor、
   一个 Provider 和有界次数；顺序为 `status.inspect`、`wolfram.run`、
   `latex.render`。
4. 工具结果作为带来源、数据分级、边界和长度限制的不可信模型输入；等价重复调用按
   loop 拒绝。
5. 所有响应仍只能形成 Phase 4 delivery intent；工具和模型都不能调用平台 API。

5b 不创建历史检索库，不把 `history.search` 作为退出条件。检索与记忆等待 Phase 8
的 conversation scope 和保留策略。

## 5c 阻塞项

写操作必须等待真实 principal/authorization 模型。当前非 `public` descriptor 会在
invocation service 中收敛为 `principal_unauthorized`，这不是可绕开的临时限制。
完成该工作包后才可复用 `0016_confirm_artifacts` 的 principal/policy/args hash、
expiry 和单次消费语义，并同步放宽 confirmation caller CHECK。

## 验收与生产纪律

5a 发布前至少要求 SQLite/PostgreSQL 迁移往返与 drift、严格合同、越权 Provider、
prompt 注入、禁用工具、无效参数、等价循环、预算、失败重试、幂等、Core 中断以及
零 invocation/零 delivery 的确定性测试。之后仍按默认禁用部署、后台账本 canary、
稳定窗口和签署推进。

生产操作员已禁止向公开群主动发送合成测试内容。Phase 5 默认只使用自动化、后台
账本和无发送探针；任何群消息必须取得当次明确授权。
