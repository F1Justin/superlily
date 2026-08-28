# Nekro prompt-cache overlay

This builds a narrow production overlay on the exact Nekro Agent 2.3.3 image
currently deployed on Kanako. It does not vendor or fork the full upstream
repository.

The patch:

- marks the end of Nekro's stable system plus practice-message prefix with an
  explicit OpenRouter Gemini cache breakpoint;
- removes the two built-in fallback practice conversations while preserving
  adapter-supplied examples;
- supplies an opaque, stable per-chat session ID for OpenRouter sticky routing;
- records cache reads, cache writes, provider-reported cost, and cache discount
  in the existing sandbox `extra_data` JSON;
- preserves the current message's direct reply target when a reduced history
  window would otherwise evict it; and
- captures usage-only final streaming chunks instead of discarding them.

The random per-run history token remains dynamic and is never made stable.
The overlay does not change the system prompt text, plugins, model parameters,
or sandbox behavior. History and image limits remain runtime configuration.

The validated production runtime profile is:

- `AI_CHAT_CONTEXT_MAX_LENGTH: 16`
- `AI_CONTEXT_LENGTH_PER_SESSION: 3200`
- `AI_VISION_IMAGE_LIMIT: 1` (current operator choice; independent of this overlay)
- `MEMORY_ENABLE_SYSTEM: false`

Upstream source: `KroMiose/nekro-agent` tag `v2.3.3`, commit
`a34c32f853c3d530b2372b93f68f8bf2469c5333`.

Registry manifest digest:
`sha256:0817f343aad4f9b4af72b7406a08d2790e9c22fbbedc2c6c91a577339df0668a`.

Verified local image ID:
`sha256:4c209098e439345fed92b9870bf9e4d2361650476b6ab177924f11911167e72e`.

## Commit and image identity

Production builds pass the owning Superlily commit as `VCS_REF` and the Git tag
as `BUILD_VERSION`. The resulting image exposes both values through OCI labels
and receives both the stable `2.3.3-prompt-cache3` tag and a `git-<short-sha>`
tag. The Compose override uses the stable tag; operators can compare its OCI
revision label with the Git tag target before deployment.
