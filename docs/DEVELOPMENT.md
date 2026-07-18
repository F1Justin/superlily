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

The PostgreSQL fixture creates and drops ORM tables but deliberately does not
own Alembic's `alembic_version` table. Do not run a downgrade test against the
same database after pytest has torn its tables down: it may still report
`head` while the application tables are absent. Recreate the disposable
`public` schema (or use a fresh test container) before a migration round trip,
then run `upgrade head -> downgrade base -> upgrade head -> alembic check`.

Production schema changes use Alembic; `create_schema` and `drop_schema` exist
only for disposable tests. The constraints files are the verified resolver
input; `pyproject.toml` ranges remain the package compatibility declaration.

Run the Phase 3a authority-contract tests and verify the shared descriptor with
the same parser, validator, canonicalizer, and hash implementation used by the
contracts package:

```bash
.venv/bin/pytest -q tests/test_tool_registry_contracts.py
.venv/bin/superlily-tool-registry verify-descriptor \
  packages/contracts/vectors/tool_registry/status.inspect-1.0.0.json
```

The descriptor under `vectors/` is a test vector, not an active registry entry.
The CLI performs offline verification only and cannot import, activate, or run
a tool.

The reviewed production candidate and its reporting-only implementation are
verified separately:

```bash
.venv/bin/superlily-tool-registry verify-descriptor \
  registry/descriptors/status.inspect/1.0.0.json
.venv/bin/superlily-status-provider verify \
  --descriptor registry/descriptors/status.inspect/1.0.0.json
.venv/bin/pytest -q tests/test_provider_sdk.py tests/test_status_provider.py
```

The second command runs only a local schema-bound self-test. The Phase 3a SDK
can publish inventory and heartbeat, but it has no invocation or lease client.
The provider intentionally reports wall-time enforcement as `unsupported`
until the Phase 3b hard-timeout executor exists.

Phase 3a persistence and Core API regression tests are isolated with:

```bash
.venv/bin/pytest -q \
  tests/test_tool_registry_contracts.py \
  tests/test_provider_sdk.py \
  tests/test_status_provider.py \
  tests/test_tool_registry_api.py \
  tests/test_migrations.py
```

C0-D contracts, action ingestion, receipt/watermark idempotency and migration
round trips are covered by `tests/test_contracts.py`, the `test_c0d_*` cases in
`tests/test_api.py`, and `tests/test_migrations.py`. Run them on both disposable
SQLite and PostgreSQL before changing bridge spool behavior.

After `0012_tool_registry` is applied, the initial admin read must report zero
descriptors/providers and execution `off`. The local administration CLI has
only `import-descriptor` and `register-provider`; it reads descriptor bytes
from the exact `--source-commit` Git object and never activates a tool. For the
initial one-descriptor bundle, obtain `--bundle-hash` from
`superlily-tool-registry verify-descriptor`. Do not import the shared
`status.inspect` test vector as production authority.

Historical imports start with a write-free dry run. Candidate records should be
normalized to EventIn-shaped JSONL first, then inspected with:

```bash
.venv/bin/python -m superlily_core.history_import /path/to/candidates.jsonl
```

The report validates contracts and counts references, text fields, message IDs,
and original source labels; it does not write to Core storage.
