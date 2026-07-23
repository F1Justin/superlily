# 第四阶段：统一 Renderer 与投递边界

## 目标与用户入口

第四阶段把内容、排版、平台能力和实际发送拆开。工具只返回经过 descriptor
校验的结构化结果；Core 把结果转换成 `RenderDocument`，选择与目标 adapter
能力相符的 `DeliveryPlan`，平台 bridge 只执行计划并回报真实平台结果。

Nekro 的模型入口不再要求模型逐段选择 `text`、`math` 或 `list`。0.9.0 bridge
提供 `submit_rendered_markdown(markdown_text)`：模型提交一整段普通 Markdown，
Core 再确定性转换为安全 AST。支持：

- `#` / `##` 标题、自然段、`-` / `1.` 列表和 `>` 引用；
- 成对 `**加粗**` 与 `$行内公式$`，无需拆分段落；
- 独占行的 `$$...$$`、代码围栏和简单管道表格；
- 未受信 HTML、链接和 Markdown 图片只显示为经过转义的文字，不访问网络、
  本地文件或回调。

旧 `submit_render_document(document_json)` 仍保留为结构化兼容入口，但不再注入
模型提示。这样既减少模型 token，又避免截图中 `**` 和 `$...$` 被当作普通文本。

## RenderDocument 1.3

当前版本为 1.3，继续读取 1.0–1.2，并包含以下有界节点：

- `text`、`paragraph`、`heading`、`list`、`quote`；
- `math`、`code`、`table`；
- `notice`、`warning`、`error_summary`、`progress`；
- 仅含展示字段和不可执行 action ID 的 `card`；
- `image`、`artifact_ref`、`group`、`alternative`。

1.1 起所有节点必须具有唯一稳定 `node_id`；1.2 起文本字段识别成对
`**strong**` 和单美元行内公式；1.3 加入段落、卡片、警告和显式错误摘要。
节点不能包含脚本、平台原生 segment、可执行 callback、远程资源或本地路径。

## 内容寻址 artifact

每份制品保存并独立校验：

- SHA-256、MIME、字节数、像素尺寸和存储对象键；
- renderer/tool producer、source invocation、render attempt 和 fence；
- data classification、canonical scope、无害文件名和 accessibility text；
- expiry、retention、逻辑删除时间和删除原因。

逻辑删除立即令下载返回 410，但保留最小审计元数据。同一 digest 仍被活跃 tool
artifact 或其他未到期 render artifact 引用时，只删除当前逻辑引用；最后一个引用
消失后才移除物理对象。LaTeX 工具结果通过 `tool-artifact-passthrough-v1`
直接复用原始 PNG 字节，不重新编译、压缩或改变 hash。

普通文档不做跨会话全局缓存。幂等请求只在同一 instance、同一规范化
RenderDocument 记录内复用，并要求完整 Renderer snapshot hash 一致；该 snapshot
同时绑定 contract version、resolved document hash、能力决策、Renderer profile
和实现/字体包 hash。实现升级、能力变化、输入变化或不同幂等作用域都会产生新 attempt。
Tool artifact passthrough 还额外绑定 source invocation 与 source artifact，因此
conversation/sensitive 内容不会因相同字节或相似输入跨作用域复用。

## 能力规划与第二 adapter 证明

Core 在 renderer 执行前规范化目标能力，并保存：

- capability snapshot hash；
- 选中及拒绝的 `alternative`、拒绝原因和缺失能力；
- resolved document hash、decision hash；
- 有序 payload、最终 family 和全部 degradation reason。

QQ profile 支持图片时选择内容寻址 PNG；固定的受限 adapter simulator 只支持
文本，因此确定性退化为 semantic plain text。相同 fixture 在两种 profile 下具有
相同语义文本 hash，simulator 不含平台连接或发送权限。

## 兼容命令迁移

以下转换只接受精确 tool ID、descriptor 版本和已成功 invocation 的服务端保存结果：

| 路径 | RenderDocument | 发送前验证 | 回滚 |
| --- | --- | --- | --- |
| `status.inspect@1.0.2` | 状态 `card` | principal、output schema/hash | 关闭 status flag |
| `wolfram.run@1.0.0` | 有界 Wolfram `code` | principal、output schema/hash | 旧 `/wf` matcher |
| `latex.render@1.0.0` | 原 artifact `image` | invocation/attempt/artifact 六项绑定 | 旧 `/tex` matcher |
| `/help` | 结构化命令 `list` | bridge 身份与 exact conversation | 旧 help matcher |

Lily bridge 0.6.0 的新 matcher 为 `block=False`。在 Core 成功创建 delivery intent
以前，它不会阻断旧 matcher；计划未命中、`ledger_only`、网络失败或无效凭证都会
安全回到旧路径。一旦 intent 已存在，bridge 才停止传播并执行一次平台发送。平台
可能已经接受但未返回 message ID 时记为 `ambiguous`，绝不盲目重发。

Git authority `phase4-command-canary-20260723@1.0.0` 只包含：

- 群 `1080353942` 与 `861651713`；
- caller `command`；
- 上述三个精确 descriptor hash 和三个精确 Provider；
- 总计 200 次调用、最长 23 小时 59 分钟；
- 硬回滚目标 `ledger_only`。

`/help` 不执行工具，只受 Renderer allowlist 和独立 bridge flag 约束。

## API

- `POST /v1/render-documents`：提交已验证 AST；
- `POST /v1/markdown-documents`：提交有界 Markdown 并在 Core 内降为 AST；
- `POST /v1/tool-invocations/{id}/render-result`：从保存的成功工具结果渲染；
- `POST /v1/help-documents`：渲染结构化命令帮助；
- `GET/DELETE /v1/render-artifacts/{id}/content`：受 scope 约束的读取与删除；
- `POST /v1/render-artifacts/{id}/delivery-intents`：创建一次性投递意图；
- `POST /v1/render-delivery-intents/{id}/complete`：记录成功、失败或不明确完成。

所有创建接口都有稳定幂等身份。重复 delivery intent 不会获得第二次发送许可；
过期 pending intent 原子变为 `ambiguous` 并追加投递证据。

## 安全与故障矩阵

自动化覆盖：

- Markdown/HTML/SVG、远程 URL、本地路径和未知字段均不会变成主动内容；
- LaTeX 文件、网络、字体、Lua、宏定义和展开循环命令被拒绝；
- 图片字节、MIME、尺寸、symlink、对象键和 canonical document 大小受限；
- 超量 blocks、节点、表格、代码、数学和 artifact 引用失败关闭；
- 重复请求、renderer 失败、过期 artifact、stale render fence 和恢复；
- stale tool fence、取消竞态、unknown completion；
- delivery intent 过期、重复 completion 和冲突 completion；
- 共享与非共享 artifact 的逻辑/物理删除。

2026-07-23 的实现证据：

- SQLite 全量：512 passed，4 skipped；
- PostgreSQL 17 Alembic：`upgrade head -> downgrade base -> upgrade head ->
  alembic check` 通过，head 为 `0019_phase4_planning`；
- PostgreSQL 17 全量：512 passed；
- exact Renderer snapshot 缓存回归在 SQLite 与独立临时 PostgreSQL 17
  上分别通过；
- PostgreSQL 测试发现并修复 passthrough artifact 在无 ORM relationship 时的
  外键写入顺序：同一事务显式先 flush fenced attempt，再插入 artifact。

## 生产部署与签署

2026-07-23 的生产部署以提交 `a63940b6a8f82420c0c22e41ad5005af215d0c64`
为 Core/Renderer authority，并在提交 `aa84f62` 收入生产发现的领取延迟修复：

- 迁移前 PostgreSQL 自定义格式备份位于
  `/home/justin/backups/superlily/20260723-phase4-complete/`，备份大小
  195,407,181 字节，SHA-256 为
  `4f4f48b73d2b0283dc7a8577008a7bf1a188f9d3d7c23b0105118c0636009465`，
  `pg_restore --list` 通过；
- 生产 Alembic 已从 `0018_render_attempt_delivery` 线性升级到
  `0019_phase4_planning (head)`，`alembic check` 无 drift；
- Git-bound plan `phase4-command-canary-20260723@1.0.0` 的规范化 hash 为
  `9545953d724b3fa16d0c493c64f4794dff8e18fa4909c5a9bd9cea6dde756c97`。
  它经 control login、reauth、preview、CAS apply 和 append-only audit 激活，
  mutation ID 为 `eea06f8d-e5fd-4cdf-99a2-3852f87e8b60`；激活后控制面重新恢复
  0 operator；
- Lily 0.6.0 与 Nekro 0.9.0 均已加载；Lily 的命令开关和 Renderer allowlist
  都只包含两个生产 scope，Nekro 的模型提示只暴露普通 Markdown 入口；
- RenderDocument 1.2 Markdown-lite 的既有生产探针在 1116 ms 内完成，没有
  degradation、delivery intent 或平台发送；强调、行内公式、未配对标记和代码围栏
  语义均符合 reviewed parser。

灰度发现 status descriptor 的 5 秒总期限与 Provider 原 5 秒空闲退避上限存在
竞态：一次调用在领取前收敛为 `timed_out/deadline_expired`。提交 `aa84f62`
把 status、Wolfram、LaTeX 三类 Provider 的生产空闲上限统一为 1 秒，完整 SQLite
套件 512 passed / 4 skipped；替换后三个最新 Provider 心跳均为
`healthy`、`current_concurrency=0`、`self_test=ok`。

生产账本保留了四条兼容路径的已完成平台回执：status、Wolfram、中文 LaTeX 和 help
各有一次 `succeeded` delivery intent 与平台 message ID。另有一个在平台发送前因
本机凭据文件权限失败而终结为 `failed` 的 intent；没有 pending/expired intent，
也没有 queued/leased/running invocation。一次完全后台、私聊标识、无 Renderer、
无 QQ 连接的回滚演练得到 invocation
`691cdb0d-a61a-4c67-ae4a-1fbe8fbbea3f`，结果为
`recorded_only/rollout_fallback_ledger_only`，证明 plan 未命中时不会领取或发送。

19:35 CST 的稳定窗口复核距三个 Provider 替换超过 21 分钟，距最后一条平台投递
超过 19 分钟，距无发送回滚演练超过 16 分钟。Core、Renderer、Nekro、PostgreSQL、
三个 Provider 与 LaTeX worker 均为 running，带 healthcheck 的服务均 healthy，
容器和 Lily service 的 restart count 都为 0；三条最新 Provider 心跳新鲜且
`healthy/current_concurrency=0/self_test=ok`，Core readiness 为
`status=ok/database=ok`，控制面 operator 数为 0，账本中没有 active invocation
或 pending delivery intent。该窗口内没有新的 failed/timed_out/ambiguous 记录。

生产操作员随后明确指出，不应把阶段验收理解为可以向公开群主动发送合成测试内容。
从该纠正起停止所有公开群测试；后续生产验证默认只使用自动化、后台账本和无发送探针。
任何新的群消息、撤回或其他平台动作都必须获得当次明确授权。

## 部署配置与回滚

Core 继续保持精确 Renderer allowlist：

```dotenv
SUPERLILY_RENDER_MODE=canary
SUPERLILY_RENDER_CANARY_CONVERSATIONS_JSON=["onebot_v11-group_1080353942","onebot_v11-group_861651713"]
```

Nekro：

```text
RENDER_ENABLED = true
RENDER_CANARY_CHAT_KEYS = onebot_v11-group_1080353942,onebot_v11-group_861651713
```

Lily：

```dotenv
LILY_CORE_PHASE4_COMMANDS_ENABLED=true
LILY_CORE_PHASE4_COMMAND_CANARY_GROUPS=1080353942,861651713
LILY_CORE_PHASE4_STATUS_ENABLED=true
LILY_CORE_PHASE4_WOLFRAM_ENABLED=true
LILY_CORE_PHASE4_LATEX_ENABLED=true
LILY_CORE_PHASE4_HELP_ENABLED=true
```

三类独立 Provider 的空闲领取退避上限统一为 1 秒。原先 5 秒上限会与
`status.inspect` 的 5 秒总期限形成竞态：请求若恰好落在最长空闲窗口，可能尚未被领取
就先由 Core 收敛为 `timed_out`。1 秒上限同时降低 status、Wolfram 与 LaTeX 的首包
等待，不改变每个工具的执行预算、并发上限或安全边界。

回滚分三层且互不依赖：

1. 任一命令 flag 设为 false，立即恢复对应旧 matcher；
2. 暂停 rollout plan 或把 Core 上限恢复 `ledger_only`，三个工具均不再执行；
3. `SUPERLILY_RENDER_MODE=off` / bridge Renderer flag 关闭统一渲染。

生产签署不扩大群范围，也不开放自然语言工具调用；后者属于第五阶段。计划到期或暂停
后保持 `ledger_only` 回落，新的生产 scope 必须重新走 Git-bound plan 和 control
preview/apply。
