# R3.1 对话让步与 Web Search canary

状态：**技术链路可用，全量产品验收尚未完成，生产保持关闭。**

本页记录 2026-08-30 的最小实现与隔离回放。它不是 R3.1 签署，也不授权修改生产模型
组、发送 QQ canary 或扩大流量。

## 已验证的技术路径

- Runtime 可按模型组开关向 OpenRouter Chat Completions 提供
  `openrouter:web_search`；当前 canary 固定为 Parallel `turbo`、最多一次、最多三个结果、
  每个结果最多 1000 字符；
- 初始回答可获得搜索工具，后续 sandbox 调试迭代不再提供，避免一次 Agent loop 重复收费；
- URL citation 只保留 URL、标题和文本索引；网页正文不持久化，只累计结果字符量；
- `usage.server_tool_use.web_search_requests` 是供应商观测事实。它缺失而响应存在 citation
  时，Runtime 另记 `web_search_inferred_requests=1`、
  `web_search_observation_source=url_citation`，不伪造供应商计数；
- 模型组开关开启时，每个初始请求统一获得 server tool；Runtime 不读取消息内容来决定
  是否提供。`web_search_offered` 只记录能力是否由模型组开关提供，实际是否调用由供应商
  usage 或 citation 观测。

OpenRouter 当前文档仍将 server tools 标为 Beta。Parallel `turbo` 标称约 200ms、每千次
1 美元；`max_uses=1` 和顶层 `max_tool_calls=1` 共同约束一次请求的搜索次数：

- <https://openrouter.ai/docs/guides/features/server-tools/web-search>

## 真实种子集

只读导出脚本为 [`../scripts/r3_1_reaction_eval.sql`](../scripts/r3_1_reaction_eval.sql)。生产
Core 实跑得到 51 行，其中 50 行能关联到莉莉回复；未关联目标与空回复保留为事实，不在
SQL 中赋予负反馈语义。导出文件保存在 git 忽略的 `run/`，权限为 `0600`，避免把私人群聊
内容提交进仓库。

评测必须把 social epistemics 与 search 分开：

1. `failed_to_defer`：参与者在告知或纠正，莉莉应接受当前对话前提，不以旧知识争辩；
2. `search_required`：参与者要求莉莉负责回答当前或不确定的外部事实；
3. 视觉/OCR、上下文、意图、工具和风格失败另行标注，不因 reaction 一律路由搜索。

## Prompt 瘦身

旧 system prompt 将真实性、验证、行动语义、交付和 raw Python 契约重复了多次，也把
沙盒、插件框架和基础方法写成了教程。当前实现把它收敛为六个小块：

1. `Core Policy`：真实性、证据边界和可信运行时标签；
2. 原样保留的 `Character`；
3. `Runtime Contract`：raw Python 与代码作为行动媒介；
4. `Sandbox` 和当前 `_ck`；
5. 当前真正可用的方法签名；
6. prompt 尾部的一句 raw Python 最终约束。

`basic` 插件的四个方法说明也已从教程式 docstring 压缩为签名和一个必要行为约束；插件
实现未改变。通用插件系统教程、few-shot 边界、chat key 格式表、正反代码示例、重复的
Warning 和交付说明均已删除。何时搜索与如何回应参与者告知不在 Runtime system prompt
中规定，由当前人格 prompt 和模型决定。

以同一人设和 `basic + Lily Core Bridge` 插件集合渲染：

- 旧版：371 行、17,912 字符；
- 新版固定 prompt：85 行、3,886 字符，减少 78.3%。

以上新版使用 2026-08-30 生产数据库中当前的莉莉人格快照；Runtime 代码没有写入或改动
该人格。

核心原则为：

```text
Never claim an action, verification, result, file, or delivery that did not
actually occur.

Intermediate observations, errors, and partial signals are not final conclusions.

If requested verification is unavailable or incomplete, state the uncertainty
rather than inventing certainty.
```

固定 prompt、工具对每个初始请求统一可见的同场景单次回放中：

- “草间弥生去世了”获得了搜索工具，但模型没有调用搜索、没有 citation，直接按当前
  人格回应参与者告知；
- “草间弥生是什么时候去世的？请核实后只回答日期”获得同一工具并实际搜索，随后只回答
  日期；
- 两次请求使用完全相同的 system prompt，不存在 Runtime 内容分类；
- 单次请求的 prompt tokens 为 1,622/4,090。费用和延迟也
  下降，但单次样本不足以作为性能结论。

因此当前结论是：**删除旧 prompt 的重复和冲突显著改善了原失败样本，但单次样本尚未
证明完整 social epistemics。** 在 51 条真实回放通过前，不得开启生产模型组开关。

## 剩余退出条件

- 人工完成 51 条种子集标签，并分别报告 `failed_to_defer` 与 `search_required`；
- 用当前人格配置通过完整 `failed_to_defer` 回放；若仍不稳定，由人格设计继续调整，
  Runtime 不恢复内容分类或案例式行为补丁；
- 对完整回放集报告搜索召回、误搜索、p50/p95 延迟、缓存命中与每千条消息增量费用；
- 只有以上全部通过，才讨论生产小流量 canary 与签署。
