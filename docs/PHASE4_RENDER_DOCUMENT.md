# Phase 4: RenderDocument canary

## Goal

The first Phase 4 slice replaces model-written Pillow/Matplotlib prose images with a
reviewed, deterministic path for mixed Chinese text, lists, and mathematics. The first
canary target is the Nekro conversation `onebot_v11-group_1080353942`.

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
the `RenderDocument` AST, not TeX and not Markdown, so an HTML/KaTeX worker can replace
the initial backend without changing Nekro, the Core API, artifact storage, or delivery
receipts.

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
4. Prose fields may mix ordinary text with single-dollar inline math, for example
   `已知 $f(x)=x^3+px^2+qx+r$`; the contract reviews each inline expression with the
   same restrictions as a standalone math block. The worker escapes only the prose
   segments and renders one bounded PNG with no network or shell escape. Standalone
   math blocks remain for matrices and long display equations.
5. Core independently validates and content-addresses the PNG, then returns a scoped,
   expiring artifact path.
6. Nekro verifies the SHA-256, forwards the bytes into its sandbox, and sends the image.
7. The bridge appends a delivery attempt. Current Nekro `send_image` does not expose the
   platform message ID, so successful calls are conservatively recorded as `ambiguous`
   with `platform_message_id_unavailable` rather than claiming confirmed delivery.

## Safe defaults and canary configuration

Core defaults to `SUPERLILY_RENDER_MODE=off`. A deployment-ready canary uses:

```dotenv
SUPERLILY_ARTIFACT_ROOT=/var/lib/superlily/artifacts
SUPERLILY_ARTIFACT_SECRET_PEPPER=<independent-random-secret-at-least-32-chars>
SUPERLILY_RENDER_MODE=canary
SUPERLILY_RENDER_CANARY_CONVERSATIONS_JSON=["onebot_v11-group_1080353942"]
SUPERLILY_RENDER_BACKEND_URL=http://document-renderer:8000
SUPERLILY_RENDER_BACKEND_TOKEN_FILE=/run/secrets/render_backend_token
```

The Nekro bridge remains off independently. Its plugin configuration must set:

```text
RENDER_ENABLED = true
RENDER_CANARY_CHAT_KEYS = onebot_v11-group_1080353942
```

Do not enable only one side, broaden the allowlist, restart live services, or send a
real canary message as part of a repository-only rollout. Those are separate operational
changes requiring an explicit deployment decision.

## Exit checks for this slice

- Contract rejects unknown fields, oversized documents, invalid chat keys, and TeX file
  I/O/control commands.
- Core stores immutable request identity, terminal status, content-addressed artifact,
  render duration, and append-only delivery evidence.
- Artifact download is restricted to the submitting ingest instance and expires.
- Exact retries reuse the same render artifact.
- The golden mixed Chinese/math sample contains `≅`, `⊗`, `⊕`, and `∩` without missing
  glyph boxes and wraps within a fixed document width.
