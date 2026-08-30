# Superlily

Superlily 是 [`MANIFESTO.md`](MANIFESTO.md) 约束的 Lily Core 与长期社交主体工程。
当前稳定基础已经覆盖事件观察与规范关联、持久采集、工具 authority、可恢复执行、
artifact/Renderer、平台投递和冻结的 Core Agent v1 参考实现；旧群聊历史也已统一进入
PostgreSQL archive read model。

当前生产认知运行时不是 Core Agent v1，而是独立维护的
[SuperLily Nekro Runtime](https://github.com/F1Justin/superlily-nekro-runtime)。项目将在
现有 Runtime 内逐步改善真实执行反馈、可逆认知工作空间和自然 Agent loop，不会把
Pi、Codex、DSH 等参考工程当成候选 backend 或集成目标。

这是一个由个人维护、长期运行于真实社交环境中的生产研究项目，不是承诺稳定 API、
托管服务或商业支持的通用 Bot 发行版。项目大量使用 AI-assisted / vibe-coded 开发，
但生产变更以 Git 身份、合同测试、迁移证据、成本数据和可回滚验收为准。

当前权威状态：

- P1–P4：stable foundation；
- P5 Core Agent v1：accepted / frozen reference；
- H0–H4：completed；
- Cognitive Runtime：[SuperLily Nekro Runtime](https://github.com/F1Justin/superlily-nekro-runtime)；
- 当前生产 Runtime 的 tag、commit 与镜像唯一以
  [`deploy/nekro-runtime.lock.yml`](deploy/nekro-runtime.lock.yml) 为准，README 不重复手写版本；
- 后续工作：以 [`docs/ROADMAP.md`](docs/ROADMAP.md) 的 R0–R5 为唯一顺序。

问题与 PR 可以作为外部反馈提交，但维护者不承诺响应时间、兼容性周期或为第三方部署
提供免费支持；范围见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。安全问题请按
[`Security Policy`](.github/SECURITY.md) 私下报告，不要公开附带凭据或私人聊天数据。

## 目录

- `packages/contracts`：版本化采集、工具、Agent 和投递合同；
- `apps/core`：FastAPI Core、PostgreSQL 模型、authority 与审计服务；
- `apps/*_provider`：独立、受边界约束的工具和模型 Provider；
- `bridges/lily_nonebot`、`bridges/nekro`：平台观察与接入桥；
- `registry`：Git-reviewed descriptor、Provider 与精确 rollout authority；
- `deploy`：Docker Compose 和部署配置；
- `docs`：当前路线、合同、ADR、运维说明和正式验收证据。
- 外部 Runtime 仓库：
  [`F1Justin/superlily-nekro-runtime`](https://github.com/F1Justin/superlily-nekro-runtime)，
  精确生产身份由 `deploy/nekro-runtime.lock.yml` 锁定。

## 权威入口

- 项目宪法：[`MANIFESTO.md`](MANIFESTO.md)
- 唯一路线：[`docs/ROADMAP.md`](docs/ROADMAP.md)
- 双仓统一目标与工作看板：
  [`SuperLily GitHub Project`](https://github.com/users/F1Justin/projects/1)
- R0 生产基线：[`docs/R0_BASELINE.md`](docs/R0_BASELINE.md)
- 架构决策：[`docs/adr/README.md`](docs/adr/README.md)
- 当前架构：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- HTTP/数据合同：[`docs/CONTRACTS.md`](docs/CONTRACTS.md)
- 数据库与外部接入：[`docs/DATABASE_INTEGRATION.md`](docs/DATABASE_INTEGRATION.md)
- 部署与运维：[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
- 本地开发：[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- 安全边界：[`docs/SECURITY.md`](docs/SECURITY.md)

## 冻结合同与验收证据

- P1–P2：[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)、
  [`docs/C0D_ACCEPTANCE.md`](docs/C0D_ACCEPTANCE.md)
- P3：[`docs/PHASE3_TOOL_REGISTRY.md`](docs/PHASE3_TOOL_REGISTRY.md)、
  [`docs/PHASE3_ACCEPTANCE.md`](docs/PHASE3_ACCEPTANCE.md)
- P4：[`docs/PHASE4_RENDER_DOCUMENT.md`](docs/PHASE4_RENDER_DOCUMENT.md)
- P5：[`docs/PHASE5_AGENT_RUN.md`](docs/PHASE5_AGENT_RUN.md)、
  [`docs/PHASE5_PRODUCTION_ACCEPTANCE.md`](docs/PHASE5_PRODUCTION_ACCEPTANCE.md)、
  [`docs/PHASE5_AGENT_PRODUCT_ACCEPTANCE.md`](docs/PHASE5_AGENT_PRODUCT_ACCEPTANCE.md)
- H0–H4：[`docs/HISTORY_UNIFICATION.md`](docs/HISTORY_UNIFICATION.md)、
  [`docs/adr/0018-legacy-history-read-model.md`](docs/adr/0018-legacy-history-read-model.md)
- Nekro prompt/cache：[`docs/NEKRO_PROMPT_OPTIMIZATION.md`](docs/NEKRO_PROMPT_OPTIMIZATION.md)

这些文件约束已经存在的基础，不会因 Phase 编号自动授权继续施工。被 R0 删除的旧路线
和中间文档仍保存在 Git 历史中，不再出现在当前工作树。
