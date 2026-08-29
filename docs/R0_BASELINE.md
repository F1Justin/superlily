# R0 当前生产基线

- 状态：frozen reference
- 核对时间：2026-08-29 CST
- 性质：只读事实记录，不改变生产行为，不授权删除数据、切换 Runtime 或开放新权限。

## 已接受基础

| 范围 | R0 结论 | 权威证据 |
| --- | --- | --- |
| P1–P2 | stable foundation | [`ACCEPTANCE.md`](ACCEPTANCE.md)、[`C0D_ACCEPTANCE.md`](C0D_ACCEPTANCE.md) |
| P3 | stable foundation | [`PHASE3_ACCEPTANCE.md`](PHASE3_ACCEPTANCE.md)、accepted ADR 0001–0014 |
| P4 | stable foundation | [`PHASE4_RENDER_DOCUMENT.md`](PHASE4_RENDER_DOCUMENT.md)、生产记录 |
| P5 Core Agent v1 | accepted / frozen reference | [`PHASE5_AGENT_RUN.md`](PHASE5_AGENT_RUN.md)、两份生产签署、ADR 0015–0017 |
| H0–H4 | completed | [`HISTORY_UNIFICATION.md`](HISTORY_UNIFICATION.md)、ADR 0018、生产恢复证据 |

P5 的冻结含义是：保留实现、schema、审计事实和安全边界作为参考，不把它继续扩成当前
产品大脑，也不删除既有数据。若未来确有需求，可以在新的明确授权下重新使用其中的
能力；R0 本身不启用它。

## Superlily Git 基线

- 分支：`codex/phase5-agent-runtime`
- commit：`cb6d104fa0f6dac9380bb325f9808149228ca021`
- tag：`nekro-raw-python-prompt-production-20260829`
- 提交含义：生产部署声明要求 Nekro 模型直接输出 raw Python，不能在首行输出
  Markdown fence language marker `python`。

本 commit 是 R0 记录时仓库引用，不等同于声称每个正在运行的 Core 容器都由该 commit
重建。

## PostgreSQL 基线

生产 `superlily` 数据库只读核对：

- PostgreSQL：17.10；
- Alembic head：`0026_history_timeline_export`；
- 数据库大小：约 23 GB；
- `public` 与 `archive` 等非系统 schema 合计 86 张 base table、352 个索引、62 个
  trigger；
- `archive.legacy_messages`：
  - `lily.nonebot.chatrecorder.v2`：8,262,010；
  - `nekro.chat_message`：1,035,247；
- `archive.conversation_mappings`：17,168。

H0–H4 的语义验收、零拒绝/零重复复跑和隔离恢复证据继续以既有签署为准。R0 不授权
删除两个旧源库或其只读备份。

### 已知部署漂移

数据库已经位于 `0026_history_timeline_export`，但 R0 核对时运行中的
`deploy-lily-core-1` 镜像（image ID
`sha256:48c39b8f3c952ae3c0cb6cd93ace4ff52bd2e9dac7d103f7a27f5c61cc7c0909`）内置
Alembic 代码尚不能解析 `0026`。容器健康检查仍为 healthy。R0 只记录该事实，不在
文档冻结提交中重建或切换 Core；后续部署工作必须单独解决并验证镜像/迁移身份一致性。

## Cognitive Runtime 基线

- 仓库：[`F1Justin/superlily-nekro-runtime`](https://github.com/F1Justin/superlily-nekro-runtime)
- 生产 checkout：`/home/justin/SuperLily-Nekro-Runtime`
- 分支：`superlily/runtime-v2.3.3`
- tag：`v2.3.3-superlily.4`
- commit：`b56e4655205c0e896b9e18a71da0b8580a3e2a12`
- 生产镜像：`superlily/nekro-agent:2.3.3-superlily.4`
- image ID：`sha256:70b4b3c8e2a60ca048b61ae798678418a53f9620ae77df39bebb3eaae09c1b3e`
- R0 核对状态：`nekro_agent` healthy。

`.3`（commit `92f8231`）是上一版历史基线，不是 current production。

## Nekro 调用与成本基线

来源是生产 Nekro PostgreSQL `exec_code.extra_data` 中 OpenRouter 返回的真实 usage，
不是本地价格估算。样本为截至 2026-08-29 20:11:59 CST 最近 100 次有费用的
`google/gemini-3-flash-preview` 调用（起点 15:44:58 CST）：

| 指标 | 数值 |
| --- | ---: |
| 样本数 | 100 |
| input token 中位数 | 7,248.5 |
| cached token 中位数 | 3,956 |
| 有 cached token 的调用 | 100 / 100 |
| 平均费用 | $0.002357557 |
| 费用中位数 | $0.002226883 |
| sandbox 成功 | 87 / 100 |

该样本包含当前“每次消息带一张历史图片”的生产设置，也包含 `.4` raw-Python prompt
发布后的调用。它是后续 R1/R3 比较成功率、缓存和费用的冻结参照，不代表质量目标。

## 文档权威与清理边界

R0 删除了旧总计划、P6–P11 未来阶段设计、三账号 HA 预案、旧 Agent/采集共识路线、
已被正式生产签署覆盖的 Phase 2 中间审计和 Phase 5a 预生产 shadow 报告。内容仍可从
Git 历史恢复，但不再留在工作树中影响新任务排序。

保留 accepted ADR、当前合同、正式生产验收、历史迁移/恢复证据以及开发、部署、安全
文档。保留不等于继续按旧 Phase 扩建；它们只约束已经存在的 stable foundation。

R0 文档改动的回滚方式是对相应 Git commit 执行普通 revert。R0 没有生产数据或运行时
副作用需要回滚。
