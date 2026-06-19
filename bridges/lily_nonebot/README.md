# Lily NoneBot bridge

This plugin observes OneBot events and completed send APIs. It never changes a
matcher result and never waits for Lily Core in the event-processing path.

Deployment is intentionally separate from development. Copy or symlink the
`lily_core_bridge` package into `/home/justin/lily/plugins/`, then add
`"plugins.lily_core_bridge"` to the explicit local-plugin list in
`/home/justin/lily/bot.py`.

Required NoneBot environment values:

```dotenv
LILY_CORE_URL=http://127.0.0.1:8765
LILY_CORE_TOKEN=replace-with-lily-instance-token
LILY_CORE_INSTANCE_ID=lily-command
LILY_CORE_BOT_ID=985393579
```

Raw OneBot payload capture is off by default. Set `LILY_CORE_INCLUDE_RAW=true`
only for a short diagnostic window; Core still redacts and size-limits it.

