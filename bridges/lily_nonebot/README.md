# Lily NoneBot bridge

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
LILY_CORE_CLAIM_TIMEOUT_SECONDS=3.0
LILY_CORE_REPORT_TIMEOUT_SECONDS=5.0
LILY_CORE_REPORT_ATTEMPTS=3
LILY_CORE_REPORT_RETRY_BACKOFF_SECONDS=0.1
```

Raw OneBot payload capture is off by default. Set `LILY_CORE_INCLUDE_RAW=true`
only for a short diagnostic window; Core still redacts and size-limits it.

When a canary claim returns an enforced deny, the bridge suppresses outgoing
`send_*` API calls for that event. It does not ignore the event, so
chatrecorder, wordcloud collection, and other observation matchers continue to
run. Any claim timeout or malformed response leaves sends unchanged. Claim and
background-ingestion deadlines are separate so a brief database delay does not
turn a committed event into a false telemetry drop. Background reports retry
transient transport, 429, and 5xx failures with the same idempotency key before
incrementing `dropped`.
