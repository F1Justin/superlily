# Phase 4: RenderDocument and delivery canary

## Goal

The Phase 4 canary replaces model-written Pillow/Matplotlib prose images with a
reviewed, deterministic path for mixed Chinese text, lists, and mathematics. The active
canary targets are the Nekro conversations `onebot_v11-group_1080353942` and
`onebot_v11-group_861651713`.

This slice is deliberately separate from Phase 5 natural-language tool selection.
`submit_render_document` is a terminal Nekro behavior, and Core authenticates the real
ingest instance and checks the exact conversation allowlist.

## Why not "Markdown rendering" directly?

Markdown is an input syntax, not a rasterizer. A Markdown solution still needs an HTML
engine, CSS fonts, a math engine such as KaTeX or MathJax, and a browser/screenshot
process. The initial backend reuses the existing no-network XeLaTeX worker because its
Chinese font set, mathematical glyphs, process limits, and PNG validation are already
reviewed.

Core speaks to an authenticated internal document-renderer gateway. The gateway alone
owns access to the private `render_document` worker socket. The public contract is
the `RenderDocument` AST, not TeX or unrestricted Markdown, so an HTML/KaTeX worker can
replace the initial backend without changing Nekro, the Core API, artifact storage, or
delivery receipts. RenderDocument 1.2 recognizes only paired `**strong**` markers inside
reviewed prose fields; block Markdown, HTML, links, images, and code fences remain escaped
text or explicit structural nodes.

Local warm-process measurements for a representative mixed CJK/math document were
1.16 s, 0.84 s, and 0.89 s. Core records `render_duration_ms` on every request. The
canary should use observed p50/p95 latency and failure rate before deciding whether the
extra Chromium/KaTeX runtime is justified.

## Flow

1. The canary prompt tells Nekro to call `submit_render_document(document_json)` for
   long prose/math images and forbids Pillow/ImageDraw/Matplotlib text layout.
2. The bridge overwrites `instance_id` and `conversation_key`; the model cannot choose
   either authority value.
3. Core validates the bounded AST, ingest token, idempotency key, render mode, and exact
   canary conversation.
4. Prose fields may mix ordinary text, paired `**strong**` spans, and single-dollar
   inline math, for example `**结论：** 已知 $f(x)=x^3+px^2+qx+r$`. The contract reviews
   each inline expression with the same restrictions as a standalone math block.
   Unmatched strong markers remain literal, code blocks never interpret them, and the
   text fallback removes only recognized presentation markers. The worker escapes the
   prose segments and renders one bounded PNG with no network or shell escape.
   Standalone math blocks remain for matrices and long display equations.
5. Core creates a fenced `RenderAttempt` with an exact renderer snapshot. Failed,
   abandoned, missing-object, and expired-artifact states can create a new attempt;
   a still-live lease cannot be executed twice.
6. Core independently validates and content-addresses the PNG, then creates an immutable
   `DeliveryPlan` from the instance's latest heartbeat capability snapshot. QQ currently
   selects image; an adapter with only `send_text` gets bounded plain text plus an explicit
   `image_unsupported_fallback_to_text` degradation.
7. Before sending, Nekro creates an idempotent delivery intent. Its NoneBot API-completion
   hook captures the OneBot `message_id`; Core then records a confirmed success, bounded
   failure, or ambiguous completion. A pending/ambiguous intent is never retried blindly.

## Safe defaults and canary configuration

Core defaults to `SUPERLILY_RENDER_MODE=off`. A deployment-ready canary uses:

```dotenv
SUPERLILY_ARTIFACT_ROOT=/var/lib/superlily/artifacts
SUPERLILY_ARTIFACT_SECRET_PEPPER=<independent-random-secret-at-least-32-chars>
SUPERLILY_RENDER_MODE=canary
SUPERLILY_RENDER_CANARY_CONVERSATIONS_JSON=["onebot_v11-group_1080353942","onebot_v11-group_861651713"]
SUPERLILY_RENDER_BACKEND_URL=http://document-renderer:8000
SUPERLILY_RENDER_BACKEND_TOKEN_FILE=/run/secrets/render_backend_token
SUPERLILY_RENDER_IMPLEMENTATION_HASH=<reviewed-worker-identity-sha256>
SUPERLILY_RENDER_DELIVERY_INTENT_SECONDS=60
```

The Nekro bridge remains off independently. Its plugin configuration must set:

```text
RENDER_ENABLED = true
RENDER_CANARY_CHAT_KEYS = onebot_v11-group_1080353942,onebot_v11-group_861651713
```

Do not enable only one side, broaden the allowlist, restart live services, or send a
real canary message as part of a repository-only rollout. Those are separate operational
changes requiring an explicit deployment decision.

## Exit checks for this slice

- Contract 1.2 provides stable node IDs and bounded text, heading, math, list, quote,
  code, table, notice, progress, group, alternative, image, and artifact-reference nodes.
- Contract 1.2 adds only paired `**strong**` inline presentation, preserves 1.0/1.1
  literal-marker semantics, and keeps image and plain-text delivery semantically aligned.
- Contract rejects unknown fields, duplicate node IDs, inaccessible artifact references,
  oversized documents, invalid chat keys, and TeX file I/O/control commands.
- Core stores immutable request identity, fenced attempts, renderer snapshots,
  content-addressed artifacts, capability plans, delivery intents, and append-only evidence.
- Artifact download is restricted to the submitting ingest instance and expires.
- Exact retries reuse a live artifact; an expired artifact or terminal failure re-renders
  under a new attempt without changing document identity.
- QQ success is confirmed only when OneBot returns a platform message ID. Unknown
  completion remains ambiguous and at-most-once.
- The golden mixed Chinese/math sample contains `≅`, `⊗`, `⊕`, and `∩` without missing
  glyph boxes and wraps within a fixed document width.

## Remaining Phase 4 work after this canary

The generic compatibility migration and exit proof are still separate work packets:

1. move status, Wolfram, LaTeX command output, and help paths one by one to
   structured result -> RenderDocument -> DeliveryPlan, retaining rollback paths;
2. add a deterministic constrained-adapter simulator and run the same fixtures through
   QQ-image and text-only profiles;
3. complete malicious-input, crash/fence, deletion, and PostgreSQL fault matrices, then
   hold a measured exact-conversation stable window before widening any canary.

## 2026-07-22 deployment state

- Production is at migration `0018_render_attempt_delivery`; the 80 legacy render
  documents were backfilled to 80 attempts and all 76 artifact rows are attempt-bound.
- Core, the document gateway, the provider, and the no-network worker use reviewed
  worker identity `cd49f0e444eb6d1f973340c1ae597f5955051add347a191e03ecdf922f679fe2`.
- Nekro bridge `0.8.0` is online and allows exactly
  `onebot_v11-group_1080353942` and `onebot_v11-group_861651713`.
- A production RenderDocument 1.1 probe completed in 1087 ms and selected an image plan
  without degradation. The deploy probe intentionally did not send a group message;
  the first natural canary delivery still needs confirmation that the OneBot completion
  hook records its real platform message ID.
- After adding `onebot_v11-group_861651713`, a scoped production probe completed in
  1009 ms and likewise selected an image plan without degradation. It created no
  delivery intent and therefore sent no test message to the group.
- The RenderDocument 1.2 Markdown-lite production probe completed in 1116 ms with no
  degradation or delivery intent. Visual inspection confirmed paired strong markers,
  strong text containing inline math, literal unmatched markers, and literal code-block
  markers all follow their reviewed semantics.
- `image` and `artifact_ref` nodes currently render bounded accessibility placeholders.
  Resolver-backed composition of existing artifacts remains part of the compatibility
  migration rather than granting the worker Core credentials or filesystem authority.
