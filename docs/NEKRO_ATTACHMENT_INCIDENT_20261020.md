# Nekro OneBot attachment incident — 2026-10-20

All times below are UTC+8.

At 20:57:34, a user uploaded a 14,576-byte `README.md` describing ios-topaz in
`onebot_v11-group_861651713`. NapCat saved the download as `README (1).md`.
The upload-notice converter ignored the returned download path and reconstructed
`NapCat/temp/README.md` from its display name. That path held a 3,718-byte game
README previously uploaded in `onebot_v11-group_1049695095` on September 21.
The converter copied the old bytes into the current conversation's upload directory.

The normal message converter downloaded the correct bytes independently. Thus the
same upload produced two contradictory attachment records: 1839503 (correct) and
1839504 (incorrect). Execution 84844 read the incorrectly registered README using
Nekro's built-in `view_str_content` agent method. Execution 84845 rendered its
summary; 84846 answered a correction without rereading the attachment.

## Final behavior

- Both file ingress paths use the same attachment validation and storage helper.
- Local downloads use the actual API-returned path, translated between the NapCat
  container's QQ directory and its host mount. Bare display names and paths outside
  that directory are rejected; there is no basename lookup fallback.
- Event/API/actual sizes must agree where provided. Local reads and streamed HTTP
  downloads enforce the configured maximum size.
- Storage names contain a SHA-256 digest of the bytes and a validated extension.
  Original names remain in the segment's display text. Complete files are published
  atomically, without replacing existing attachments, and are readable by sandbox
  UID 65534 through the existing conversation-only read-only mount.
- Upload notices allow one second for the ordinary message to arrive, then suppress
  a notice if a matching attachment from that sender has already been recorded in
  the same conversation within ten seconds. Extremely delayed/reordered events can
  still produce two equivalent records; both resolve to identical validated bytes.
  This change does not claim exactly-once delivery across the two OneBot event types.
- Nekro's native agent loop, model routing, and Superlily AgentRun settings are unchanged.

## Verification

Runtime commit: `503cf53d47adb61b9d9f1b34c247eb61e4c593b0`.

`poe lint` passed, including explicit checks of the modified adapter files (the
repository otherwise excludes adapters from the default lint target).
Targeted attachment, QQ voice, runtime, and sandbox tests: **117 passed, 1 skipped**.
The attachment tests cover the incident's renamed download, same-size stale files,
file-ID API lookup, both ingress paths, duplicate notice suppression, size limits,
invalid paths, symlink escape, concurrent storage, and same-name replacement.

The built image imported the patched modules successfully. An isolated container
with the real NapCat directory mounted read-only replayed the incident, selected
the correct file, and successfully read the resulting attachment as UID 65534.
No QQ test messages were sent.

| Content | Size | SHA-256 |
| --- | ---: | --- |
| Correct ios-topaz README | 14,576 | `86a502849c08fd3d8f1824289aabfc8ffc2accb4517b4aa1422c4edc329089e8` |
| Incorrect historical game README | 3,718 | `1b07e62b69efdd8d5523d2e0ca55b8b3a5fbe19681106785f6301816101fceb7` |

## Deployment and correction

Only `nekro_agent` was recreated at 21:37. NapCat and PostgreSQL stayed running.
The new container became healthy, `/api/health` returned `{"ok":true}`, and OneBot
reconnected at 21:37:43; subsequent ordinary group messages were ingested normally.
No post-deployment attachment upload or model reply was induced for testing.

Image: `superlily/nekro-agent:2.3.3-superlily.15-attachment-identity.1`.
Image ID: `sha256:84d63e6771c7b4fa4559d06c6d40c0f449fc11f6df4b81456c940d4ef4706b1c`.

Restricted backup directory:
`/home/justin/nekro/backups/attachment-incident-20261020`.
It contains the original affected database rows, execution records, both file
contents, deployment configuration, file hashes, and correction records.

Both attachment rows now reference the correct content-addressed ios-topaz file.
System tool-result row 1839511 contains an explicit incident correction instead of
presenting the leaked game README as this upload's result. The wrong current-group
`README.md` was quarantined outside the upload mount. Original execution records
and already-sent QQ replies were retained. Other groups' files and the NapCat
source cache were not removed. A complete historical impact audit is not included.

## Rollback

The previous image remains available as
`superlily/nekro-agent:2.3.3-superlily.14-argon-cache.1`
(`sha256:b435f8304da133704922a3f011eb06fe15c794073787d0c46c0a8a60ebcefa08`).
To roll back the service, restore `nekro-compose.override.yml` and
`nekro-runtime.lock.yml` from the restricted backup into this repository's `deploy/`
directory, then run the same compose invocation with `up -d --no-deps --no-build
nekro_agent`. The database corrections and quarantine can remain in place with
the old image; no schema migration was performed. The pre-correction row snapshots
are retained for a separately targeted data rollback if required.
