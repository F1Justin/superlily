# Phase 5a/5b 生产签署

本文记录 2026-07-30 Phase 5a planner-only shadow 与 Phase 5b 单次只读 Wolfram
loop 的生产证据。签署范围只有：

- 5a：真实 DeepSeek 模型在 Core authority 下完成一次无执行、无发送 shadow；
- 5b：一个 Git-bound exact canary 把一条有效 proposal 提升为一次
  `wolfram.run@1.1.0` 调用，并把不可信结果回注模型一次；
- 回落：生产恢复 `SUPERLILY_AGENT_MODE=off` 与
  `SUPERLILY_TOOL_EXECUTION_MODE=ledger_only`。

本签署不开放 5c 写工具、confirmation 的 `agent` caller、历史检索、长期自治或公开群
自动回复。整个验收只使用 `system:system:phase5-acceptance` 内部会话，没有向
QQ/NapCat 或任何群聊发送合成消息。

## 1. 发布包与数据库

- 实现与验收驱动提交：
  `31989a8fe19824d5bc94aa3529b631213ca521aa`
- exact rollout plan 提交：
  `2253c6c634e62c82e7ae0fedb006032b37d8be8b`
- Core 镜像：
  `sha256:a4c096b22b91b1506f3e000696d39c050a99b2519b9d875c125eb85687c227aa`
- Wolfram Provider 镜像：
  `sha256:d7de00dc75dea8df9bcb39e8d6da275e9dc0dc49d4ade45cf4924d7c58d6ff4d`
- SQLite 全量：547 passed、4 skipped、1 个既有 Starlette 422 deprecation warning，
  56.54 秒
- PostgreSQL 17 全量：551 passed、1 个同类 warning，136.41 秒
- 两个镜像内 `pip check` 均通过

迁移前逻辑备份位于主机权限 0700 的
`/home/justin/backups/superlily/20260730-phase5-agent-runtime/`；dump 文件权限
0600、大小 274,134,448 字节，SHA-256 为
`6ab2c7cc6812c1974331f4ec24384b16cc845a935ebe4c6dbcfe3b522abd1d43`。
`pg_restore --list` 通过，并在 `network=none` 的一次性 PostgreSQL 17 中以
`--exit-on-error` 完整恢复。恢复副本确认迁移 head 为 `0019_phase4_planning`，
并恢复出 source event 549,827、observation 601,857、ingress receipt 190,248、
response 23,718、descriptor 5、Provider 3、rollout plan 16、invocation 24、
attempt 16、artifact 2、RenderDocument 257、delivery intent 153 和 19 个非内部
trigger。一次性容器和匿名卷随后删除，主机备份保留。

生产 Core 将 PostgreSQL 从 `0019_phase4_planning` 线性迁移到
`0023_agent_model_routes (head)`；`alembic check` 无 drift。生产 PostgreSQL
没有重启。默认 `off` 验证中，创建 AgentRun 返回 409，Registry 为
`ledger_only`、无 active rollout、lease 关闭、natural-language caller 关闭。

Git-bound 资源为：

- `deepseek-v4-pro@1.0.0` profile hash
  `948f9b7cd20394f0607d1bb347f776e80f5b5e307c381223b8a40d3bf735bec3`
- `wolfram.run@1.1.0` descriptor hash
  `ec3375907804f588d765ed643b9c8481eb2d4a578924a614652cca64d0414da4`
- Wolfram inventory hash
  `f91ae5fbae7501febea9353748cb296901aebd0fb94f66776c4b0400b2cdf6af`
- Wolfram implementation hash
  `5a37a59e5422aab0926a2e22ee9700d3d0e268f52720f263461f1652320d9366`

模型 Provider token 是与现有凭据均不相同的一次性随机 token。DeepSeek API key
只从既有 Nekro 模型组读取到验收进程环境，没有复制进 Core、Git 或验收账本文本。

## 2. Phase 5a 生产 shadow

2026-07-30 12:46 CST，Core 临时切到 `shadow + ledger_only`，专用驱动执行一次真实
DeepSeek 请求：

- run：
  `561b0179-027c-4209-9701-ec18cee321d9`
- source event：
  `event:9ecad016-8df4-454a-9d0a-ac88510f1346`
- model request：
  `d1792efe-c9c3-4979-9765-3353de98dc39`
- 终态/reason：
  `shadow_complete / proposal_recorded_no_execution`
- attempt：1 次，`deepseek-v4-pro` 成功
- usage：1,200 input、162 output、1,362 total tokens；4,690 input bytes、
  1,183 output bytes、3,197 ms、663 USD microunits
- proposal：0
- tool invocation：0
- delivery intent：0

运行前后的工具 invocation、RenderDocument 和 delivery intent 计数没有因该 run
增加；Core 日志只有 AgentRun 路径，没有 Renderer 或 delivery 路径。随后 Core
先恢复 `off`，再准备 5b。

## 3. Phase 5b exact Wolfram canary

Reviewer 经 control login、preview 与 CAS 把 `wolfram.run@1.1.0` 激活为
`active/rv2`。Provider 精确报告该 descriptor，健康、并发 0/1，且硬执行
wall-time、memory、input-bytes 与 output-bytes 四类预算。

rollout plan 为：

- plan：`phase5-wolfram-agent-20260730@1.0.0`
- plan hash：
  `15b9d3281c093e0affb60e65e95ddc5065cb864783d0ef2a6dd45e25fa59e740`
- conversation：`system:system:phase5-acceptance`
- caller/tool/provider：
  `agent / wolfram.run@1.1.0 / provider-wolfram-primary`
- window：2026-07-30 12:45–15:30 CST
- ceiling/rollback：1 次 / `ledger_only`
- activation mutation：
  `8a70328c-c9b1-4cf1-9571-a374a5ef50e9`

2026-07-30 12:55 CST，专用驱动在执行前确认唯一 active plan、0/1、descriptor exact
且 eligible、natural-language caller 与 lease 均已开启，然后执行唯一一次 canary。
生产证据为：

- AgentRun：
  `e4653010-281a-4933-a602-2830bb66706a`
- model request：
  `52c7b0aa-bd7b-449a-aef4-373cf12239a0`
- proposal：
  `f367410a-17f2-4360-924e-454c75cec25b`
- loop：
  `1124af37-ef96-46f8-8df8-53144aefd18f`
- invocation：
  `710152f4-5490-4e54-80b2-d3a44c3dbf9c`
- Provider attempt：
  `3c8ad94d-a86d-40d1-ae1a-e98a320f2a6f`
- model usage：1,317 input、839 output、2,156 total tokens，其中 cache hit 256、
  cache miss 1,061；5,108 input bytes、4,181 output bytes、14,075 ms、
  1,193 USD microunits

proposal 精确绑定 `wolfram.run@1.1.0` 与 reviewed descriptor hash，validation 为
`valid`。独立 loop 账本按以下序列完成：

```text
tool_pending -> result_ready -> complete
```

invocation 使用 `caller=agent`、`execution_mode=canary`，只由
`provider-wolfram-primary` 取得一次 lease/fence=1，并以 `provider_completed`
进入 `succeeded`。Provider output 只按 hash
`4e711e7d75af6c86e91b31baf85341d321f5687023c7c60abe299dd6201758b7`
记录；loop 将带来源、分级、限长与不可信标记的结果交给一次 continuation，最终
result 为 486 bytes、hash
`66ba1fe7bec137f5ef59eabd5bc12c3be7e145e6291220d18243304cbdabdbf5`。
continuation 没有再次调用工具。

本切片沿用 5a 的不可变 AgentRun：run 自身仍保持
`tool_invocation_count=0`、`delivery_intent_count=0`；proposal 到执行的提升和一次
调用计数记录在独立 `agent_tool_loops`/`tool_invocations` 账本。验收驱动聚合后断言
场景总 tool invocation 为 1、delivery intent 为 0。该 invocation 的 artifact=0、
confirmation=0；内部会话的 `render_delivery_intents` 总数为 0。

计划消费后为 1/1。Operator 随后经正式 preview/CAS 以 authority decrease 暂停：

- pause preview：
  `b2933e24-8b0a-4604-b9bb-b90f16fa0b5a`
- preview hash：
  `d527226575247d92f13977b78e86cdd4e0915efdae700ff56acdc6c6ccc46205`
- pause mutation：
  `32d78431-a891-4813-942b-e5d359e97be9`
- 终态：`paused/rv3`、1/1

控制面登录限速使审计性 pause 比执行总闸回落稍晚；期间 plan 已耗尽 1/1。Core 先
回落 `off + ledger_only`，确认 effective active plan 为 null、lease 关闭且 active
attempt=0，随后才完成正式 pause，没有扩大或重复使用 authority。

## 4. 凭据撤销与最终稳定窗口

最终 Core 于 2026-07-30 12:59:46 CST 以同一镜像重新创建，配置为：

```text
SUPERLILY_AGENT_MODE=off
SUPERLILY_TOOL_EXECUTION_MODE=ledger_only
SUPERLILY_MODEL_PROVIDER_TOKENS_JSON={}
SUPERLILY_CONTROL_OPERATORS_JSON=
SUPERLILY_CONTROL_ALLOWED_HOSTS_JSON=[]
SUPERLILY_CONTROL_ALLOWED_ORIGINS_JSON=[]
SUPERLILY_CONTROL_AUDIT_PEPPER=
```

控制面登录返回 503。一次性 model token、reviewer/operator password 和 audit pepper
文件均已从主机文件系统命名空间删除。已产生的 session、preview、mutation 和 audit
行作为只追加证据保留，但没有配置 operator 可以继续认证。

稳定窗口从最终 Core 启动的 2026-07-30 12:59:46 CST 观察到 13:30:20 CST，
共 30 分 33 秒。窗口跨过 13:02、13:07、13:12、13:17、13:22、13:27 六组完整
300 秒 inventory；Wolfram/LaTeX/status 的最终 snapshot sequence 为
9132/9133/9134，hash 全程不变。最终 heartbeat sequence 为
89451/89452/89453，三者均 `healthy`、并发 0/1。

最终审计结果：

- Core、PostgreSQL、Wolfram/LaTeX/status Provider、Nekro 均 restart=0、
  OOM=false；Core/PostgreSQL/Nekro health 为 healthy，Lily 进程从
  2026-07-23 持续运行；
- Core 与三个执行 Provider 从 13:00 CST 起没有 warning/error/exception；
- Lily/Nekro spool 均 pending=0、quarantine=0、`last_error` 为空；Core watermark
  分别为 158,729 与 32,323，均满足 seen=contiguous、gap=null；
- 命令 Registry 为 28 plugins/198 candidates，最终检查时 snapshot age 为
  242 秒；
- active rollout=0、active attempt=0，Agent 账本保持 2 runs/2 attempts/1 loop/
  1 invocation，5b 完成后没有新增 agent invocation，内部探针 delivery 仍为 0；
- control session 中未过期且未撤销的记录为 0。

Nekro 应用日志中仍有与普通会话模型请求、过期 sandbox 清理、既有 renderer 请求和
消息转换相关的 warning/error；它们不含 Phase 5 conversation、run、loop 或
invocation ID。Nekro 在这些日志期间保持 healthy、0 restart/OOM，spool 与 Core
watermark 继续前移，因此不归因于本次内部 canary，也不被写成“全系统零错误”。

## 5. 签署边界

上述稳定窗口与最终审计已完成，Phase 5a/5b 于 2026-07-30 签署。最终生产满足：

- Agent off、工具 `ledger_only`、无 effective active rollout、lease 关闭；
- Phase 5 plan `paused/rv3`、1/1，active attempt=0；
- 三个执行 Provider inventory/heartbeat 健康且无新增不一致；
- Lily/Nekro spool 无 pending/quarantine/gap，确定性命令 Registry 新鲜；
- 探针关联的 Renderer、delivery、QQ/NapCat 发送均为 0。

本次签署没有开放 5c 或 `history.search`。二者至今仍未获授权；如果出现真实产品
需求，按当前路线 R5 重新立项，而不是继续旧 Phase 顺序。
