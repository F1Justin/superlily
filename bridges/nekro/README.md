# Nekro bridge

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
