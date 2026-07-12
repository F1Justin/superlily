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
- Phase 2a.1 native identity capture is an explicit scalar allowlist. It never
  stores raw message bodies, sender objects, attachment locations, access
  tokens, cookies, or arbitrary adapter extension dictionaries.
- Event text is personal chat data even when it contains no credentials. Set a
  retention policy before enabling broad group ingestion.
- Runtime plugin snapshots contain plugin IDs, module names, matcher types,
  deterministic triggers, priority, and block flags. They do not include
  handler source, plugin configuration, secrets, or arbitrary matcher state.
- Runtime discovery never grants authority. Target instance, permission, and
  sensitive status require a reviewed static rule. Uncovered runtime commands
  make claim evaluation abstain.
- Composite/custom matcher rules and matcher-level permissions are reported as
  incomplete. Sensitive or non-public commands remain shadow-only until Core
  has a sender authorization model.

## Claim safety

- Claim mode defaults to `off`; bridge-side claim requests also default off.
- Canary scope is an exact allowlist of canonical conversation keys. There is
  no wildcard interpretation.
- Core errors and timeouts are fail-open. A deny is enforced only when every
  readiness gate passes.
- The current canary never enforces `observe_only`, so an unknown passive
  matcher cannot cause a message to disappear.
- Lily suppresses send APIs but still runs chat recording and other observers.
  Nekro uses its public history-preserving `BLOCK_TRIGGER` signal.
- Claim attempts and suppressed/failed responses remain auditable.

Recommended starting retention:

- redacted raw payloads: 7 days;
- event/response text: 30 days while validating the system;
- aggregate status and transition records: 180 days.

Automated retention deletion is intentionally not enabled until the operator
confirms these periods.
