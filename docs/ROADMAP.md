# Superlily 执行路线图

本文是 Phase 2 事件路由底座之后的权威实施顺序。`manifesto.md` 保留架构愿景，
分阶段设计文档负责可实施合同与验收门。

Tool Registry 之后的详细设计见 `docs/FUTURE_PHASES_DESIGN.md`。它定义 Phase
4–11 的共享边界、内部工作包、故障模型和退出门，但不授权提前启动这些阶段。

采集完整性、渐进式工具披露、Unix 风格资源探索、自然语言命令兼容、快速聊天路径
和成本感知模型路由等长期产品共识，记录在
`docs/COLLECTION_AND_AGENT_CONSENSUS.md`。

## 当前位置

Phase 1、Phase 2 和 C0-D1 至 C0-D5 已签署完成；路由、claim/ACK、响应归因、
typed platform capability 和 durable ingress 的验收证据分别见
`ACCEPTANCE.md` 与 `C0D_ACCEPTANCE.md`。C0-A 的长期档案完整性仍可独立推进，
不是 Phase 3b 的 correctness 前置。

Phase 3a 已签署真实 `status.inspect@1.0.0` authority、独立 Provider 身份、
共享 Provider SDK 和只报告的运行时。该描述符继续保留为不可变历史 authority。

Phase 3b 的第一切片 `0014_tool_invocations` 已于 2026-07-19 上线。execution mode
为 `ledger_only`：调用提案会冻结 descriptor、input、principal、capability 和 policy
快照，但只能终止为 `recorded_only` 或 `rejected`。Lily/Nekro bridge 0.5.1 已为
心跳和两个 reporter worker 增加监督与自恢复，两个实例生产心跳已恢复新鲜。

`0015_tool_attempts`、Provider 拉取的单活动 lease、单调 fence、attempt secret、
数据库时间、恢复 reaper 和三个历史执行模式已实现，并在 SQLite 与
PostgreSQL 17 各通过 313 项测试。历史可执行候选 `status.inspect@1.0.1` 已在
`ledger_only` 部署；canary 前审查又新增不可变 `1.0.2`，使用创建时不继承 secret 的
独立 worker、硬 wall-time/输出边界和带裕量的 320 MiB 诚实预算。详细边界见
`docs/PHASE3B_EXECUTION.md`。

`0015` 与新 Provider 先在生产以 `ledger_only` 安全空转，随后又在
`0015d_rollout_plans` 上完成首次有界生产执行。`status.inspect@1.0.2`
已通过审阅者控制面激活为 `active/rv4`；五份来自完整 Git commit
的单次计划分别证明 global stop、descriptor suspension、Provider quarantine、
rollout plan pause 和一次成功 canary。四条停止路径在 deadline 前均为
lease=204/零 attempt；成功路径仅产生 1 个 attempt/fence，没有平台发送。首批五份
计划均已暂停，Core 恢复 `ledger_only`、无 active plan/lease。随后八份单调用计划
完成 safe retry、旧 fence、非法输出、快慢时钟、取消路径以及 Core/PostgreSQL
中断的生产故障矩阵；所有计划均暂停并耗尽，两个不确定结果保留。修正空 lease
keep-alive 边界后，Provider 又跨过完整 inventory 稳定周期且无日志异常。详细证据见
`docs/PHASE3_FAULT_DRILLS.md`。

直接改数据库激活 descriptor 的路径继续被禁止。ADR 0005 的治理包中，M0 会话/
审计底座已默认禁用部署；M1 descriptor lifecycle preview/CAS 已完成实现、审查和
双数据库发布前回归。M1 使用服务端 canonical preview、reviewer 角色、新鲜再认证、
精确资源版本、幂等键、运行时重算和只追加 before/after 证据；数据库也拒绝 authority
改写、证据删改和无匹配 lifecycle event 的状态更新。

M1 当前 SQLite 为 334 项通过、1 项 PostgreSQL 专用测试跳过，PostgreSQL 17 分段
合计 335 项通过；迁移往返和 drift 均通过。生产已默认禁用迁移到
`0015b_descriptor_mutations`，operator/Host/Origin/pepper 为空，5 张控制面表为零，
`ledger_only` 且零 attempt。M2 Provider quarantine 已完成实现、审查与双数据库
全量回归：SQLite 341 项通过、2 项 PostgreSQL 专用测试跳过，PostgreSQL 17 合计
343 项通过，并已默认禁用部署到 `0015c_provider_quarantine`：Provider 仍为
`active/resource_version=1`，控制面五表为零，preview 返回 503。M3 Git-bound 精确
rollout plan 现已完成实现和双数据库关键回归：环境 scope 被废止，执行模式只作为
`off/ledger_only/canary` 上限；reviewed plan 精确绑定工具、会话、caller、Provider、
资源版本、24 小时内窗口和调用上限，调用创建与 lease 都会重验并支持可审计 pause。
默认禁用生产迁移与首次计划已于 2026-07-19 完成：`0015d` head/no
drift，五份计划各消费 1 次并停在 `paused/rv3`，Registry 无 active plan/lease。
四个独立 stop 和首个 `admin_api` 精确 canary 已有生产证据；恢复故障矩阵与稳定
窗口现也已签署。`0016_confirm_artifacts` 随后完成精确请求 confirmation、批准时
消费 rollout、内容寻址 artifact、Provider reserve/upload/finalize SDK、保留期/
orphan 清理和字段级数据库 guard。SQLite 全量为 439 通过、4 跳过，PostgreSQL 17
全量为 443 通过；生产已经备份并在独立 PostgreSQL 17 磁盘卷中恢复验证，随后默认
关闭迁移到 `0016`，新表全零、旧调用/计划计数不变且无 schema drift。它仍不扩大
conversation、caller 或自然语言 authority，工具循环仍属 Phase 5。

文本模式 `wolfram.run@1.0.0` 的实现与发布前审查现已完成：它由独立 Provider 通过
私有 Unix socket 复用既有 Wolfram 15.0 隔离 worker，只返回有界文本，不接收图片、
不产生 artifact、不发送平台消息。worker image/source/version/隔离配置进入部署身份，
取消无法被旧协议证明时保守收敛为 `unknown_completion`。SQLite 全量为 455 通过、
4 跳过，PostgreSQL 17 全量为 459 通过；受限镜像的真实 `2+2` 探针返回 `4`。
生产随后从完整 Git commit 注册 Provider、导入并通过 reviewer 激活 descriptor；
一份最多一次的 plan 经 operator 激活后，唯一 `admin_api` canary 用一个 attempt/fence
返回 `4`、wall=8 ms、artifact=0。旧 `/wf` data source 串行对比同样返回 `4`。
计划已暂停并耗尽，Core 恢复 `ledger_only`、active plan/attempt 为 0、临时控制面关闭。
随后两份 300 秒间隔的 inventory hash 一致，10 次 heartbeat 全部 healthy，Provider
零新日志、相关容器零重启。文本 `wolfram.run` 因而完成迁移签署。随后
`latex.render@1.0.0` 已完成发布前实现：独立 Provider 调用无网络、无凭据、只读
rootfs、1 GiB/1 CPU/128 PIDs 的 XeLaTeX/Poppler worker，图片只能通过
reserve/upload/finalize 成为单张 4 MiB、2048×2048 内的 PNG。真实宿主/容器渲染、
恶意 TeX、错误脱敏、严格 socket/framing 和 artifact 顺序测试已通过；旧 `/tex`
未改，自然语言和平台发送仍关闭。SQLite 全量为 463 通过、4 跳过，隔离 PostgreSQL
17 为 467 全通过。下一生产门是启用 artifact store、激活 descriptor
和执行精确一次 finalized artifact canary。详细证据见 `PHASE3_WOLFRAM_TEXT.md`、
`PHASE3_LATEX_RENDER.md`、`DEPLOYMENT.md` 第 17 节、ADR 0013 与 ADR 0014。

## Sequencing rules

1. A phase begins only after the previous phase's acceptance evidence exists,
   rollback is documented, and production defaults remain safe.
2. Contracts and ledgers precede execution. Execution precedes model autonomy.
3. Read-only, deterministic, public operations migrate before costly,
   privacy-sensitive, state-changing, or administrator operations.
4. Tools return structured results and artifacts; tools do not send platform
   messages. Rendering and delivery are separate responsibilities.
5. Missing identity, authority, capability, registry freshness, confirmation,
   budget, or provider health always reduces authority rather than expanding
   it.
6. Existing command entry points stay available until their tool-backed path
   has passed shadow comparison, exact-conversation canary, and rollback.

## Completed authority-neutral packet: C0-D collection reliability

C0-D is an earlier-layer reliability repair scheduled after deployed Phase 3a
and before Phase 3b. Its detailed scope and acceptance criteria are in
`docs/COLLECTION_AND_AGENT_CONSENSUS.md`.

- Define exact capture profiles, versioned sanitizer/completeness metadata,
  and normalized basic platform actions; reaction capture records facts and
  assigns no feedback semantics.
- Keep image bytes out of PostgreSQL while retaining ordered placeholders and
  available metadata. Any future binary retention uses bounded object storage.
- Replace loss-prone in-memory-only telemetry with idempotent durable bridge
  spools, commit receipts, replay, watermarks, lag/gap diagnostics, quotas, and
  quarantine.
- Preserve per-account observations and resolve only with verified strong
  identity. Old or unresolved data is never merged by text/time similarity.

C0-D is complete only when outage/restart replay loses no controlled records,
Lily and Nekro basic action notices are represented with honest completeness
status, receipt/coverage diagnostics and media policy are enforced, and
command/claim/Registry behavior is unchanged.

Those gates passed and were signed on 2026-07-18. The production evidence and
exact controlled sequence ranges are recorded in `C0D_ACCEPTANCE.md`.

After C0-D is stable, `C0-A` adds nested merged-forward expansion,
`archive_full` rollout, portable exports, reconstruction, retention/deletion
propagation, and old-history import. C0-A and Phase 3b may be scheduled
independently; Phase 3b does not wait for archival edge cases.

## Dependency shape

The numbered phases describe release gates, not a ban on preparatory design.
Contracts for a later phase may be designed early, but production authority is
enabled only in dependency order:

```text
Phase 2 event identity / decisions / claims / capabilities
    -> Phase 3 tool contract / invocation ledger / provider protocol
        -> Phase 4 renderer IR / artifact delivery / capability degradation
            -> Phase 5 natural-language planning and tool loop

Phase 3 health + audit -> Phase 6 Watchdog
Phase 4 adapter boundary -> Phase 7 additional platforms
Phase 3 query tools + Phase 5 on-demand calls -> Phase 8 Memory as Tool
Phases 3/4/6/7 -> Phase 9 event operations
Phases 4/7/9 -> Phase 10 Fumo and avatar adapters
All stable boundaries -> Phase 11 optional legacy runtime replacement
```

## Phase 3: Tool Registry and controlled execution

Detailed design: `docs/PHASE3_TOOL_REGISTRY.md`. Phase acceptance is in
`docs/PHASE3_ACCEPTANCE.md`; the future operator UI and mutation boundary are in
`docs/CONTROL_PLANE.md`.

### 3a. Authoritative tool descriptors

状态：2026-07-18 已完成真实、经审阅的 `status.inspect@1.0.0` authority 与
只报告的 Provider。此处描述的是当时的 3a 退出门；后续 invocation 与 lease
表面由 3b 的独立迁移提供，不改变 3a 的历史签署。

- Define versioned input/output JSON Schemas, side-effect class, permission,
  confirmation, timeout, rate, concurrency, resource, privacy, provider, and
  required-output-capability metadata.
- Accept authenticated runtime provider snapshots, but keep reviewed authority
  separate from runtime discovery just as Phase 2 separates static command
  review from matcher inventory.
- Expose admin audit views for loaded, stale, missing, incompatible, and
  unreviewed tools.
- Use a Git-tracked reviewed descriptor bundle as the authority source. Store
  immutable canonical descriptor copies/hashes and lifecycle records in the
  database; neither provider inventory nor the control panel edits descriptor
  authority in 3a.
- Separate stable provider registration/authentication from dynamic inventory
  and heartbeat health. Provider credentials are not bot-ingest/admin tokens.
- Do not execute tools in 3a.

Exit gate: descriptor canonicalization and hashes are deterministic; unknown or
stale providers cannot become callable; schema compatibility and authority are
covered on SQLite and PostgreSQL.

### 3b. Invocation ledger and provider lease protocol

状态：`0014_tool_invocations`、`0015_tool_attempts`、M0–M3 控制面和
`status.inspect@1.0.2` 已完成实现、双数据库回归与生产部署。
`status.inspect@1.0.2` 已为 `active`；五份 Git-bound 单次计划已完成独立
stop 和首次成功 canary，随后全部暂停，生产恢复 `ledger_only`。实施、
证据与回滚细节见 `docs/PHASE3B_EXECUTION.md`、`docs/PHASE3_ACCEPTANCE.md`
和 `docs/DEPLOYMENT.md`。

- Add an auditable invocation state machine, attempts, confirmations, leases,
  fencing tokens, deadlines, cancellation, structured errors, and artifact
  references.
- Providers pull authenticated leases from Core. Core does not open inbound
  ports into Lily/Nekro and does not run plugin code inside the API process.
- Validate input before queueing and output before success. Capture the exact
  descriptor, principal, policy, capability, and budget snapshots used.
- Apply idempotency per source event/tool/request and never retry an ambiguous
  state-changing completion automatically.
- Ship `off`, `ledger_only`, and Git-bound exact canary ceilings plus independent
  global stop, tool suspension, provider quarantine, and rollout-plan pause
  before a real lease. `enforce` remains closed in the first M3 package.
  Canary scope binds exact tool/version, conversation, caller, and provider.
- Use database time for leases/deadlines. Specify cancellation, reaping, late
  completion, invalid output, clock skew, unknown completion, and starvation
  transitions before implementation.
- Implement reserve/upload/finalize content-addressed artifacts before
  `latex.render` or image-producing Wolfram output can succeed.

Exit gate: crash/restart, duplicate delivery, expired lease, timeout,
cancellation, malformed result, provider outage, and Core outage all have
tested deterministic outcomes.

### 3c. First providers and migration order

Recommended order:

1. `status.inspect`: read-only, deterministic, small structured output.
2. `wolfram.run`: bounded compute, persistent worker, explicit time/memory and
   expression-size budgets.
3. `latex.render`: read-only artifact production with MIME/hash/size metadata.
4. `markdown.render_image`: artifact production after renderer boundaries are
   stable enough to avoid duplication with Phase 4.
5. `history.search`: read-only but privacy-sensitive; requires conversation
   scope and audit policy first.
6. Configuration, moderation, announcements, restarts, and other writes only
   after sender authorization and confirmation are implemented.

Each old command becomes a compatibility adapter that creates a typed tool
invocation. The provider may initially wrap existing code, but command parsing,
permission checks, tool execution, result structure, rendering, and sending
must become separately observable steps.

### 3d. Shadow and canary migration

- Shadow only metadata and read-only results; never double-execute a write.
- Compare old-command output with tool-backed structured output using bounded,
  redacted diff records.
- Canary one tool and one exact conversation at a time.
- Keep the old path as rollback until latency, error rate, result equivalence,
  resource budgets, and audit completeness pass a stable window.

Phase 3 exit gate: at least `status.inspect`, `wolfram.run`, and
`latex.render` use the common descriptor and invocation protocol; command
compatibility remains; no natural-language model has execution authority yet.
The control panel may expose read-only effective state during Phase 3, but
mutating operator controls remain gated by the roles/session/audit requirements
in `CONTROL_PLANE.md`.

## Phase 4: Unified Renderer

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-4-unified-renderer-and-delivery-boundary`.

### Deliverables

- Define a versioned `RenderDocument` intermediate representation for text,
  headings, tables, code, math, images, cards, progress, warnings, and artifact
  references.
- Add content-addressed artifacts with MIME, hash, byte size, dimensions,
  provenance, TTL, access scope, and retention.
- Implement renderer providers separately from platform delivery.
- Negotiate against the Phase 2 capability snapshot and record every
  degradation, such as Markdown to image or image to plain text.
- Cache only deterministic, non-sensitive render results; include renderer
  version and normalized input in cache keys.

### Safety and exit gate

Untrusted HTML/SVG/Markdown is sanitized; local-file and remote-fetch policy is
explicit; font and image work has resource limits. Wolfram, LaTeX, status, and
help results render identically through command and tool paths, with QQ text
and image fallbacks tested. Tools no longer call platform send APIs directly.

## Phase 5: Natural-language Tool Calling

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-5-natural-language-planning-and-controlled-tool-loop`.

### 5a. Planner without execution

- Build the light default context: system policy, current message/reply graph,
  short conversation window, and only eligible tool summaries.
- Record proposed calls and explanations without execution.
- Measure false calls, missed calls, schema validity, and permission requests.

### 5b. Read-only execution loop

- Allow a bounded number of calls and model turns with total time/token/cost
  budgets.
- Core validates every call against the Phase 3 descriptor and principal
  policy; the model never talks directly to providers.
- Expose `docs.search`, `state.get`, and later `history.search` only on demand;
  there is no default RAG injection.

### 5c. Confirmed writes

- State-changing tools require explicit confirmation bound to principal, tool,
  normalized arguments, scope, expiry, and invocation ID.
- A changed argument or expired confirmation returns to proposal state.
- High-risk operations can require a second administrator or an out-of-band
  channel.

Exit gate: adversarial prompts, tool-result injection, loops, provider errors,
timeouts, budget exhaustion, confirmation replay, and model failover are
covered; command behavior remains independent of model availability.

## Phase 6: Three-account coordination and Watchdog

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-6-watchdog-incidents-and-role-failover`.
Three-account collection/failover design:
`docs/PHASE6_THREE_ACCOUNT_HA.md`.

Availability-priority decision: the authority-neutral `HA-0` durable ingress
spool and coverage packet is pulled forward into C0-D after Phase 3a and before
Phase 3b. This does not deploy Reserve or enable failover egress; the remaining
Phase 6 release gates stay in numbered order.

- Model Command, Talk, and Watchdog as explicit roles with capability and
  health snapshots, not hard-coded account IDs.
- Run collection active/active/active across Command, Talk, and a continuously
  connected silent Reserve account. Response remains active/passive: Reserve
  speaks only while holding a bounded logical-role lease.
- Add durable per-adapter ingress spools and coverage/watermark diagnostics;
  another in-memory observer does not by itself guarantee no message loss.
- Define a degradation matrix per tool: primary provider, permitted fallback,
  required health, reduced limits, and forbidden failover.
- Reserve's adapter collects protected ordinary chat for ingestion coverage,
  but Watchdog/incident logic consumes only health, coverage, status, and
  incident events. It cannot silently acquire chat context or tool authority.
- Use leases/fencing for failover, cooldown and hysteresis for recovery, and
  an administrator-visible incident timeline.
- When automatic failover is active, managed egress becomes lease-required;
  ingress remains durable and fail-open. This prevents a partitioned primary
  and Reserve from speaking simultaneously.

Exit gate: loss of each bot, NapCat, provider, Core, PostgreSQL, and network
path has a tested outcome; failback does not duplicate a reply or invocation.

## Phase 7: Additional platform entry points

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-7-additional-platforms-and-web-admin`.

Order: Telegram administrator private chat, Web Admin, then lower-priority
WeChat/Discord/email/live-stream adapters.

- Keep platform adapters limited to identity, events, references,
  capabilities, delivery, and platform acknowledgements.
- Map principals across platforms explicitly; never equate display names.
- Web Admin initially exposes read-only audit and health, then confirmation and
  configuration behind stronger authentication, CSRF protection, and an
  immutable audit trail.
- Cross-platform forwarding is a state-changing tool with consent and privacy
  policy, not an adapter shortcut.

Exit gate: the same event/tool/render contracts work on QQ and one second
platform without platform-specific branches in tool providers.

## Phase 8: Memory as Tool

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-8-retrieval-and-memory-as-tool`.

Build in privacy order:

1. `state.get`: explicit structured group/task/event state.
2. `docs.search`: curated documentation with source/version metadata.
3. `history.search`: scoped lexical/SQL search over authorized conversations.
4. `memory.lookup`: curated long-lived facts with provenance and expiry.
5. Embeddings/reranking only where exact retrieval is insufficient.

Every result carries source, scope, time, confidence, and redaction metadata.
Writes require a separate reviewed path; inferred personal profiles are not
silently persisted. Retention, deletion, export, and consent are acceptance
requirements, not later cleanup.

## Phase 9: Event operations

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-9-event-operations`.

- Treat programs, tickets, check-in, raffle pools, staff actions, timers, OBS
  scenes, and announcements as structured state and tools.
- Use an event-scoped role model and a rehearsal/simulation mode.
- Make raffle inputs and draws reproducible and auditable; secrets and attendee
  data receive stricter retention.
- Add offline/degraded runbooks so a venue network failure does not stall the
  event.

Exit gate: a full rehearsal can be replayed from the audit log, and every
state-changing action has confirmation, operator identity, and rollback or
compensation behavior.

## Phase 10: Fumo and avatar adapters

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-10-fumo-and-avatar-adapters`.

- Define versioned output intents: `speak`, `subtitle`, `emotion`, `action`,
  `display_card`, and `attention`.
- Devices and Live2D/OBS clients render intents but own no planning or tool
  authority.
- Add session leases, physical emergency stop/mute, privacy indicators for
  microphones/cameras, bounded queues, and safe offline behavior.

Exit gate: disconnects, stale commands, duplicated intents, audio feedback,
and operator override are tested; no device credential grants Core admin tool
authority.

## Phase 11: Optional legacy runtime replacement

Detailed design: `docs/FUTURE_PHASES_DESIGN.md#phase-11-optional-legacy-runtime-replacement`.

Replacement is evidence-driven and component-by-component. A custom OneBot or
Satori adapter, plugin host, agent loop, runner, sandbox, or admin UI replaces
NoneBot/Nekro only after the shared contracts have proven that component is a
replaceable boundary. Each replacement needs traffic shadowing, behavior and
latency comparison, a data-migration boundary, and immediate rollback.

The project is complete without full replacement if the legacy runtimes remain
healthy providers behind stable Lily Core contracts.

## Cross-phase production gates

Every authority-increasing release records:

- schema migration upgrade/downgrade on SQLite and the production PostgreSQL
  major version;
- deterministic contract/hash compatibility;
- authentication and principal binding;
- permission, confirmation, capability, and budget decisions;
- idempotency/concurrency/crash behavior;
- redaction, retention, and artifact privacy audit;
- latency/error/resource baselines and alert thresholds;
- exact canary scope, fail-open/fail-closed choice, rollback, and a stable
  evidence window;
- updated operator runbook and acceptance checklist.
