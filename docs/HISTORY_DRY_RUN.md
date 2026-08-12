# Historical data dry-run report

This report records read-only production measurements. No historical row was
copied, changed, or deleted.

## Import boundary

The boundary is the first observation received by Core for each instance:

- Lily: `2026-06-19 11:45:17.17105+00`;
- Nekro: `2026-06-19 11:49:44.696404+00`.

Rows at or after the corresponding boundary overlap Core ingestion and must
never be imported.

Nekro's source column has integer-second precision, so its executable source
predicate is the conservative `send_timestamp < 1781869784`. The one row in
second `1781869784` is the first Nekro message already observed by Core and is
part of the overlap set.

## Lily chatrecorder

The read-only snapshot at 2026-07-12 11:40 CST covered
`nonebot_plugin_chatrecorder_messagerecord_v2`:

- 8,519,226 rows total, from 2024-08-28 through 2026-07-12;
- 8,262,010 rows before the Lily boundary and 257,216 overlapping/newer rows;
- 7,878,503 pre-Core inbound OneBot messages with a non-empty local message ID;
- 383,507 pre-Core outbound `message_sent` rows with a non-empty local message ID;
- 5,891,333 pre-Core inbound rows with text and 4,876 with a reply segment;
- 88 scenes and 12,299 senders in the pre-Core inbound set;
- 23 duplicated `(bot, session, message_id)` keys, 46 rows total, with maximum
  multiplicity two.

The table contains the current Lily account `3643287298` and the historical
account `985393579`. Source-specific IDs must therefore include the recorder
primary key and bot identity; a local message ID alone is not an import key.

## Nekro history

The read-only snapshot at 2026-07-11 18:20 CST contained 1,142,966 rows from
2025-09-11 through 2026-07-11:

- 1,035,247 rows precede Nekro's Core boundary and 107,719 overlap it;
- 947,371 conservative pre-Core OneBot candidates have a non-empty local
  message ID;
- 115,055 carry reply hints and 740,822 carry text;
- 50 `(adapter, chat, message_id)` keys occur twice.

## Decision

Neither historical schema contains a verified cross-account `real_seq`.
Consequently there is no safe way to infer canonical Lily/Nekro equality from
text and time, and the millions of old rows are not copied into Core as part of
Phase 2. If a later `history.search` tool needs unified access, it should query
the source databases or use a staged, source-specific import that excludes the
overlap window and never fabricates cross-account identity.

The retained Core reference links are handled separately by the deterministic
resolver. It may update only targets that are unique under instance, platform,
canonical conversation, local message ID, and causal time constraints; all
other links remain unresolved.
