# ADR 0016：模型 Provider 的 Git-reviewed 注册

- 状态：accepted for implementation
- 日期：2026-07-27
- 细化：ADR 0001、0002、0011 与 0015

## 背景

模型端点会接收 conversation 数据，并产生随厂商、区域、保留策略、结构化输出能力和
价格变化的结果。仅配置一个 API key 不能说明它可以接收哪类数据，也不能提供可重复
的预算或 failover 决策。

## 决定

1. 模型 Provider 使用独立身份和 token；不得重用 ingest、admin、执行 Provider、
   Renderer 或 artifact credential。
2. 可用性不是 authority。每个 profile 必须由 Git commit 审阅并以 canonical hash
   导入，绑定 locality、retention、允许的数据分级、structured-output protocol、
   context/output 上限、定价和健康协议。
3. AgentRun 冻结精确 profile JSON/hash 和价格。Core 复算 usage 成本，不能采用运行
   时返回的未审阅价格。
4. Provider 只能读取显式分配给自己的 run；不能列举其他 run、改变 context、选择
   工具、领取执行 lease 或创建 delivery。
5. fallback 必须重新通过相同数据分级、能力、预算和显式路由政策；健康但不匹配的
   Provider 不可接收数据。
6. 首个真实厂商尚未选定，因此 5a 合同保持 vendor-neutral。选择厂商时再把官方 SDK
   与精确约束纳入依赖审查，不能先加入未使用的 SDK。

## 后果

本决定使隐私、成本与 failover 可以按历史 profile 重放审计，但要求每次模型、区域、
保留期或价格变化创建新 profile version。token 轮换不改变 profile authority。

## 必需证据

canonical profile/hash、不可变导入、token 域隔离、错误 Provider 拒绝、数据分级
拒绝、价格复算、上下文上限、同键异内容冲突和 Provider 故障不影响命令路径。

## 2026-07-29 实现注记

首个真实 profile 已选定为 Git-reviewed `deepseek-v4-pro@1.0.0`，实现直接使用
`httpx` 调用审阅过的官方 OpenAI-compatible 端点，不引入厂商 SDK。
`0023_agent_model_routes` 把 primary、最多三份有序 fallback、route hash 与
`routing_reason` 冻结进 AgentRun。每份 fallback 在接收数据前重新校验 profile
hash、独立凭据、数据分级、上下文/输出上限和总 attempt 预算；失败后旧 Provider
失去 planner-input 读取权，实际 attempt 成本按新 profile 快照复算。
