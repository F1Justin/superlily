# Lily NoneBot bridge

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
