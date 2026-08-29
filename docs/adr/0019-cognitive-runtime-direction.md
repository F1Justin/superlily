# ADR 0019：认知运行时方向与参考工程定位

- 状态：accepted
- 日期：2026-08-29
- 取代：旧 P6–P11 中的 Runtime 替换路线和任何把外部 Harness 视为候选 backend 的设想

## 背景

P1–P4 已建立可观察、规范事实、工具 authority、artifact 与投递边界。P5 Core Agent
v1 证明了模型不等于 authority、proposal 不等于 invocation、执行不等于发送，已经
完成其安全参考价值。当前真实群聊的认知运行由 SuperLily Nekro Runtime fork 承担。

Renderer 的近期失败同时证明，若 Runtime 只能得到泛化错误码，却必须经过多层状态和
发送协议才能看到真正执行反馈，模型就无法像优秀 coding agent 那样自然修正。这是
认知工作空间与世界副作用边界的问题，不是引入另一个 Harness 的理由。

## 决策

1. SuperLily 自己拥有 Cognitive Runtime 的设计方向；当前生产实现是 Nekro fork。
2. 近期不替换 Runtime，也不为假想的第二实现建立多 backend 平台。
3. Pi、Codex、DSH 与其他 Agent Harness 仅是研究和设计启发来源。它们不是候选
   backend、部署依赖、shadow/canary 实现或 A/B 对象。
4. 可以吸收的原则包括：模型直接获得真实、有界的执行反馈；认知过程可在私有、可逆
   工作空间中试错；临时脚手架不需要逐步升级为世界事实；外部副作用另过明确边界；
   context/session 不成为莉莉的身份或长期世界真相。
5. P5 Core Agent v1 冻结为 accepted reference。保留其代码、schema、审计数据和安全
   不变量，但后续自然 Agent loop 优先在现有 Nekro Runtime 中演进。
6. 任何把参考 Harness 改为集成对象的决定，必须由新的明确 ADR 取代本决策，不能从
   调研、原型或路线图措辞中默示产生。

## 暂不决定

本 ADR 不提前定义完整 Cognitive Workspace API、Effect 数据模型或 Renderer 实现。
R1 先用真实 Renderer 反馈链路证明模式，R2 再从实现中提炼最小边界。它也不授权新的
平台发送、历史检索、写工具、长期任务或身份迁移。

## 结果

- 路线从“寻找/接入下一种 Harness”改为“演进当前生产 Runtime”；
- 外部工程研究产出必须落成原则、实验或现有 Runtime 的可验证小改进；
- Core 继续拥有世界事实、权限与持久副作用，Runtime 获得自然认知试错所需的空间；
- 未来确有第二实现需求时再设计 seam，不提前支付抽象和运维成本。
