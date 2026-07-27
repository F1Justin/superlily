# Development

## 文档语言约定

自 2026-07-18 起，仓库中新建或实质改写的面向人的项目文档统一使用简体中文。代码标识符、API 字段、协议名、命令和必须逐字匹配的错误信息保留原文。已经验收的历史 ADR、审计证据和部署记录不为统一语言而批量改写；后续新增的 ADR、设计说明、运维记录及其新章节仍应使用中文。

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

Phase 5a 的重点回归不需要真实 LLM endpoint。测试导入一份本地 reviewed profile，
通过独立模型 Provider 身份提交结构化 proposal，并断言零工具调用和零发送：

```bash
.venv/bin/pytest -q \
  tests/test_agent_runtime_contracts.py \
  tests/test_agent_run_api.py \
  tests/test_migrations.py
```

运行时默认 `SUPERLILY_AGENT_MODE=off`。本地启用 `shadow` 时必须同时配置非空且不与
任何现有认证域重用的 `SUPERLILY_MODEL_PROVIDER_TOKENS_JSON`；这不会开放
`caller=agent`，也不会执行提案。

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

`vectors/` 下的 descriptor 只是测试向量，不是可导入的生产 authority。该 CLI
命令只做离线校验，不能导入、激活或执行工具。

Phase 3a 的历史报告版本与 Phase 3b 的可执行候选应分别验证：

```bash
.venv/bin/superlily-tool-registry verify-descriptor \
  registry/descriptors/status.inspect/1.0.0.json
.venv/bin/superlily-status-provider verify \
  --descriptor registry/descriptors/status.inspect/1.0.0.json
.venv/bin/superlily-tool-registry verify-descriptor \
  registry/descriptors/status.inspect/1.0.2.json
.venv/bin/superlily-status-provider verify \
  --descriptor registry/descriptors/status.inspect/1.0.2.json
.venv/bin/pytest -q tests/test_provider_sdk.py tests/test_status_provider.py
```

`verify` 只运行本地、受 schema 约束的自检，不连接 Core，也不领取 lease。
`1.0.0` 固定代表只报告实现；`1.0.1` 保留为首个已部署的执行候选历史 authority；
`1.0.2` 使用创建时不继承 secret 的独立 worker、硬 wall-time/输出字节监督和带裕量的
内存预算，是当前 canary 候选。
Provider execution SDK 的网络操作均为单次调用，不对不明确的状态变更响应盲目重试。

Phase 3a Registry 与 Phase 3b 执行账本的重点回归可分别运行：

```bash
.venv/bin/pytest -q \
  tests/test_tool_registry_contracts.py \
  tests/test_provider_sdk.py \
  tests/test_status_provider.py \
  tests/test_tool_registry_api.py \
  tests/test_migrations.py

.venv/bin/pytest -q \
  tests/test_tool_execution_contracts.py \
  tests/test_tool_execution_api.py \
  tests/test_provider_sdk.py \
  tests/test_status_provider.py \
  tests/test_migrations.py
```

C0-D 合同、action ingestion、receipt/watermark 幂等和 migration 往返由
`tests/test_contracts.py`、`tests/test_api.py` 中的 `test_c0d_*` 用例和
`tests/test_migrations.py` 覆盖。修改 bridge spool 前必须在一次性 SQLite 与
PostgreSQL 上都运行。

应用 `0012_tool_registry` 后，初始 admin 视图必须报告零 descriptor、零 Provider
和 execution `off`。本机管理 CLI 只有 `import-descriptor` 与
`register-provider`；它从精确 `--source-commit` Git 对象读取 descriptor 字节，
不会激活工具。单 descriptor bundle 的 `--bundle-hash` 由
`superlily-tool-registry verify-descriptor` 给出。不得把共享 `status.inspect`
测试向量当作生产 authority 导入。

Historical imports start with a write-free dry run. Candidate records should be
normalized to EventIn-shaped JSONL first, then inspected with:

```bash
.venv/bin/python -m superlily_core.history_import /path/to/candidates.jsonl
```

The report validates contracts and counts references, text fields, message IDs,
and original source labels; it does not write to Core storage.
