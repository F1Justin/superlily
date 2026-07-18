# Control plane and operator panel design

## Status and boundary

This is a design contract, not a deployed panel. Phase 3a authority contracts
have started, but there is no panel and no control-plane mutation surface. A
future panel must expose existing Core truth before it is allowed to mutate
anything; it is not an alternate authority store and must never infer
“healthy” or “eligible” from one green status.

The panel always separates:

- **desired:** reviewed Git descriptors, conversation topology, rollout mode,
  policies and explicit operator controls;
- **reported:** authenticated bot/provider inventory and heartbeat claims;
- **effective:** Core's independently evaluated state and stable reason codes;
- **actual:** events, claims, acknowledgements, responses, invocations,
  attempts, artifacts and incidents.

Drift between these layers is first-class data. Runtime discovery, a display
name, or a UI toggle cannot grant authority absent a reviewed contract.

## Initial pages

1. **Overview:** Core/database readiness, bridge/provider freshness, queues,
   reporter counters, rollout modes, stops, current canaries and unexplained
   exceptions. One component's heartbeat never masks another path's outage.
2. **Conversations:** canonical key, desired/effective
   `command_only`/`conversation_only`/`full`/`observe_only`, bot membership,
   Nekro channel activation, claim scope, drift and last verified time.
3. **Routing timeline:** source, observations, references, policy revision,
   claims/deny acknowledgements, suppression, attributed responses and outcome;
   raw identifiers are redacted/scoped.
4. **Tools:** Git descriptor commit/hash, immutable DB copy/lifecycle, provider
   inventory, eligibility reasons, rollout mode and stops. Descriptor content is
   read-only in Phase 3.
5. **Invocations:** principal/input hash, policy snapshot, transitions,
   attempts/fences, budgets, output validation, artifacts and recovery class.
6. **Providers:** stable authenticated identity, credential age, accepted
   inventory, separate heartbeat/load, implementation drift, quarantine and
   lease history. Bot-ingest identity is not provider identity.
7. **HA coverage:** per-adapter epoch/sequence watermarks, durable spool depth,
   oldest unacknowledged age, account/host/path coverage, gaps and failover
   leases. This page does not itself deploy the third account.
8. **Security/Audit:** operator sessions/roles, changes, confirmations,
   break-glass use, secret/custom-URI/raw-retention audits and exported evidence
   bundles.

Every list has bounded time/scope filters and pagination. Detail views link by
opaque IDs; they do not put message text, tokens, inputs, or artifact secrets in
URLs. Exports are explicit, redacted, size-limited and audited.

## Roles and mutations

| Role | Read | Mutate after its gate |
|---|---|---|
| `auditor` | all redacted operational/audit views and evidence export | none |
| `operator` | health, routing, providers, invocations | pause/resume reviewed rollout scope, cancel safe work, acknowledge incidents |
| `reviewer` | descriptors, policy diffs and canary previews | approve imported lifecycle/policy transitions; cannot manage credentials |
| `security_admin` | security/audit and credential metadata | rotate/revoke credentials, quarantine providers, manage role assignments |
| `break_glass` | incident-scoped necessary views | time-limited emergency global stop/revocation with mandatory reason and follow-up |

No single role both authors descriptor content and silently activates it.
Dangerous mutations show a server-computed preview of desired/effective diff,
require a reason and fresh reauthentication, use an idempotency key and expected
resource version, and append before/after hashes plus outcome to immutable
audit. Stale compare-and-swap fails closed. Rollback is a new audited mutation,
not history deletion.

## Web security

- Use short-lived server-side sessions in `Secure`, `HttpOnly`, `SameSite`
  cookies; never keep admin/provider/bot bearer tokens in browser local storage.
- Protect every mutation with CSRF validation, exact Origin/Host checks,
  content-type enforcement, rate limits and authorization at the API, not only
  hidden buttons.
- Reauthenticate for credential, role, global-stop, provider-quarantine,
  descriptor-lifecycle and canary-expansion changes. `break_glass` expires
  automatically and alerts other administrators.
- Apply CSP, no third-party script by default, output escaping, safe downloads,
  bounded queries, redaction and cache-control. Tool output, message text,
  provider errors and artifact metadata are untrusted content.
- Audit login/session events, reads of sensitive detail, exports, previews,
  attempted/accepted/rejected mutations and resulting effective state. Audit
  storage is append-only and retention is separate from chat/artifact retention.

## Rollout

1. CLI/API evidence remains authoritative while the page/query contracts are
   tested.
2. Ship read-only Overview, Conversations, Routing, Tools and Providers.
3. Add Invocations/artifacts only after their ledgers exist; add HA Coverage
   only after HA-0 envelope/spool contracts exist.
4. Add authenticated auditor sessions and compare every panel count/reason with
   direct API/SQL evidence.
5. Enable one low-risk operator mutation at a time only after role, preview,
   CSRF, reauth, CAS, idempotency, audit and rollback fault tests pass.
6. Keep descriptor editing and secret values outside the panel during Phase 3.

Panel availability is never a runtime dependency for event ingestion, claims,
tool leases, emergency CLI stop, or bot fail-open behavior.
