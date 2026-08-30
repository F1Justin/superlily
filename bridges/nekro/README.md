# Nekro bridge

## 1.2.0 QQ 名称观测

1.2.0 分别上报 Nekro 已有的 `sender_name`（QQ 账号昵称）与
`sender_nickname`（群内显示名），并给群消息补充已观测群名。启动后立即拉取 OneBot
群清单，之后默认每 6 小时重查；OneBot 尚未连接时每 30 秒短轮询。清单快照复用 durable
reporter，不触发模型、matcher 或 QQ 发送。

- `GROUP_INVENTORY_SECONDS=21600`

## 1.0.0 Core-owned Agent product adapter

1.0.0 does not add another Nekro command catalog or planner. In an exact
allowlisted chat, an explicitly addressed message is atomically offered to
Core. Core owns AgentRun, the optional Wolfram loop, budgets and the native-text
delivery intent. Only when Core returns `accepted=true` does the callback return
`BLOCK_TRIGGER`, preventing Nekro's own configured chat model from also
replying. Core rejection or transport failure remains fail-open
`CONTINUE`.

A supervised delivery worker leases at most one Core intent, verifies the text
hash, replies to the triggering platform message through OneBot, and reports a
fenced terminal receipt. A successful platform send whose completion cannot be
committed is not retried.

Initial production values are deliberately exact:

- `AGENT_ENABLED=false` by default;
- `AGENT_CANARY_CHAT_KEYS=onebot_v11-group_708309706` only during canary;
- `AGENT_DELIVERY_POLL_SECONDS=0.5`.

## 0.9.2 真实模型纠错迭代

Nekro 的普通 sandbox RPC 返回值只会写入系统消息，不会自动触发第二轮模型调用。
0.9.2 在首次 Markdown 校验或 XeLaTeX 编译错误时抛出仅含安全修正提示的内部异常，
让 Nekro 把当前沙盒标记为 `ERROR`，沿既有 `run_agent` 调试路径真实调用模型一次。
第二次内容失败由 Bridge 在当前 RPC 内立即发送普通文本；Core/Renderer 传输故障也
直接文本回退。同一请求的后续调用仍会被有界状态抑制，平台投递结果不明确时仍然禁止
回退，避免重复消息。成功渲染不会触发额外模型调用。

## 0.9.1 有界纠错重试与静默文本回退

模型在 Python 沙盒中必须用原始三引号字符串提交 Markdown。Core 会在 XeLaTeX
之前拒绝被 Python 转义破坏的数学控制字符和不平衡花括号；Bridge 对内容错误只允许
一次修正后的重试。第二次内容失败、基础设施失败或不可重试的请求会确定性发送普通
文本并封住同一请求的后续重复发送。所有 `INTERNAL_RENDER_*` 返回值只用于控制模型，
不得出现在用户回复中；平台完成不明确时仍然禁止重试。

## 0.9.0 普通 Markdown 模型入口

模型不再构造逐段 blocks JSON。canary prompt 只要求调用
`submit_rendered_markdown(markdown_text)`，直接提交一整段普通 Markdown；Core
确定性转换标题、段落、列表、引用、代码、表格和 display math，文本字段继续支持
`**加粗**` 与 `$行内公式$`。HTML、链接和 Markdown 图片只显示为普通文字，不访问
远程或本地资源。

旧 `submit_render_document(document_json)` 保留为兼容回滚入口，但不再出现在模型
提示中。0.9.0 不改变现有两个 exact chat allowlist、一次性 delivery intent 或
OneBot completion 语义。

## 0.5.1 后台任务自恢复

从 0.5.1 起，普通上报 worker 与 durable spool worker 都会观察异常退出、记录不含异常正文的错误类型，并在一秒退避后自动重建。正常 shutdown 不计为故障。heartbeat 的单轮构造也有独立异常边界；累计失败数、最后异常类型、两个 worker 的运行状态和重启次数会进入后续 heartbeat 元数据。修复只提高采集链路的可观测性与自恢复能力，不改变 Nekro 的 matcher、claim、回复或 fail-open 行为。

This is a Nekro 2.2.1 local plugin. It uses the public plugin callback for
normalized human messages and the public NoneBot event hook for confirmed
OneBot `message_sent` and supported platform-action events. Failed send API
calls are recorded separately.

Copy or symlink `superlily_bridge/` to:

```text
/home/justin/nekro/plugins/workdir/superlily_bridge/
```

Then restart Nekro once and configure `Superlily.core_bridge` in the plugin
panel:

- `CORE_URL=http://lily-core:8000`
- `CORE_TOKEN=<nekro-agent instance token>`
- `INSTANCE_ID=nekro-agent`
- `BOT_ID=2022692714`
- `CLAIM_ENABLED=false`
- `CLAIM_TIMEOUT_SECONDS=10.0`
- `CLAIM_ATTEMPTS=2`
- `CLAIM_RETRY_BACKOFF_SECONDS=0.1`
- `REPORT_TIMEOUT_SECONDS=10.0`
- `REPORT_ATTEMPTS=3`
- `REPORT_RETRY_BACKOFF_SECONDS=0.1`
- `AGENT_ENABLED=false`
- `AGENT_CANARY_CHAT_KEYS=`
- `AGENT_DELIVERY_POLL_SECONDS=0.5`

The container must join the `superlily_bus` network; see
`deploy/nekro-compose.override.yml`.

Bridge 0.5.0 reports the same factual reaction, group/friend recall and poke
mapping as Lily. The two packages contain an identical, dependency-free action
normalizer and fixture suite. Reaction count is stored as platform-observed
state, not inferred add/remove intent or human-feedback meaning. Poke display
text and numeric action/effect IDs are bounded; jump/image URLs and internal QQ
UIDs are not retained and their omission is explicit. Missing required fields
remain `partial`/`unavailable`. Action notices never enter the claim path and
do not trigger Nekro's agent.

Do not hot-reload this plugin during the first smoke test. Its global NoneBot
hooks are guarded against duplicate registration, but a clean process restart
is easier to audit.

Claim evaluation and ACK, as well as background reports, retry transient
transport, 429, and 5xx failures with the same idempotency key before
incrementing their failure counters.

With `CLAIM_ENABLED=true`, only an enforced deny returns Nekro's
`BLOCK_TRIGGER`: the incoming message remains in Nekro history but does not
start the agent. Failure to reach Core returns `CONTINUE`. Nekro's current
public plugin API aggregates all plugin signals after this bridge returns and
offers no post-aggregation callback; a later `FORCE_TRIGGER` may override the
block. The bridge therefore also installs an exact-source OneBot outbound API
guard covering both the active event and any later Nekro scheduler task. It
records send attempts made by earlier plugins and acknowledges the deny only
when the event is matched, no earlier send exists, and the guard is installed.
Missing context, an earlier send, or ACK failure safely leaves the target at
`abstain` while the local guard still suppresses later sends.

When the canonical decision targets Nekro but deny-before-allow coordination
safely returns `abstain`, the bridge retains that decision only as a pending
response-correlation hint. It does not convert the abstain into authorization;
Nekro's existing matcher still decides whether a response is produced.

Response-correlation state expires only while idle. A bounded bridge task binds
the pending source as soon as Nekro exposes the scheduler task token; a task
that is still active then retains its exact source binding past the ordinary
cache TTL. Slow model retries and tool work therefore do not turn a later
successful response into an unlinked response. A different task token cannot
revive an expired idle binding.
