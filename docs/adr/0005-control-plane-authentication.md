# ADR 0005: Control-plane authentication and authority

- Status: accepted
- Date: 2026-07-18

## Context

The operator panel must expose Core truth without becoming a second authority
store or leaking long-lived administrator/provider/bot credentials into a
browser.

## Decision

- Phase 3 descriptor content is Git-authored and read-only in the panel.
- The panel always separates desired, reported, effective, and actual state.
- Browser access uses short-lived server-side sessions in Secure, HttpOnly,
  SameSite cookies. Bearer credentials are never stored in browser storage,
  URLs, exported evidence, tool input, or artifacts.
- Mutations require API authorization, CSRF and exact Origin/Host checks,
  content-type enforcement, rate limits, a reason, idempotency key, expected
  resource version, fresh reauthentication for dangerous actions, and
  append-only before/after audit hashes.
- Auditor, operator, reviewer, security-admin, and expiring break-glass roles
  remain distinct. No role silently authors and activates descriptor content.
- The panel is never a dependency for ingestion, claims, leases, or emergency
  CLI stop.

## Consequences

Phase 3 may add read-only registry views before mutation sessions exist. Any
activation, suspension, quarantine, credential, or canary mutation remains
disabled until its role/session/audit/rollback tests pass.

## Required evidence

- Direct API/SQL parity tests for every displayed count and reason.
- Session expiry, CSRF/Origin, role, reauthentication, CAS, idempotency,
  redaction, CSP, export, and append-only audit tests before mutation rollout.
