# Phase 5 AgentRun 实施合同

本文是 Phase 5 的实现权威，细化 `FUTURE_PHASES_DESIGN.md` 的自然语言规划边界。
在本文被后续 ADR 取代前，模型输出始终只是请求，不是 Core authority。
群聊快速路径、模型自主选择、渐进式披露、Unix 原语、自然语言命令、模型路由和
输出体验的长期产品约束见 `AGENT_PRODUCT_AND_IMPLEMENTATION_CONSENSUS.md`；本文只
规定当前 Phase 5 authority、合同和发布门。

## 当前切片

当前实现包含 **5a planner-only shadow** 和尚未生产签署的 **5b 单工具 Wolfram
执行环**。数据库 head 为 `0023_agent_model_routes`，运行时仍默认
`SUPERLILY_AGENT_MODE=off`。显式切到 `shadow` 时：

- `AgentRun` 的 `tool_invocation_count` 与 `delivery_intent_count` 由数据库约束固定为
  0；
- 模型 Provider 只能读取分配给自己的有界 planner input，并提交一次终态 attempt；
- Core 只校验和记录 answer/tool proposal，不创建 `ToolInvocation`、Renderer 请求或
  delivery intent；
- 命令路径不读取模型健康状态，不等待模型响应。

Git-bound `deepseek-v4-pro@1.0.0` profile、真实 JSON Provider、缓存命中/未命中
精确定价和离线评分器已经进入实现。切到 `bounded_readonly` 后，也只允许把一个
`wolfram.run@1.1.0` 有效 proposal 显式提升为一个 tool loop；它仍需 exact rollout
plan 才能从 `ledger_only` 进入队列。

5a/5b 都还不是生产签署。凭据、开关、评分样本、双库证据、无发送 canary 和稳定窗口
必须分别完成审查。

2026-07-29 已在一次性本地 Core 上完成真实 DeepSeek 5a 无发送 shadow：
1 次 attempt 成功，账本终态为 `shadow_complete`，工具调用与 delivery intent 均为
0。该证据不等于生产部署或签署，详见 `PHASE5_5A_SHADOW_EVIDENCE.md`。

## Authority 边界

`AgentRun` 只能由现有 `admin_api` 身份创建。模型 Provider 使用独立
`SUPERLILY_MODEL_PROVIDER_TOKENS_JSON` 身份；该 token 不得与 ingest、admin、
执行 Provider、render backend 或 artifact secret 重用。模型 Provider 没有创建 run、
选择别人的 run、调用工具或发送消息的接口。

5a 不使用 `caller=agent`。5b 通过 `0021_agent_tool_callers` 同时放宽共享合同、
`InvocationIdentity`、tool invocation 和 rollout item CHECK；descriptor 只有在
`public + none|read|compute + confirmation=never` 时才能把
`natural_language=true` 与 `allowed_callers=agent` 成对开启。confirmation caller
CHECK 没有放宽，因此不能借 5b 到达写操作。

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

Core 按冻结的 cache-hit、cache-miss 和 output 价格分别复算 attempt 成本；不匹配即
拒绝。首个实现使用现有 `httpx` 直接调用 DeepSeek OpenAI-compatible HTTP 协议，不
新增厂商 SDK。profile 将厂商未承诺精确上限的保留期记为 `null`，不能误写成零保留。
群聊文本只允许进入 `public|conversation` profile，不允许 `sensitive` 或
`administrative`。

`0023_agent_model_routes` 进一步冻结 primary profile、最多三份有序 fallback
profile、完整 route hash 与 `routing_reason`。fallback 不是“换一个在线模型”：
Core 在创建 run 时逐份重新校验 Git profile/hash、独立凭据、数据分级、
context/output 上限和总 attempt 预算；失败后只有当前有序 route 的下一份 Provider
能读取 planner input。每次 attempt 仍按实际选中 profile 的冻结价格复算，旧 Provider
不能在 route 已前移后读取或提交新 attempt。5b continuation 使用同一有序 route，
模型故障不会改变工具 invocation、delivery authority 或确定性命令路径。

## API 与幂等

- `POST /v1/agent-runs`：admin 创建一份 shadow run。
- `GET /v1/agent-runs/{run_id}`：admin 查看账本。
- `GET /v1/agent-runs/{run_id}/planner-input`：只有当前 route 选中的模型 Provider 可读。
- `POST /v1/agent-runs/{run_id}/attempts`：当前 Provider 回报终态 attempt；可重试错误
  才会按冻结 route 前移。

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

5b 实现与验收清单：

1. 新合同与迁移同时放宽 `caller=agent`、rollout item caller CHECK 和
   `InvocationIdentity`，并把 descriptor hard fail 改为 Phase 5 门控。
2. 当前只让 Wolfram Provider 接受新的 `wolfram.run@1.1.0`；status 与 LaTeX 不为
   了凑顺序解禁。Provider 仍复验 descriptor、lease、fence、预算和精确 rollout
   authority。
3. 一份新 Git-bound plan 只允许一个 exact conversation、一个工具、一个 descriptor、
   一个 Provider 和有界次数。首个且当前唯一验收工具为 `wolfram.run`；
   `status.inspect` 只是普通偶用插件，不是 Agent 架构的先决门。
4. 工具结果作为带来源、数据分级、边界和长度限制的不可信模型输入；等价重复调用按
   loop 拒绝。
5. 所有响应仍只能形成 Phase 4 delivery intent；工具和模型都不能调用平台 API。

`0022_agent_tool_loops` 把 proposal、invocation、带来源/边界/分级/长度上限的
不可信结果和一次模型 continuation 串在独立账本中。初始包硬限制一次调用、深度 1、
fanout 1、artifact 0；continuation 再提工具一律拒绝，最终结果仍不创建 delivery
intent。`0023_agent_model_routes` 同时覆盖初始 planning 与 continuation 的显式
Provider failover；不存在未经审阅的隐式降级。

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
