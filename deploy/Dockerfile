FROM python:3.13-slim@sha256:eb43ff125d8d58d7449dcba7d336c23bcac412f526d861db493b9994d8010280

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY deploy/constraints.txt ./deploy/constraints.txt
COPY apps ./apps
COPY packages ./packages
RUN python -m pip install --constraint deploy/constraints.txt . && chmod -R a+rX /app

USER 65532:65532
EXPOSE 8000

CMD ["uvicorn", "superlily_core.app:app", "--host", "0.0.0.0", "--port", "8000"]
