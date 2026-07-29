# Superlily

Superlily 是 [`manifesto.md`](manifesto.md) 描述的 Lily Core。Phase 1 建立可观测
脊柱；Phase 2 建立规范关联、确定性裁决、运行时命令清单、结果审计和 fail-open
claim canary；C0-D 建立持久采集 spool、commit receipt、action observation 和覆盖诊断。
这些阶段均已完成生产签署。

Phase 3 已于 2026-07-19 完成生产签署。Phase 3a 的 descriptor authority、Provider
身份、inventory/heartbeat 和共享 SDK 已上线；Phase 3b 的 invocation/attempt 账本、
lease/fence/reaper、控制面 M0–M3、
`status.inspect@1.0.2` 和 Git-bound rollout plan 已部署；四个独立 stop 与一次无平台
发送的生产 canary、八项异常恢复故障矩阵和修正后的稳定窗口均已完成。13 份一次性
计划全部暂停并耗尽，Core 恢复 `ledger_only`。`0016_confirm_artifacts` 的精确确认、
内容寻址 artifact、Provider SDK、清理器与数据库防篡改已经完成双数据库全量回归，
并已按默认关闭状态完成生产备份/恢复、迁移和零 authority 签署。文本模式
`wolfram.run@1.0.0` 的 descriptor、独立 Provider、既有 Wolfram 15.0 私有 socket
边界和中文 ADR 已实现，SQLite 455 项通过、4 项跳过，PostgreSQL 17 为 459 项通过，
受限容器中的真实 `2+2` 探针返回 `4`。生产随后完成 reviewed 空转、descriptor
激活和最多一次的 Git-bound canary；唯一 attempt/fence 返回 `4`、artifact=0，旧
`/wf` data source 串行对比同样返回 `4`。计划已暂停并耗尽，Core 恢复
`ledger_only`、控制面关闭；完整 inventory 稳定周期也已通过。自然语言、conversation
和平台发送权限均未扩大。`latex.render@1.0.0` 随后完成独立无凭据 worker、生产
artifact store、reviewer 激活和最多一次的 Git-bound canary：
它把 XeLaTeX/Poppler 放进无网络、无凭据、1 GiB cgroup 的独立 worker，通过
reserve/upload/finalize 返回最多 4 MiB、2048×2048 的内容寻址 PNG。唯一 attempt
得到 finalized/referenced 的 34,883 字节、2048×499 PNG，计划随即暂停并耗尽；旧
`/tex` 串行对比成功且保持不变。最终 SQLite 为 463 通过、4 跳过，PostgreSQL 17
为 467 通过，稳定窗口和零关联平台 response 均已签署。Phase 4 又于
2026-07-26 完成 RenderDocument 1.3、能力规划、artifact 溯源、四条兼容路径和
61 小时稳定窗口的生产签署。当前 Phase 5 已实现但尚未生产签署：5a 提供默认关闭的
DeepSeek planner-only `AgentRun` shadow；5b 只允许经 Git-bound canary 把一个
`wolfram.run@1.1.0` proposal 提升为一次受限调用，并把有来源、限长的不可信结果回注
模型一次。模型故障切换只能沿 run 中冻结、逐份重新授权的显式 profile route 前移。
平台发送仍为 0，status 插件不是 Agent 前置门。实现边界见
[`docs/PHASE5_AGENT_RUN.md`](docs/PHASE5_AGENT_RUN.md)。

运行时仍刻意 fail-open：遥测故障不阻塞 Lily/Nekro，claim 故障保留原有行为；
工具执行则必须显式 fail closed，缺 authority、身份、健康、预算或 fence 时不执行。

## 目录

- `packages/contracts`：版本化采集/工具合同、authority 校验、共享向量和 payload
  sanitizer。
- `apps/core`：FastAPI 采集/查询/工具账本/控制面服务和数据库模型。
- `apps/status_provider`：独立、受硬边界约束的 `status.inspect` Provider。
- `apps/wolfram_provider`：文本模式、复用现有隔离 worker 的 `wolfram.run` Provider。
- `apps/model_provider`：Phase 5 DeepSeek 严格 JSON planner Provider。
- `apps/latex_provider`：无凭据渲染 worker 与 artifact-only `latex.render` Provider。
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
- 确认与 Artifact 实施包：
  [`docs/PHASE3_CONFIRMATIONS_ARTIFACTS.md`](docs/PHASE3_CONFIRMATIONS_ARTIFACTS.md)
- 文本 Wolfram 实施与上线验收：
  [`docs/PHASE3_WOLFRAM_TEXT.md`](docs/PHASE3_WOLFRAM_TEXT.md)
- LaTeX artifact 实施与上线验收：
  [`docs/PHASE3_LATEX_RENDER.md`](docs/PHASE3_LATEX_RENDER.md)
- 第五阶段 AgentRun 实施合同：
  [`docs/PHASE5_AGENT_RUN.md`](docs/PHASE5_AGENT_RUN.md)
- 群聊 Agent 产品与实施共识：
  [`docs/AGENT_PRODUCT_AND_IMPLEMENTATION_CONSENSUS.md`](docs/AGENT_PRODUCT_AND_IMPLEMENTATION_CONSENSUS.md)
- 采集与归档共识：
  [`docs/COLLECTION_AND_AGENT_CONSENSUS.md`](docs/COLLECTION_AND_AGENT_CONSENSUS.md)
- C0-D 签署：[`docs/C0D_ACCEPTANCE.md`](docs/C0D_ACCEPTANCE.md)
- 后续阶段设计：[`docs/FUTURE_PHASES_DESIGN.md`](docs/FUTURE_PHASES_DESIGN.md)
- 三账号高可用：[`docs/PHASE6_THREE_ACCOUNT_HA.md`](docs/PHASE6_THREE_ACCOUNT_HA.md)
- Phase 2 最终审计：[`docs/PHASE2_FINAL_AUDIT.md`](docs/PHASE2_FINAL_AUDIT.md)
