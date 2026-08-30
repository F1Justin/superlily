# Nekro traditional-path prompt optimization

Date: 2026-08-28 CST

R0 frozen production baseline: `v2.3.3-superlily.4` / `b56e465` / image
`superlily/nekro-agent:2.3.3-superlily.4`. The current 100-call production
baseline is frozen in [`R0_BASELINE.md`](R0_BASELINE.md); the table below is the
one-variable rollout evidence that led to it.

## Scope

This rollout keeps Nekro's traditional Python sandbox agent intact. Changes are
applied one variable at a time in this order:

1. explicit OpenRouter Gemini prompt caching and cache observability;
2. removal of the two built-in fallback practice conversations;
3. disabling automatic inclusion of historical images; and
4. reducing ordinary history while preserving the current and directly
   referenced messages.

The per-run random history token remains random and dynamic. Memory injection
stays disabled. External CoT was already disabled in the live model group before
this rollout.

## Baseline and results

| Stage | Evidence | Input tokens | Cached tokens | Provider cost | Status |
|---|---|---:|---:|---:|---|
| Original production | 2026-08-28 median, 24 runs | 8,846 median | unavailable | about $0.006 | baseline |
| Cache-only probe, write | isolated real stable prefix | 4,595 | 4,586 written | $0.000628 | passed |
| Cache-only probe, read | two repeated isolated requests | 4,595 | 4,586 read | $0.000246 | passed |
| Cache-only production | exec_code 70296 | 8,581 | 4,586 read | $0.002464 | passed |
| Cache-only production | exec_code 70297 | 8,774 | 4,586 read | $0.002527 | passed |
| No-default-few-shot probe, write | isolated real system prefix | 3,958 | 3,949 written | $0.000543 | passed |
| No-default-few-shot probe, read | two repeated isolated requests | 3,958 | 3,949 read | $0.000214 | passed |
| Historical images enabled | same saved real history, two images | 9,094 | 3,949 read | $0.002923 | passed |
| Historical images removed | same saved real history, text unchanged | 6,925 | 3,949 read | $0.001853 | passed |
| Full 5,115-character history | same saved saturated history | 7,677 | 3,949 read | $0.002235 | passed |
| History reduced to 3,200 characters | same saved saturated history | 6,244 | 3,949 read | $0.001504 | passed |
| No-default-few-shot production | exec_code 70299, historical images still enabled | 8,618 | 3,949 read | $0.003104 | passed |
| No-history-images production | pending natural request | pending | pending | pending | deployed |
| Reduced-history production | pending natural request | pending | pending | pending | deployed |
| Final combined replay | 10 recent real prompt logs, no QQ sends | 6,018 median | 3,949 each | $0.001376 average | 10/10 valid Python |

Provider cost is the `usage.cost` returned by OpenRouter, not a local estimate.
The production rows are stored in Nekro's existing `exec_code.extra_data` JSON.
The final replay used the production system prompt plus each saved real dynamic
history, removed historical image payloads, retained complete message
boundaries up to 3,200 characters, called the real provider, and compiled the
returned Python without executing it.

## Findings that changed the implementation

The running image is Nekro Agent 2.3.3. Its system message and default practice
messages were already a stable prefix. The random `one_time_code`, current time,
chat key, plugin runtime context, images, and chat history begin only in the
later dynamic history message. The cache miss was therefore not caused by the
nonce placement.

OpenRouter's Gemini path requires an explicit cache-control breakpoint. Nekro
also discarded cache details from the final usage payload. The cache-only patch
adds the breakpoint after the last stable message, an opaque hashed per-chat
session ID for sticky routing, and cache/cost fields in `extra_data`. It does not
make the nonce stable or expose the raw chat key as routing metadata.

## Rollback

Rollback must select a previously reviewed and pinned SuperLily Runtime tag,
never the moving `kromiose/nekro-agent:latest`. The current production identity
and immediate predecessor are recorded in `deploy/nekro-runtime.lock.yml` and
Git history; recreate only `nekro_agent` after verifying the selected source
commit and image identity. PostgreSQL, Qdrant, NapCat, Lily Core, data mounts,
and sandbox images do not need to be changed.
