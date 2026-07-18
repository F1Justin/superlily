# ADR 0004: Artifact lifecycle and storage

- Status: accepted
- Date: 2026-07-18

## Context

Wolfram image results and LaTeX rendering cannot safely return provider-local
paths or arbitrary URLs. Artifact completion must remain bound to the current
provider attempt and independently verified by Core.

## Decision

- Artifact state is `reserved -> uploading -> finalized`, or
  `expired/rejected`; only finalized artifacts may appear in successful output.
- A short-lived, one-use upload secret is bound to invocation, attempt,
  provider, current fence, MIME allowlist, count/byte/dimension limits,
  classification, scope, and expiry.
- Core streams uploads into quarantine, independently counts bytes, calculates
  the digest, inspects MIME/dimensions where applicable, and atomically moves a
  verified object to content-addressed storage.
- Late, failed, mismatched, or stale-fence uploads cannot change invocation
  success. Cleanup is idempotent and never deletes a referenced finalized
  object.
- Artifact retention and audit retention are separate. Provider paths, bearer
  secrets, and arbitrary remote URLs are never artifact identities.

## Consequences

Migration `0015_tool_confirmations_artifacts` precedes image-producing Wolfram
and `latex.render`. Storage implementation may change in Phase 4 without
changing content identity or invocation references.

## Required evidence

- Reservation replay, expiry, quota, MIME/hash/size/dimension, stale-fence,
  finalize, orphan, and cleanup tests.
- A failure-injection test proving no partially uploaded object becomes visible.
