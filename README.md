# Superlily

Superlily 是 [`manifesto.md`](manifesto.md) 描述的 Lily Core。Phase 1 建立可观测
脊柱；Phase 2 建立规范关联、确定性裁决、运行时命令清单、结果审计和 fail-open
claim canary；C0-D 建立持久采集 spool、commit receipt、action observation 和覆盖诊断。
这些阶段均已完成生产签署。

Phase 3a 的 descriptor authority、Provider 身份、inventory/heartbeat 和共享 SDK 已
上线。Phase 3b 的 invocation/attempt 账本、lease/fence/reaper、控制面 M0–M3、
`status.inspect@1.0.2` 和 Git-bound rollout plan 已部署；四个独立 stop 与一次无平台
发送的生产 canary、八项异常恢复故障矩阵和修正后的稳定窗口均已完成。13 份一次性
计划全部暂停并耗尽，Core 恢复 `ledger_only`。当前开始 `0016` 的确认/artifact
账本设计与实现，不扩大自然语言、conversation、caller 或工具集合。

运行时仍刻意 fail-open：遥测故障不阻塞 Lily/Nekro，claim 故障保留原有行为；
工具执行则必须显式 fail closed，缺 authority、身份、健康、预算或 fence 时不执行。

## 目录

- `packages/contracts`：版本化采集/工具合同、authority 校验、共享向量和 payload
  sanitizer。
- `apps/core`：FastAPI 采集/查询/工具账本/控制面服务和数据库模型。
- `apps/status_provider`：独立、受硬边界约束的 `status.inspect` Provider。
- `bridges/lily_nonebot`：Lily/NoneBot observer 与 durable reporter。
- `bridges/nekro`：Nekro observer 与 durable reporter。
- `registry`：Git-reviewed descriptor、Provider 和短时 rollout plan authority。
- `deploy`：Docker Compose、固定依赖和集成配置。
- `docs`：设计、运维、安全、路线和验收证据。

## 文档入口

- 本地开发：[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- 权威实施顺序：[`docs/ROADMAP.md`](docs/ROADMAP.md)
- 第三阶段协议：[`docs/PHASE3_TOOL_REGISTRY.md`](docs/PHASE3_TOOL_REGISTRY.md)
- 第三阶段故障矩阵：[`docs/PHASE3_FAULT_DRILLS.md`](docs/PHASE3_FAULT_DRILLS.md)
- 采集与 agent 共识：
  [`docs/COLLECTION_AND_AGENT_CONSENSUS.md`](docs/COLLECTION_AND_AGENT_CONSENSUS.md)
- C0-D 签署：[`docs/C0D_ACCEPTANCE.md`](docs/C0D_ACCEPTANCE.md)
- 后续阶段设计：[`docs/FUTURE_PHASES_DESIGN.md`](docs/FUTURE_PHASES_DESIGN.md)
- 三账号高可用：[`docs/PHASE6_THREE_ACCOUNT_HA.md`](docs/PHASE6_THREE_ACCOUNT_HA.md)
- Phase 2 最终审计：[`docs/PHASE2_FINAL_AUDIT.md`](docs/PHASE2_FINAL_AUDIT.md)
