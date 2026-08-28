ARG BASE_IMAGE=kromiose/nekro-agent@sha256:0817f343aad4f9b4af72b7406a08d2790e9c22fbbedc2c6c91a577339df0668a
FROM ${BASE_IMAGE}

ARG VCS_REF=unknown
ARG BUILD_VERSION=unknown
LABEL org.opencontainers.image.title="Superlily Nekro prompt-cache overlay" \
      org.opencontainers.image.source="https://github.com/F1Justin/superlily" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.version="${BUILD_VERSION}"

COPY nekro-agent-prompt-cache.patch /tmp/nekro-agent-prompt-cache.patch
COPY nekro-agent-runtime.patch /tmp/nekro-agent-runtime.patch
COPY nekro-agent-startup.patch /tmp/nekro-agent-startup.patch
COPY verify_overlay.py /tmp/verify_overlay.py
RUN cd /app \
    && patch --batch --forward -p1 < /tmp/nekro-agent-prompt-cache.patch \
    && patch --batch --forward -p1 < /tmp/nekro-agent-runtime.patch \
    && patch --batch --forward --fuzz=0 -p1 < /tmp/nekro-agent-startup.patch \
    && python -m compileall -q \
        nekro_agent/core/os_env.py \
        nekro_agent/services/agent/creator.py \
        nekro_agent/services/agent/openai.py \
        nekro_agent/services/agent/run_agent.py \
        nekro_agent/services/agent/templates/compiler.py \
        nekro_agent/services/agent/templates/history.py \
        nekro_agent/schemas/sandbox.py \
        nekro_agent/services/agent/resolver.py \
        nekro_agent/services/sandbox/runner.py \
    && uv run python /tmp/verify_overlay.py \
    && rm /tmp/nekro-agent-prompt-cache.patch /tmp/nekro-agent-runtime.patch /tmp/nekro-agent-startup.patch /tmp/verify_overlay.py
