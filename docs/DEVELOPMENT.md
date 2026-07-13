# Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install \
  --constraint deploy/constraints.txt \
  --constraint deploy/test-constraints.txt \
  -e '.[dev]'
```

Run tests against a disposable PostgreSQL database:

```bash
sudo docker run --rm -d --name superlily-test-postgres \
  -e POSTGRES_DB=superlily_test \
  -e POSTGRES_USER=superlily \
  -e POSTGRES_PASSWORD=test-only-password \
  -p 127.0.0.1:55432:5432 \
  postgres:17-alpine@sha256:dc17045ccfd343b49600570ea734b9c4991cf1c3f3302e67df51e3b402dd55c4

SUPERLILY_TEST_DATABASE_URL=postgresql+asyncpg://superlily:test-only-password@127.0.0.1:55432/superlily_test \
  .venv/bin/pytest -q
```

Production schema changes use Alembic; `create_schema` and `drop_schema` exist
only for disposable tests. The constraints files are the verified resolver
input; `pyproject.toml` ranges remain the package compatibility declaration.

Historical imports start with a write-free dry run. Candidate records should be
normalized to EventIn-shaped JSONL first, then inspected with:

```bash
.venv/bin/python -m superlily_core.history_import /path/to/candidates.jsonl
```

The report validates contracts and counts references, text fields, message IDs,
and original source labels; it does not write to Core storage.
