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

旧版 Historical imports 段落现在只是 legacy EventIn lint：候选记录先规范化成
EventIn 形状的 JSONL，再用 `history_import` 做离线校验。报告验证 contract 并统计
references、text fields、message IDs 与原始 source labels；它不写入 Core 存储，
**不是 H2 importer**。

真正 H2 的零写入 legacy dry-run 使用主模块 CLI：

```bash
.venv/bin/python -m superlily_core.history_import legacy \
  --source lily --cutover 2026-06-19T11:45:17.17105+00:00 \
  --snapshot-id botmsg-readonly-snapshot-YYYYMMDDTHHMMSSZ \
  --source-schema-version chatrecorder-v2 \
  --mapping-version history-map-v1 \
  --jsonl /path/to/legacy-export.jsonl \
  --output /path/to/legacy-manifest.json
```

`--source` 取 `lily` 或 `nekro`；cutover 必须使用 H0 冻结的对应 UTC 值。Nekro CLI 仍传
Core 微秒边界，但工具会按 H0 的来源粒度合同使用 `send_timestamp < 1781869784`，并在
manifest 的 `source_cutover_boundary` 中明确记录 `2026-06-19T11:49:44+00:00`。
输入必须是只读快照/导出行；快照身份、来源 schema 版本和映射版本都必须显式记录。
本次调用 `writes=0`，
不连接目标 archive，也不触发任何后续 sample 写入——sample 写入另有独立授权门。
大批量导出可以把 `--jsonl` 设为 `-`，通过 stdin 流式传入；重复来源 ID 使用临时磁盘
ledger 检查，结束即删除，不把百万级 identity 集合常驻内存。

冻结来源导出由 `scripts/history/export_lily_snapshot.sql` 和
`scripts/history/export_nekro_snapshot.sql` 生成严格的一行一个 JSON object；脚本只执行
`COPY (SELECT ...) TO STDOUT`，不修改来源库。导出文件和 manifest 校验通过后，H2 writer
必须显式指定写权限开关，并依次运行 sample、month、full：

```bash
SUPERLILY_DATABASE_URL='postgresql+asyncpg://archive-writer@target/superlily' \
  .venv/bin/python -m superlily_core.history_archive_import \
  --source lily \
  --jsonl /path/to/lily-snapshot.jsonl \
  --manifest /path/to/lily-manifest.json \
  --scope sample --conversation-key SOURCE_KEY --max-rows 100 \
  --chunk-size 100 --write-archive

SUPERLILY_DATABASE_URL='postgresql+asyncpg://archive-writer@target/superlily' \
  .venv/bin/python -m superlily_core.history_archive_import \
  --source lily \
  --jsonl /path/to/lily-snapshot.jsonl \
  --manifest /path/to/lily-manifest.json \
  --scope month --month YYYY-MM --chunk-size 5000 --write-archive

SUPERLILY_DATABASE_URL='postgresql+asyncpg://archive-writer@target/superlily' \
  .venv/bin/python -m superlily_core.history_archive_import \
  --source lily \
  --jsonl /path/to/lily-snapshot.jsonl \
  --manifest /path/to/lily-manifest.json \
  --scope full --chunk-size 20000 --write-archive
```

Nekro 使用同样三步并把 `--source` 改为 `nekro`。每次 apply 都先重算并核对完整
manifest；目标必须是 PostgreSQL 且位于当前 Alembic head（本阶段为
`0031_platform_api_calls`；旧 H2 工具也兼容已冻结的 `0026_history_timeline_export`）。writer 通过临时
staging + `COPY` 分块提交，batch checkpoint 记录输入行号和分范围计数，来源身份 ledger
使同一 scope 重跑为零新增；full 完成后更新 archive bulk-load 统计，避免版本化 timeline
沿用导入前的空表计划。不得跳过 sample/month 直接全量，也不得把生产 DSN 留在 shell
history 或仓库文件中。

输入列和 JSON 类型必须遵守 `HISTORY_UNIFICATION.md` §5.1。尤其 Lily `time` 必须是
无 offset 的完整 UTC 时间，`bot_id`/`sender_id`/`scene_type` 必须由 session 关系展平；
Nekro `send_timestamp` 保持来源整数 epoch 秒，不能经过 binary float。报告使用
`history-dry-run-v1`，包含逐会话及时间范围等对账维度；`sample_rejections` 只暴露来源 ID、
有限错误码和无正文诊断。
