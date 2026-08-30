# Superlily 权威路线图

本文是当前唯一的实施路线。项目目标以根目录 [`MANIFESTO.md`](../MANIFESTO.md)
为最高约束；已经接受的 ADR、合同和生产验收只说明现有基础及其不变量，不会因为旧
Phase 编号自动产生下一项工作。

## 当前结论

截至 2026-08-29：

- P1–P4 是已经生产验收的 stable foundation；
- P5 Core Agent v1 已接受并冻结为安全参考实现，不是当前产品大脑的替换计划；
- H0–H4 已完成；
- 当前生产 Cognitive Runtime 是
  [SuperLily Nekro Runtime](https://github.com/F1Justin/superlily-nekro-runtime)
  `v2.3.3-superlily.4`，commit `b56e465`；
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

## R1：Renderer 的认知反馈回路

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

代码候选已通过真实 `\oiint` 编译、未知命令定位及 Worker→Core→bridge 契约测试；只有
生产镜像、身份、bridge 和无外部发送探针全部钉住后，R1 才标记完成。

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
