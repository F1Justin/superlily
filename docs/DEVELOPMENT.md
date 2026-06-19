# Development

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
```

Run tests against a disposable PostgreSQL database:

```bash
sudo docker run --rm -d --name superlily-test-postgres \
  -e POSTGRES_DB=superlily_test \
  -e POSTGRES_USER=superlily \
  -e POSTGRES_PASSWORD=test-only-password \
  -p 127.0.0.1:55432:5432 postgres:17-alpine

SUPERLILY_TEST_DATABASE_URL=postgresql+asyncpg://superlily:test-only-password@127.0.0.1:55432/superlily_test \
  .venv/bin/pytest -q
```

Production schema changes use Alembic; `create_schema` and `drop_schema` exist
only for disposable tests.

