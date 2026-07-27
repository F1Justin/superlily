# ADR 0015：AgentRun 的零执行 authority 边界

- 状态：accepted for implementation
- 日期：2026-07-27
- 细化：ADR 0003、0005、0011、0012 与 Phase 5 设计

## 背景

第四阶段结束后，工具执行、渲染与发送已有独立账本，但 Core 没有 AgentRun、模型
Provider 或模型预算。现有合同还在 descriptor、caller 类型、数据库 CHECK 和三个
Provider 中主动拒绝 agent caller。直接让模型调用工具会同时绕过四层 authority。

## 决定

1. 先实现 planner-only shadow。模型输出是结构化提案，不是调用、确认或发送权限。
2. `0020_agent_runs` 冻结 context recipe/hash、principal、eligible tool summaries、
   模型 profile 和预算，并以追加事件约束状态迁移。
3. 5a 的工具调用、串行深度、并行扇出、result/artifact bytes、delivery 都由合同和
   数据库固定为 0。
4. `caller=agent` 不在 5a 引入；既有四层拒绝继续作为可测试的安全边界。
5. 模型失败只影响对应 AgentRun，不能参与命令路由、Provider lease 或平台发送。
6. 任何后续执行只能由 Core 把通过当前 authority 复验的 proposal 转成普通 Phase 3
   invocation，并且必须绑定新的 Git-reviewed exact rollout plan。

## 后果

5a 可以采集真实规划质量和成本证据而不扩大生产权限。代价是模型即使给出正确提案也
不会执行或回复用户；这正是 shadow 的目标。5b 必须以新 ADR/迁移显式取代此零调用
约束，不能把它当成环境开关。

## 必需证据

双数据库 trigger/constraint、Provider 越权、上下文边界、预算、重试/幂等、终态
不可变，以及数据库中 `ToolInvocation` 和 delivery intent 均为零。
