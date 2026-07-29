# Phase 5a 真实模型 Shadow 证据

本文记录 Phase 5a 在生产部署前的本地、无发送真实模型探针。它证明真实模型 Provider
能够完成受 Core 约束的 planner-only run；它不是生产 canary、稳定窗口或 Phase 5a
生产签署。

## 2026-07-29 DeepSeek 探针

- 代码提交：`78b24e644aa6c31d2b9fb65091c681b1c110965c`
- 数据库：一次性 SQLite，Alembic head `0023_agent_model_routes`
- 运行模式：`shadow`
- source conversation：内部 `system` 探针，不对应公开群
- run ID：`c86f2be8-ce60-4b02-93bd-5ac6e77f54a7`
- model provider：`deepseek-v4-pro`
- profile version：`1.0.0`
- profile hash：
  `948f9b7cd20394f0607d1bb347f776e80f5b5e307c381223b8a40d3bf735bec3`
- routing reason：`phase5a_real_no_send_probe`
- 终态：`shadow_complete`
- reason：`proposal_recorded_no_execution`
- attempt：1 次，成功
- model request ID：`d364ad53-1c22-4c0e-acb3-5843179db4bd`
- usage：630 input、317 output、947 total tokens，4,106 ms，551 USD
  microunits
- eligible tools：0
- tool proposals：0
- tool invocations：0
- delivery intents：0

Core 在 `context_ready` 事件中冻结
`tool_execution_enabled=false`、`delivery_enabled=false`，随后只允许所选 Provider
读取有界 planner input。模型成功回报后，Core 记录 attempt 和 usage 并直接进入
`shadow_complete`；没有创建工具调用或平台发送意图。

API key 仅在既有 Nekro 容器进程内短暂注入 Provider，没有写入仓库、SQLite 账本、
探针文档或临时 Python 包。探针没有调用 QQ/NapCat、Renderer 或任何公开群发送路径。

## 结论与剩余门

本探针满足“真实 Provider + 后台账本 + 零执行 + 零发送”的本地 5a 证据。仍需完成：

1. 经操作员明确授权后，以默认 `off` 部署生产 Core 并执行 `0019` 到 `0023` 迁移；
2. 在生产后台执行同类无发送 shadow，并确认命令路径不依赖模型健康；
3. 以 exact conversation、exact Git resource 和一次额度执行 5b
   `wolfram.run@1.1.0` 无发送 canary；
4. 回落 `off`/`ledger_only`，完成稳定窗口和生产签署。

任何公开群消息仍需当次明确授权。
