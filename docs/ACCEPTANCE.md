# Phase-one acceptance checklist

## Automated

- [x] Contract rejects timezone-naive events.
- [x] Secrets and URL queries are removed from optional diagnostic payloads.
- [x] Replaying an idempotency key does not create another observation.
- [x] Two bot accounts can observe one source event independently.
- [x] Two bot accounts can concurrently create observations of one source event.
- [x] Instance tokens cannot impersonate another instance.
- [x] Responses without a trigger event are accepted.
- [x] Heartbeats update liveness and admin endpoints remain protected.
- [x] A full bridge queue drops telemetry immediately instead of blocking.
- [x] Lily bridge imports under the installed Lily NoneBot runtime.
- [x] Nekro bridge imports under the pinned Nekro 2.2.1 image.
- [x] The production image builds, migrates a fresh PostgreSQL 17 database, and serves health APIs as a non-root user.
- [x] A real background reporter writes through Core into PostgreSQL.
- [x] With Core stopped, a report fails in the background without blocking or crashing the caller.

## Live smoke test before enabling broad ingestion

- [x] Confirm Lily is running in the `nb` tmux session under the enabled
  `tmux-nb.service` auto-restart supervisor.
- [x] Confirm the current Lily process is receiving live OneBot events before
  bridge installation.
- [ ] Record one known-good Lily command response before bridge installation.
- [ ] Confirm one Lily message and response appear in Core.
- [ ] Confirm one Nekro message and `message_sent` response appear in Core.
- [ ] Stop Core for two minutes and verify both bots continue responding.
- [ ] Restart Core and verify heartbeats recover without bot restarts.
- [ ] Confirm images store metadata only and no remote URL query strings.
- [ ] Confirm no access token or model API key appears in recent records.

The unchecked items intentionally require an operator-visible live deployment;
development tests do not mutate either running bot.
