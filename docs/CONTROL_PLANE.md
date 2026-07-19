# 控制面与运维面板设计

## 当前状态与边界

截至 2026-07-19，M0–M3 服务端控制面已经实现并完成生产演练：短会话、角色、
CSRF/Origin/Host、重新认证、canonical preview、CAS、幂等、descriptor lifecycle、
Provider quarantine、Git-bound rollout pause/activate 和只追加审计均已通过双数据库
回归。生产默认仍不配置 operator、Host、Origin 或 pepper，登录返回 503；需要一次性
变更时才临时启用，完成后立即撤除。

浏览器运维面板尚未部署。第三阶段以管理 API/CLI 和 SQL 证据作为 truth surface；
未来面板必须先忠实展示 Core 既有事实，不能成为另一套 authority，也不能因一个绿色
状态就推断整体“healthy”或“eligible”。面板不是第三阶段运行依赖，详细退出证据见
`PHASE3_ACCEPTANCE.md` 与 `DEPLOYMENT.md` 第 18 节。

面板始终必须分开显示：

- **desired:** reviewed Git descriptors, conversation topology, rollout mode,
  policies and explicit operator controls;
- **reported:** authenticated bot/provider inventory and heartbeat claims;
- **effective:** Core's independently evaluated state and stable reason codes;
- **actual:** events, claims, acknowledgements, responses, invocations,
  attempts, artifacts and incidents.

Drift between these layers is first-class data. Runtime discovery, a display
name, or a UI toggle cannot grant authority absent a reviewed contract.

## Initial pages

1. **Overview:** Core/database readiness, bridge/provider freshness, queues,
   reporter counters, rollout modes, stops, current canaries and unexplained
   exceptions. One component's heartbeat never masks another path's outage.
2. **Conversations:** canonical key, desired/effective
   `command_only`/`conversation_only`/`full`/`observe_only`, bot membership,
   Nekro channel activation, claim scope, drift and last verified time.
3. **Routing timeline:** source, observations, references, policy revision,
   claims/deny acknowledgements, suppression, attributed responses and outcome;
   raw identifiers are redacted/scoped.
4. **Tools:** Git descriptor commit/hash, immutable DB copy/lifecycle, provider
   inventory, eligibility reasons, rollout mode and stops. Descriptor content is
   read-only in Phase 3.
5. **Invocations:** principal/input hash, policy snapshot, transitions,
   attempts/fences, budgets, output validation, artifacts and recovery class.
6. **Providers:** stable authenticated identity, credential age, accepted
   inventory, separate heartbeat/load, implementation drift, quarantine and
   lease history. Bot-ingest identity is not provider identity.
7. **HA coverage:** per-adapter epoch/sequence watermarks, durable spool depth,
   oldest unacknowledged age, account/host/path coverage, gaps and failover
   leases. This page does not itself deploy the third account.
8. **Security/Audit:** operator sessions/roles, changes, confirmations,
   break-glass use, secret/custom-URI/raw-retention audits and exported evidence
   bundles.

Every list has bounded time/scope filters and pagination. Detail views link by
opaque IDs; they do not put message text, tokens, inputs, or artifact secrets in
URLs. Exports are explicit, redacted, size-limited and audited.

## Roles and mutations

| Role | Read | Mutate after its gate |
|---|---|---|
| `auditor` | all redacted operational/audit views and evidence export | none |
| `operator` | health, routing, providers, invocations | pause/resume reviewed rollout scope, cancel safe work, acknowledge incidents |
| `reviewer` | descriptors, policy diffs and canary previews | approve imported lifecycle/policy transitions; cannot manage credentials |
| `security_admin` | security/audit and credential metadata | rotate/revoke credentials, quarantine providers, manage role assignments |
| `break_glass` | incident-scoped necessary views | time-limited emergency global stop/revocation with mandatory reason and follow-up |

No single role both authors descriptor content and silently activates it.
Dangerous mutations show a server-computed preview of desired/effective diff,
require a reason and fresh reauthentication, use an idempotency key and expected
resource version, and append before/after hashes plus outcome to immutable
audit. Stale compare-and-swap fails closed. Rollback is a new audited mutation,
not history deletion.

## Web security

- Use short-lived server-side sessions in `Secure`, `HttpOnly`, `SameSite`
  cookies; never keep admin/provider/bot bearer tokens in browser local storage.
- Protect every mutation with CSRF validation, exact Origin/Host checks,
  content-type enforcement, rate limits and authorization at the API, not only
  hidden buttons.
- Reauthenticate for credential, role, global-stop, provider-quarantine,
  descriptor-lifecycle and canary-expansion changes. `break_glass` expires
  automatically and alerts other administrators.
- Apply CSP, no third-party script by default, output escaping, safe downloads,
  bounded queries, redaction and cache-control. Tool output, message text,
  provider errors and artifact metadata are untrusted content.
- Audit login/session events, reads of sensitive detail, exports, previews,
  attempted/accepted/rejected mutations and resulting effective state. Audit
  storage is append-only and retention is separate from chat/artifact retention.

## Rollout

1. CLI/API evidence remains authoritative while the page/query contracts are
   tested.
2. Ship read-only Overview, Conversations, Routing, Tools and Providers.
3. Add Invocations/artifacts only after their ledgers exist; add HA Coverage
   only after HA-0 envelope/spool contracts exist.
4. Add authenticated auditor sessions and compare every panel count/reason with
   direct API/SQL evidence.
5. Enable one low-risk operator mutation at a time only after role, preview,
   CSRF, reauth, CAS, idempotency, audit and rollback fault tests pass.
6. Keep descriptor editing and secret values outside the panel during Phase 3.

Panel availability is never a runtime dependency for event ingestion, claims,
tool leases, emergency CLI stop, or bot fail-open behavior.

## Compose 运维注意

生产临时启用控制面时，scrypt verifier 中的每个 `$` 在 Compose `.env` 里必须写成
`$$`，否则 Compose 会尝试按环境变量展开并破坏 verifier。应用进程最终收到的仍应是
单个 `$`。任何配置脚本都必须在容器内用 `Settings.from_env()` 复核角色数量和格式，
但不得输出 verifier。

`SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON` 只接受精确 HTTPS origin；本机回环 HTTP
也不能作为 origin 配置。无浏览器的本机操作可以连接回环端口，同时显式提交经审阅的
HTTPS Origin 与精确 Host，但不能因此放宽服务端 Origin/Host/CSRF/JSON 校验。错误
verifier 或非 HTTPS origin 应让 Core 启动失败，而不是静默关闭校验。
