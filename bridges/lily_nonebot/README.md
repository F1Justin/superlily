# Lily NoneBot bridge

## 0.7.0 QQ 名称观测

0.7.0 将 QQ 账号昵称和群名片作为不同字段上报，并为群消息补充已观测群名。启动后会
立即拉取一次 OneBot 群清单，之后默认每 6 小时重查；启动时 OneBot 尚未连接则每 30
秒短轮询，成功后才进入正常间隔。清单快照走现有 durable reporter，不增加发送 QQ
消息或其他外部副作用。

```dotenv
LILY_CORE_GROUP_INVENTORY_SECONDS=21600
```

## 0.6.0 第四阶段命令兼容

0.6.0 为 `/status`、纯文本 `/wf`、`/tex` 和 `/help` 增加统一 Renderer
兼容入口。bridge 固定三个经过 Git 审阅的 descriptor 身份，创建 command
invocation，等待独立 Provider 完成，再请求 Core 生成 capability-aware
delivery plan。工具和 Provider 不持有 QQ 发送能力。

新 matcher 默认关闭且 `block=False`。在精确 canary 命中并成功创建 delivery
intent 以前，Core 超时、`ledger_only`、rollout 未命中和其他失败都会让旧 matcher
继续处理；intent 创建以后才阻断旧路径，防止双发。平台完成不明确时记录
`ambiguous`，不自动重试。

```dotenv
LILY_CORE_PHASE4_COMMANDS_ENABLED=false
LILY_CORE_PHASE4_COMMAND_CANARY_GROUPS=1080353942,861651713
LILY_CORE_PHASE4_STATUS_ENABLED=true
LILY_CORE_PHASE4_WOLFRAM_ENABLED=true
LILY_CORE_PHASE4_LATEX_ENABLED=true
LILY_CORE_PHASE4_HELP_ENABLED=true
LILY_CORE_PHASE4_COMMAND_TIMEOUT_SECONDS=10
```

任一命令 flag 都是独立回滚开关；全局 flag 关闭后，所有命令恢复旧路径。图片输入、
Wolfram 图片/音频输出仍留在旧 `/wf`，当前 reviewed `wolfram.run@1.0.0` 只迁移
有界文本表达式与文本结果。

## 0.5.1 后台任务自恢复

从 0.5.1 起，普通上报 worker 与 durable spool worker 都会观察异常退出、记录不含异常正文的错误类型，并在一秒退避后自动重建。正常 shutdown 不计为故障。heartbeat 的单轮构造也有独立异常边界；累计失败数、最后异常类型、两个 worker 的运行状态和重启次数会进入后续 heartbeat 元数据。这样即使 bot 仍在收消息，也不会再让一个悄然退出的后台协程长期把实例显示成假离线。bridge 仍然 fail-open，不因 Core 或 reporter 故障阻断原有命令。

This plugin observes OneBot events and completed send APIs. Telemetry never
waits for Lily Core. Phase 2c optionally adds one short, fail-open claim request
before matcher dispatch; it is disabled by default.

Deployment is intentionally separate from development. Copy or symlink the
`lily_core_bridge` package into `/home/justin/lily/plugins/`, then add
`"plugins.lily_core_bridge"` to the explicit local-plugin list in
`/home/justin/lily/bot.py`.

Required NoneBot environment values:

```dotenv
LILY_CORE_URL=http://127.0.0.1:8765
LILY_CORE_TOKEN=replace-with-lily-instance-token
LILY_CORE_INSTANCE_ID=lily-command
LILY_CORE_BOT_ID=3643287298
LILY_CORE_CLAIM_ENABLED=false
LILY_CORE_CLAIM_TIMEOUT_SECONDS=10.0
LILY_CORE_CLAIM_ATTEMPTS=2
LILY_CORE_CLAIM_RETRY_BACKOFF_SECONDS=0.1
LILY_CORE_REPORT_TIMEOUT_SECONDS=10.0
LILY_CORE_REPORT_ATTEMPTS=3
LILY_CORE_REPORT_RETRY_BACKOFF_SECONDS=0.1
```

Raw OneBot payload capture is off by default. Set `LILY_CORE_INCLUDE_RAW=true`
only for a short diagnostic window; Core still redacts and size-limits it.

Bridge 0.5.0 also normalizes the action notices actually emitted by the local
OneBot/NapCat stack: `group_msg_emoji_like`, group/friend recall and poke.
Reaction rows preserve the observer-local target message ID, actor, emoji ID
and platform count as `observed_state`; they carry no positive/negative or
feedback interpretation. Recall keeps operator and message author separate.
Poke keeps actor, target and bounded display/action facts while recording the
omission of jump URLs, image URLs and internal QQ UIDs in capture evidence.
Missing actor/value/target fields become `partial` or `unavailable`; they are
never guessed. These notice reports use the same durable spool as messages.

When a canary claim returns an enforced deny, the bridge suppresses outgoing
`send_*` API calls for that event. It does not ignore the event, so
chatrecorder, wordcloud collection, and other observation matchers continue to
run. Any claim timeout or malformed response leaves sends unchanged. Claim and
background-ingestion deadlines are separate so a brief database delay does not
turn a committed event into a false telemetry drop. Claim evaluation and ACK,
as well as background reports, retry transient transport, 429, and 5xx failures
with the same idempotency key before incrementing their failure counters.
