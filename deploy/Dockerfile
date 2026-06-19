FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY apps ./apps
COPY packages ./packages
RUN python -m pip install . && chmod -R a+rX /app

USER 65532:65532
EXPOSE 8000

CMD ["uvicorn", "superlily_core.app:app", "--host", "0.0.0.0", "--port", "8000"]
