# Superlily 权威路线图

本文是当前唯一的实施路线。项目目标以根目录 [`MANIFESTO.md`](../MANIFESTO.md)
为最高约束；已经接受的 ADR、合同和生产验收只说明现有基础及其不变量，不会因为旧
Phase 编号自动产生下一项工作。

## 当前结论

截至 2026-08-30：

- P1–P4 是已经生产验收的 stable foundation；
- P5 Core Agent v1 已接受并冻结为安全参考实现，不是当前产品大脑的替换计划；
- H0–H4 已完成；
- 当前生产 Cognitive Runtime 是
  [SuperLily Nekro Runtime](https://github.com/F1Justin/superlily-nekro-runtime)
  `v2.3.3-superlily.5`，commit `3b6fb25`；
- 详细可复核身份、数据库和成本数据见 [`R0_BASELINE.md`](R0_BASELINE.md)。

旧 P6–P11、三账号 HA、通用 Web Admin、Memory/RAG、活动系统、具身入口和“替换
Nekro”不再构成排队中的阶段。只有出现真实产品需求并重新作出明确决定时，才建立
新的范围与验收门。

## 运行时方向

Superlily 不采用某个现成 Agent Harness 作为预定终点。Pi、Codex、DSH 和其他优秀
Harness 只用于研究为什么自然执行、真实反馈、临时脚手架和可逆试错有效；项目把这些
原则转化为适合长期社交主体的自有运行时。它们不是候选 backend、部署依赖、shadow
实现或 A/B 对象。

当前生产实现就是 SuperLily 的 Nekro fork。近期没有“换 Runtime”任务，也不为尚未
存在的第二实现预建多后端框架。方向决策见
[`ADR 0019`](adr/0019-cognitive-runtime-direction.md)。

## R0：冻结当前成功基线（已完成）

R0 不改变运行行为。它完成三件事：

1. 钉住 P1–P5、H0–H4、数据库、Runtime 镜像/源码和 Nekro 成本事实；
2. 删除会继续指挥施工的旧路线、未来阶段预案和已被正式签署覆盖的中间文档；
3. 建立本路线图、R0 基线和 ADR 0019 作为新的权威入口。

## R1：Renderer 的认知反馈回路（已完成）

第一项行为改动只处理已经发生的 Renderer 失败，不扩成通用治理工程：

```text
模型生成内容
  -> 可逆 preview / compile
  -> 把短而充分的真实诊断直接返回模型
  -> 模型自然修改并重试
  -> 成功产物
  -> 唯一一次受控 publish / send
```

目标是把认知试错与外部发送分开。不得再用泛化的
`renderer_execution_failed`、层层状态汇报或继续堆 prompt 约束冒充反馈。首个验收
样例应覆盖未知 LaTeX 命令/缺少宏包等真实错误：模型能看到有用诊断，自行改写表达，
失败草稿不会作为长文本泄漏到群聊。

### R1 当前实施边界（2026-08-29）

- 文档 XeLaTeX 模板加载旧 NoneBot TeX 插件使用的宏包集合，并补充 `esint`；宏包由
  Runtime 固定加载，模型仍不能提交 `\usepackage`、文件读取或 shell escape；
- worker 只把原始日志在内存中归约成固定 schema：错误类别、可选的未知命令和
  `node_id`。原始日志、路径和完整失败内容不跨 Unix socket、不进入 Core 数据库；
- Core 把内容编译错误作为 `renderer_content_error`/HTTP 422 返回调用方，数据库只记
  安全错误码；基础设施失败仍与内容错误分开；
- Nekro bridge `1.1.1` 把这份真实短诊断交回模型现有的有界 Agent loop，自身不再
  维护按请求计数的重试状态。失败草稿不自动降级成长篇普通文本；外部平台发送仍只
  发生在成功产物之后；
- 这一实施复用已有 RenderDocument、artifact、delivery intent 和唯一发送边界，不新增
  第二套 preview/publish API，也不扩成通用 Agent 治理层。

### R1 生产签署（2026-08-30）

- Core/renderer 实现来自 commit `4380154`；移除 bridge 自建重试计数、改由 Nekro
  既有有界 Agent loop 自然迭代的最终 bridge 来自 commit `da8cb17`；
- 44 项聚焦契约测试通过；最终 worker 镜像内的真实隔离探针把 `\oiint` 渲染为
  76,288 字节 PNG，并把未知命令归约为
  `undefined_control_sequence + \\notARealCommand + broken-integral`；
- 生产镜像为 Core
  `sha256:03ced7b1cf94b40f41f409c31674f450f9b5d2f71350e9ed739d78fbeb63e250`、
  document renderer
  `sha256:312915be6a72566414e88bebf6625c43f4488bb1e0c93b14618a12fec0b7560e`、
  LaTeX provider
  `sha256:3c9c5444914664a9c5a0ded10dd6e6138dc5a8166948cc284ccddf329b50bdc1`、
  worker `sha256:78c816bfbb580645d5dcd9beda0cc4d48f5bfefd2446c379c8a51a85375c6af9`；
- worker identity 与 Core render implementation 均为
  `495b98f5a8c5ce034020982a3facc2c27378fb1f6be0b4dfeaf61f7e84248d16`；Provider
  implementation 为 `fd9f2200c1c5ea26c8ebfbc27be20e5ae7671539c998950c513ba848c2f83e62`，
  最新 inventory `8c904098cf812e28f59ac17bd04f2d0f5f4d165c0dc77399125b8ba50478f4e1`
  为 healthy/self-test ok；
- 生产 Core 无发送探针中，未知命令返回 HTTP 422 / `renderer_content_error` 及
  `command=\\notARealCommand,node_id=md001`；`\oiint` 返回 201 和有效 artifact/plan，
  对应 delivery intent 数量严格为 0，因此没有产生平台测试消息；
- Nekro Runtime 仍是 `v2.3.3-superlily.4`，加载 bridge `1.1.1` 后 heartbeat 为
  online，spool 只有 committed、pending=0；Nekro、Core、document renderer、worker、
  Provider 均 running 且 restart count=0；
- 回滚保留旧 Compose 镜像以及
  `/home/justin/nekro/backups/r1-renderer-20260829/` 下 bridge 1.0.0/1.1.0 源码包。
  本次部署同时消除了 R0 记录的旧 Core 镜像无法解析数据库 `0026` 的漂移：生产 Core
  现在为 `0026_history_timeline_export (head)` 且 `alembic check` 无 drift。

至此 R1 完成。它只建立真实、短、有界的 Renderer 认知反馈和成功后唯一发送边界；
没有引入第二套 Runtime、通用工作流或新的平台副作用通道。

## R1.1：引用焦点与引用图片连续性（已完成）

R1.1 修复真实群聊中“引用后召唤莉莉”时引用关系容易被普通历史窗口稀释的问题，不改变
外部副作用边界：

- `run_agent()` 按本次触发消息的精确 message ID 绑定引用，不再把数据库中最新的人类
  消息猜作触发消息；
- 被引用消息与当前请求相邻组成独立 Reply Focus，不占普通 16 条历史和 3200 字符预算，
  因而引用 16 条以前的消息也不会被窗口裁掉；
- 被引用消息拥有独立的 4 张视觉图片预算；普通历史仍保持 1 张，更新的无关图片不能
  挤掉引用图片；
- 引用目标不在本地数据库时，OneBot 适配器在收消息时保存有界快照；若平台也没有提供
  可用内容，则明确输出 unavailable 标记，不再悄悄丢失引用语义；
- Runtime `v2.3.3-superlily.5` / `3b6fb25` 的 lint、typecheck 和 16 项聚焦回归测试通过，
  镜像 `sha256:dd054b342e1d544c1ab74329155665bce31d3a60f39dd5156f48edda06a7877e`
  的 OCI revision/version 与该 commit/tag 一致。
- 生产数据库只读回放确认：一条含 4 张图片的真实引用全部进入 `reply_focus`，普通历史
  图片仍单独保留 1 张；另一条相隔 18 条记录的真实引用中，目标与触发消息均出现在相邻
  Reply Focus。探针只组装上下文，没有调用模型或向 QQ 发送消息。

本项是对当前 Nekro 认知输入连续性的窄修复，不引入通用 router、第二 Runtime 或新的
发送通道。

## R2：Cognitive Workspace / World Effect Boundary

在 R1 的真实实现上概括最小边界：

- Cognitive Workspace 允许私有、可逆的临时文件、执行反馈、局部重试和脚手架；
- World/Core 继续掌握身份、观察事实、长期档案、权限、持久副作用、平台发送和回执；
- 只有拟产生持久或外部影响的结果跨越 effect boundary；
- Core 不要求记录模型每次内部编译、搜索或临时修改，也不把 Runtime session 当作莉莉
  的身份或世界真相。

这一阶段不预设通用多后端接口，不让新的抽象层先于真实能力增长。

## R3：演进现有 Nekro Runtime

在当前 fork 内逐步形成更自然的 Agent loop：给模型真实、有界的执行反馈，允许它在
可逆工作空间中试错和搭临时脚手架，同时保持外部副作用边界。每次改动由真实失败样本
驱动，并分别观察成功率、回复体验、token/cache 和费用；不以替换 Nekro 为目标。

## R4：参考工程研究线

持续研究 Pi、Codex、DSH 和其他 Harness，但产出必须是可验证的设计原则或现有
Runtime 的小改进。不得把研究对象写成集成路线。任何把它们变成 backend、部署依赖或
生产候选的提议，都必须由新的明确 ADR 改变本决策。

## R5：需求触发的产品扩展

多平台、Fumo、Live2D、线下活动、长期任务、检索、更多账号与控制面，只在出现具体
用户需求时立项。新工作必须回答 `MANIFESTO.md` 第五条的问题，给出用户可感知结果或
新增数据库可观察事实，并提供精确范围、失败边界和回滚路径。

## 接手与提交规则

新任务先读 `MANIFESTO.md`、本文、`R0_BASELINE.md`、当前 `git status` 和相关 accepted
ADR/合同，再判断它属于 R1–R5 的哪一项。不得从已删除文档、Git 历史中的旧 Phase
顺序或外部 Harness 的功能表自动生成任务。

每次提交只包含本次范围，并在提交后立即推送当前远端分支。
