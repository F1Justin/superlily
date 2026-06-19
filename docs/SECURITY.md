# Security and data minimization

## Before first deployment

1. Rotate the OneBot access token currently written in
   `/home/justin/lily/API_DOC.md`; remove the literal value from documentation.
2. Generate three unrelated random values: admin, Lily ingest, and Nekro
   ingest. Never reuse an existing OneBot, model-provider, or database secret.
3. Keep `.env` outside version control and verify it with `git status` before
   every commit.
4. Keep Core's published port on host loopback. Nekro reaches it through the
   private `superlily_bus` Docker network.

## Stored data

- Raw protocol payloads are disabled by default.
- If temporarily enabled, sensitive keys are recursively redacted, URL query
  strings are removed, strings/collections are bounded, and oversize objects
  are discarded without a preview.
- Attachment bytes and remote URLs are never copied in phase one; only metadata
  summaries are accepted.
- Event text is personal chat data even when it contains no credentials. Set a
  retention policy before enabling broad group ingestion.

Recommended starting retention:

- redacted raw payloads: 7 days;
- event/response text: 30 days while validating the system;
- aggregate status and transition records: 180 days.

Automated retention deletion is intentionally not enabled until the operator
confirms these periods.

