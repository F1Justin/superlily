# Nekro bridge

This is a Nekro 2.2.1 local plugin. It uses the public plugin callback for
normalized human messages and the public NoneBot event hook for confirmed
OneBot `message_sent` events. Failed send API calls are recorded separately.

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
- `CLAIM_TIMEOUT_SECONDS=3.0`
- `REPORT_TIMEOUT_SECONDS=5.0`
- `REPORT_ATTEMPTS=3`
- `REPORT_RETRY_BACKOFF_SECONDS=0.1`

The container must join the `superlily_bus` network; see
`deploy/nekro-compose.override.yml`.

Do not hot-reload this plugin during the first smoke test. Its global NoneBot
hooks are guarded against duplicate registration, but a clean process restart
is easier to audit.

Background reports retry transient transport, 429, and 5xx failures with the
same idempotency key before incrementing `dropped`.

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
