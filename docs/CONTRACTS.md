# Ingestion contracts

The wire schema is version `1.0` and the HTTP surface is under `/v1`.

## Write APIs

- `POST /v1/events` requires a bearer token and `Idempotency-Key`.
- `POST /v1/responses` requires a bearer token and `Idempotency-Key`.
- `POST /v1/heartbeats` requires a bearer token.
- `POST /v1/command-registry/snapshots` requires the Lily instance token. Core
  recomputes the canonical SHA-256 over plugins and candidates before storing
  or refreshing the snapshot.
- `POST /v1/claims/evaluate` requires an instance token and
  `Idempotency-Key`. It ingests/reuses the event and returns `allow`, `deny`, or
  `abstain`, plus `ready`, `enforced`, `claim_id`, and `acknowledged_at`.
- `POST /v1/claims/{claim_id}/ack` requires the same instance token and an
  `Idempotency-Key`. Only the instance that received an enforced deny can
  acknowledge that suppression has been installed; replay is idempotent.

Each token is bound to exactly one `instance.instance_id`. A Lily token cannot
submit a Nekro payload.

Heartbeats may carry a typed `capabilities` snapshot. A snapshot names a
versioned adapter profile, an explicit list of supported operations, and
optional numeric limits. Missing capabilities mean unknown, never “supports
everything”. The initial `onebot_v11.qq.v1` profile conservatively declares
only `send_text`, `send_image`, `reply`, and `mention`; later Tool/Renderer
routing must degrade against this snapshot rather than infer platform support
from the adapter name.

For event ingestion, the request's `source_event_id` is the reporting
account's event identifier. Core stores it as `reported_source_event_id` and
returns the canonical `source_event_id` in the response. Different bot
accounts may therefore submit different request IDs and receive the same
canonical ID. The account-local message ID is retained separately as
`platform_message_id`.

For QQ messages, both bridges derive a content-free
`qq:source:v2:<sha256>` reported ID from canonical conversation, account-local
message ID, and the allowlisted native identity. When strong native identity is
missing, sender and occurred time enter the fallback material; message text,
URLs, attachment data, display names, and secrets never do. Core independently
computes the correlation fingerprint. Replaying exact identity returns the
existing observation, while reusing a reported ID or idempotency key with a
different native identity returns HTTP 409.

Events may include `references`. Phase 2a records these as first-class
`event_links`. A reference can provide a canonical or reported
`source_event_id`, an account-local `platform_message_id`, and optional target
conversation/sender hints. Core currently extracts QQ `reply` segments as
`reply_to` references. Resolved links point to a canonical `source_event_id`;
unresolved or ambiguous links are retained for later backfill instead of being
dropped.

Phase 2b creates one canonical `event_decision` per canonical source event. A
decision has a policy version, decision type, optional target instance,
confidence, reason, feature snapshot, revision, and update time. It is
recomputed from all observations rather than duplicated per observing account.

Core does not blindly trust bridge `to_me`/`is_tome` flags for talk routing.
Those adapter/framework signals are retained as `bridge_to_me` in decision
features. The Core-level `to_me` feature means the text matched Superlily's
explicit summon policy, currently the Chinese substring `莉莉`. English `Lily`
alone is ordinary text unless another rule, such as an `@` mention or a reply to
a bot response, applies.

Command decisions are driven by the configured command registry, not by a small
hard-coded prefix list. The default registry lives at
`apps/core/config/command_registry.toml` and can be overridden with
`SUPERLILY_COMMAND_REGISTRY_PATH`. Decision features include the registry
version and the matched command rule, when any. If the registry cannot be read,
event ingestion stays fail-open and records the registry error in the decision
features.

Command matching is also gated by `features.command_eligible`. Core preserves
concatenated text for search but independently inspects the deciding OneBot
segments: it may skip a leading reply and the observing bot's own ToMe `at`,
but an `at` to another account, image, attachment, or other non-text leading
segment makes later text ineligible as a NoneBot command.

Resolved reply ownership takes precedence over command and summon text. A
reply to a Nekro response routes to Nekro in a `full` or `conversation_only`
group even when its body is also a Lily command; a reply to a Lily response is
observation-only even when QQ retained or removed its automatic `at` segment.
Ambiguous or conflicting reply targets remain observation-only. An unresolved
or reply-to-other message can route to Nekro only when it explicitly contains
the Lily summon or a known-bot mention and talk is enabled; command text alone
cannot do so. The effective per-group mode is stored in decision features.

Private QQ routing is recipient-bound. A public Lily command received by Lily
may target Lily. Ordinary Lily-private text remains observation-only; ordinary
Nekro-private text may target Nekro. A private event is never transferred to an
account that did not receive it.

`command_only` groups recognize the same reviewed Lily command registry as
`full` groups, including the directory-derived `nonebot-plugin-random`
commands. Those random matchers use NoneBot's longest command prefix: for
example, `随机学养评价` selects the `随机学养` command and trailing text is the
plugin argument. Conversational summons, bot mentions, and replies to Nekro
are observation-only in this mode. `conversation_only` groups do the inverse:
commands remain observation-only because Lily is unavailable, while summons,
mentions, and replies to Nekro route to Nekro. `observe_only` groups never
produce an actionable decision.

Cross-account correlation v3 is conservative and applies only to QQ group
message events with a sender and native `real_seq`. Private chats remain
uncorrelated until separately validated. The key uses normalized conversation,
sender, and `real_seq`; text and account-local IDs never participate. Native
time is a conflict guard. When both observations carry the same native time,
adapter-normalized `occurred_at` skew does not split the strong fingerprint;
the configured short window is only a fallback when native time is absent.
Ambiguous, missing, or conflicting identity stays separate rather than risk a
false merge.

QQ bridges may include `metadata.native_identity` using schema
`onebot_v11.qq.native_identity.v1`. This is a strict scalar allowlist for
identity diagnostics, currently covering `message_id`, `message_seq`,
`real_id`, `real_seq`, `time`, `group_id`, `user_id`, message/sub types, and a
small set of optional NapCat native ID aliases. The verified `real_seq`
combination is the v3 key; other account-local fields remain diagnostic only.

Runtime command candidates distinguish NoneBot commands, token-boundary
commands, startswith/fullmatch/regex/keyword/endswith rules, and Alconna main
commands/shortcuts where runtime objects expose them. A runtime candidate is
only inventory. Static rules retain authority over target, permission, and
sensitive status. Candidate completeness also records whether the matcher has
extra rule or permission checkers that cannot be represented by the trigger.

Claim enforcement is narrower than decision generation. Only `command` and
`talk` may produce an `allow`. Ordinary `observe_only` decisions abstain. The
single suppression-only exception is a strongly correlated, deterministic
`reply_to_other_observed` decision with no explicit Lily summon or known-bot
mention: Core returns `deny` to every requesting instance and never elects an
owner. This prevents command-shaped text in a reply to another person from
escaping through a local matcher. It still requires correlation v3, at least
one observation, the claim confidence threshold, and exact canary/enforce
scope; it does not require the multi-observer ownership quorum, a
command-registry match, or an online target because no execution target exists.
This lets a Lily-only command group suppress its own local matcher. Missing
identity, an uncertain reply target, or low confidence remains fail-open.
Incomplete matcher introspection, sensitive commands, and commands whose
permission is not `public` also abstain. `shadow` never enforces; `canary`
enforces only exact configured `platform:type:id` conversations.
An `allow` is enforced only after all other instances observed on that source
have acknowledged their enforced `deny` claims. A committed deny without an
acknowledgement is insufficient because its HTTP response may have been lost
while the peer failed open. Otherwise the target becomes
`abstain / claim_peer_suppressions_not_acknowledged`; the coordination snapshot
is retained in claim features. Actual response outcomes remain the final
behavioral evidence. Claim gates always include the canonical `decision_type`,
`decision_reason`, `target_instance_id`, and optional `suppression_scope`. A
bridge may use the decision and target fields to correlate a
legacy response after fail-open abstention, but they do not grant execution
authority; only `ready`, `action`, and `enforced` control suppression or
ownership.

Linked responses record `metadata.trigger_attribution`: Lily uses its native
`event_context`; Nekro binds `task_context` to the current per-chat scheduler
task and keeps a later pending source separate. `completion_status` is
`succeeded`, `failed`, `suppressed`, or `ambiguous`. A send timeout is
`success=false / ambiguous`; it is not confirmation that no platform message
was emitted and must not be retried blindly. Until Phase 4 adds an explicit
multipart delivery group, the audit accepts only one bounded complementary
pair for a trigger/instance: exactly one attachment-only send and one text-only
send, distinct platform message IDs, no more than five seconds apart. Any other
repeated successful send remains exceptional.

## Read APIs

- `GET /health/live` only proves the process is serving HTTP.
- `GET /health/ready` also checks PostgreSQL.
- `GET /v1/events/recent`, `/v1/responses/recent`,
  `/v1/event-links/recent`, `/v1/decisions/recent`,
  `/v1/decisions/summary`, `/v1/decisions/outcomes`,
  `/v1/claims/recent`, `/v1/claims/summary`,
  `/v1/native-identities/recent`, `/v1/native-identities/coverage`,
  `/v1/command-registry`, `/v1/command-registry/runtime`,
  `/v1/events/{source_event_id}/context`, and `/v1/instances` require the admin
  bearer token.
- `POST /v1/event-links/resolve` is also admin-only and retries unresolved or
  ambiguous references without guessing.

`/v1/decisions/summary` is the human-readable audit view. It joins the shadow
decision, source event, and deciding observation into compact rows such as
`time | group:id | sender | text | decision -> target | reason`.

`/v1/native-identities/recent` shows the same observations with compact
`message_id` and `real_seq` summaries plus the complete safe identity
allowlist, so cross-account samples can be compared without enabling raw
payload storage.

`/v1/native-identities/coverage` reports per-instance identity capture and
per-field coverage for canonical message events over a bounded one-to-168-hour
window; notices and other non-message events are excluded.

`/v1/instances` exposes the latest typed capability snapshot and derives
`offline` from the earlier of Core receipt time and the
bridge-reported time. A delayed queue or future-skewed bridge clock therefore
cannot keep an instance online. The reported timestamp remains in instance
metadata. Only reported status changes append history.

The authoritative Pydantic definitions are in
`packages/contracts/src/superlily_contracts/models.py`.
