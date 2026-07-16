# Security and data minimization

## Secret provisioning and maintenance

Secret rotation is a separate operator-authorized maintenance action. A Core,
bridge, schema, or documentation deployment must not silently replace working
OneBot/database/admin/ingest credentials.

1. At an approved maintenance window, rotate any OneBot access token still
   written in `/home/justin/lily/API_DOC.md` and remove the literal value from
   documentation. This repository does not read or print that value.
2. Generate three unrelated random values: admin, Lily ingest, and Nekro
   ingest. Never reuse an existing OneBot, model-provider, or database secret.
3. Keep `.env` outside version control and verify it with `git status` before
   every commit.
4. Keep Core's published port on host loopback. Nekro reaches it through the
   private `superlily_bus` Docker network.

## Stored data

- Raw protocol payloads are disabled by default.
- If temporarily enabled, sensitive keys are recursively redacted, URL/URI
  userinfo, queries, and fragments are removed for every scheme, including
  custom-scheme scalar values and fields ending in `href`, `src`, `file`, or
  `platform_id`,
  strings/collections are bounded, and oversize objects are discarded without
  a preview.
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
- A committed deny is not proof that the remote bridge received it. A bridge
  may acknowledge an enforced deny only after installing authoritative
  suppression; an exclusive allow requires prior acknowledgement from every
  observed peer. Missing acknowledgement forces abstention. Lily currently
  meets this with an event-scoped send guard. Nekro deliberately withholds ACK
  because its public `BLOCK_TRIGGER` is not confirmed until after aggregate
  plugin signal handling.
- The current canary never enforces `observe_only`, so an unknown passive
  matcher cannot cause a message to disappear.
- Lily suppresses send APIs but still runs chat recording and other observers.
  Nekro uses its public history-preserving `BLOCK_TRIGGER` signal; an outbound
  guard or upstream post-aggregation hook remains a Phase 2 exit requirement.
- Claim attempts and suppressed/failed responses remain auditable.
- Platform send timeouts are ambiguous completion, not confirmed non-delivery.
  They are never retried automatically without a platform idempotency/recovery
  contract.

## Phase 3 and control-plane boundary

- Git-reviewed descriptor bundles are the authority source. Runtime inventory
  and provider health cannot grant permissions or activate tools.
- Provider credentials are separate from bot-ingest and administrator
  credentials. A provider can report/lease only its reviewed identity and
  never receives a bot token or Core administrator token.
- Tool execution defaults to `off`. `ledger_only`, exact canary, and enforced
  modes are distinct; global stop, per-tool suspension, and provider quarantine
  are independent controls.
- Filesystem, process, network, secret, sandbox, artifact, and remote-fetch
  permissions are machine-readable descriptor/provider policy. A caller cannot
  escalate them through tool arguments.
- The future control panel uses server-side short sessions, CSRF protection,
  reauthentication for dangerous changes, optimistic version checks,
  idempotency keys, and append-only audit. Bearer tokens are never stored in
  browser local storage. See `CONTROL_PLANE.md`.

Recommended starting retention:

- redacted raw payloads: 7 days;
- event/response text: 30 days while validating the system;
- aggregate status and transition records: 180 days.

Automated retention deletion is intentionally not enabled until the operator
confirms these periods.
