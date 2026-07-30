# Phase 5 Agent 产品测试群生产签署

本文记录 `0024_agent_product_flow` 于 2026-07-30 在长期测试群 `708309706`
的生产发布、真实发送、故障与回滚证据。它是此前无发送
`PHASE5_PRODUCTION_ACCEPTANCE.md` 的增量签署，不取代 5a/5b 的 authority 证据。

签署范围严格只有：

- Nekro instance `nekro-agent` 的明确 mention/reply 入口；
- Core conversation `qq:group:708309706`；
- 常驻 `deepseek-v4-pro@1.0.0` 模型 Provider；
- direct answer，或一次 Git-bound `wolfram.run@1.1.0`；
- 一次 Core-owned、Nekro lease/fence 的原生文本发送。

其他群、私聊、非定向普通消息、历史检索、文件、shell、write、群管理、撤回和
5c confirmation authority 均未开放。生产验收使用合成入口事件和真实 QQ 平台发送；
它没有伪造人类 QQ 账号。实际人类 mention/reply 将走同一已部署 bridge 入口，但
不是本文声称已经发生的证据。

## 1. 发布包、备份与迁移

- 实现 commit：
  `fc9d051d309364e28e19d045acb8cd80070595be`
- 分支：
  `codex/phase5-agent-runtime`
- 远端：
  私有 `F1Justin/superlily`，发布前 push 返回 `Everything up-to-date`
- Core image：
  `sha256:2d8db0c15fa733149d94125e122bea6f627188f20e3d2cd5457a652c7d35df23`
- resident DeepSeek Provider image：
  `sha256:e8baa76022ef0ca4740d3ceeebd3385ab02c09be42f1d6dd61de7f782b0fc9f7`
- Nekro image：
  `sha256:4c209098e439345fed92b9870bf9e4d2361650476b6ab177924f11911167e72e`
- bridge：
  `Superlily.core_bridge@1.0.0`
- migration head：
  `0024_agent_product_flow`

发布包全量回归为：

- SQLite：556 passed、4 skipped、1 个既有 warning，59.65 秒；
- 隔离 PostgreSQL 17：560 passed、1 个同类 warning，146.56 秒；
- `alembic heads` 唯一为 `0024_agent_product_flow`；
- Compose Agent profile 静态配置通过。

数据库迁移前 custom-format dump：

- 路径：
  `/home/justin/backups/superlily/20260730-phase5-agent-product/superlily-pre-agent-product-fc9d051.dump`
- 大小/权限：
  277,546,241 bytes，root:root，0600
- SHA-256：
  `867e7b8a86cd2855f0c0dead1bb1e88d48951d39920843b87b30ad59254eb896`

该 dump 已完整恢复进隔离 PostgreSQL 17，恢复库确认 revision
`0023_agent_model_routes`、source event 554,338、AgentRun 2、active plan 0；
恢复命令以 0 退出。一次性恢复容器与生产容器内临时 dump 随后删除，主机备份保留。

生产先用 `Agent off + product off + ledger_only` 创建新 Core，再从 0023 迁移到
0024；`alembic check` 无待生成操作，四张 0024 表初始均为 0。PostgreSQL 没有重启。

部署 bridge 前还保存：

- 路径：
  `/home/justin/backups/superlily/20260730-phase5-agent-product/nekro-superlily-bridge-pre-1.0.0.tgz`
- 大小/权限：
  141,349 bytes，root:root，0600
- SHA-256：
  `91d605f4297699949990fa98688a31721f8a199e2ee9e0dbedf42e22c7ba7bd4`

它包含旧 bridge 源码和原配置，可精确恢复；spool 数据库未被替换或删除。

## 2. 凭据、入口与费用硬边界

三份 Agent secret 各自独立，不进入 Git、日志或 shell argv：

- `model-provider.token`
- `provider-trigger.token`
- `deepseek-api-key.token`

Compose 容器以 gid 65532 只读挂载这些文件，因此主机终态是 owner uid 1000、
container gid 65532、mode 0640。首次按 0600 启动时 Core 正确地因
`PermissionError` fail closed；修正为 0640 后又重新创建干净容器，最终
restart=0。这里的 0640 不是放宽给任意用户，而是只增加目标容器组的读取位。

DeepSeek API key 只从现有 Nekro `deepseek` 模型组复制到 resident Provider secret；
Core 不持有它。Model Provider 只持有模型身份 token，触发接口只接收 run/loop ID；
它没有 Core admin、执行 Provider 或 QQ authority。

精确运行配置为：

```text
SUPERLILY_AGENT_MODE=bounded_readonly
SUPERLILY_AGENT_PRODUCT_MODE=canary
SUPERLILY_AGENT_CANARY_CONVERSATIONS_JSON=["qq:group:708309706"]
SUPERLILY_AGENT_ENTRY_INSTANCES_JSON=["nekro-agent"]
SUPERLILY_AGENT_MODEL_PROVIDER_ID=deepseek-v4-pro
SUPERLILY_AGENT_MODEL_PROFILE_VERSION=1.0.0
SUPERLILY_TOOL_EXECUTION_MODE=canary
AGENT_ENABLED=true
AGENT_CANARY_CHAT_KEYS=onebot_v11-group_708309706
```

Core admission 仍是同群同时 1 个、60 秒 4 个、UTC 日 48 个；每 run 预算
0.10 USD，因此每日模型费用保守上界 4.80 USD。Registry eligible 摘要只暴露
`wolfram.run@1.1.0`；status、latex、history、文件、shell 与写工具没有被借机加入。

Git-bound rollout：

- plan：
  `phase5-agent-product-wolfram-708309706-20260730@1.0.0`
- plan hash：
  `d31ff37b38341636281e05e5607ee46b8ae041d72d639c60c7cdaca71f85c27f`
- conversation/caller：
  `qq:group:708309706 / agent`
- tool/provider：
  `wolfram.run@1.1.0 / provider-wolfram-primary`
- ceiling/rollback：
  8 次 / `ledger_only`
- window：
  2026-07-30 18:00 CST 至 2026-07-31 02:00 CST

它经 login、fresh reauthentication、preview 和 CAS 从 reviewed 激活；没有直接改库。

## 3. 真实 direct、幂等与 Wolfram 发送

先提交一条 `is_tome=false` 的非定向探针。Core 返回
`not_explicitly_addressed`，该 source 的 interaction 数为 0，也没有群回复。

### 3.1 Direct answer

- interaction：
  `61a3c8e0-7561-48a1-a815-c606b8b39f1b`
- AgentRun：
  `996aaf83-e5e8-4799-8ff0-0bf58918f808`
- attempt/model request：
  `9e9f1840-e4de-4697-9f5b-26aa3cb8cbed /
  fb0d4449-c645-4344-a9c0-3fa92ec1b130`
- usage：
  1,863 input、115 output、1,978 total tokens，802 USD microunits，
  2,671 ms
- tool proposal/invocation：
  0 / 0
- delivery：
  `a68bca37-3b96-42c2-9d6d-0d02925ec33c`，fence=1，succeeded
- QQ platform message：
  `64640418`

Nekro `message_sent` 证明确实发到 group `708309706`。随后重放完全相同的 source
event，Core 返回 `duplicate=true`；interaction=1、delivery=1、平台 message ID
仍只有一个，没有第二次模型调用或群发送。

### 3.2 Wolfram tool loop

- interaction：
  `473d1fe4-b97b-4acc-8071-64d29fd87de9`
- AgentRun：
  `f4b55f44-8d45-4db5-aca1-853fec603f8d`
- initial attempt/model request：
  `8063b1f6-8d04-4e11-8012-eb93cd425a1a /
  75acaa55-781f-45f5-a630-25cc7c9e4c0f`
- initial usage：
  1,895 input、459 output、2,354 total tokens，1,114 USD microunits，
  7,380 ms
- proposal：
  `b3a430b1-6a2a-479f-be0a-c03ec9a6db28`，valid，
  `wolfram.run@1.1.0`
- loop/invocation：
  `3e92b7f1-d4fe-4e70-aab7-0ca82074d60f /
  a644514f-47bd-4550-b14d-9cfac75998be`
- tool attempt：
  `0053faa5-5484-40f6-9abe-0b0dcf79c0a3`，fence=1，
  `provider-wolfram-primary`，succeeded，442 ms，39 input bytes、
  26 output bytes、0 artifact bytes
- continuation：
  `81e1baba-512b-4177-857c-89f16aff6996`，attempt 2
- continuation usage：
  2,083 input、274 output、2,357 total tokens，373 USD microunits，
  5,235 ms
- delivery：
  `9865298c-5fbc-494c-834c-edb02da27858`，fence=1，succeeded
- QQ platform message：
  `1144589780`

工具结果被标为有来源、限长的不可信输入，只进入一次 continuation；没有第二次工具
调用。群中真实结果为积分 9。rollout counter 精确从 0 变为 1/8。

direct、Wolfram 两轮和故障恢复探针的模型费用合计 3,103 USD microunits
（0.003103 USD），远低于单 run 与每日硬上限。

## 4. Provider 故障与正式 rollback drill

仅停止 `deploy-deepseek-model-provider-1` 后提交 recovery probe：

- Core 始终 healthy、restart=0、OOM=false；
- interaction
  `3f98929c-bda5-4cf5-a95e-da3bf8b10cfb`
  停在 `planning/planner_context_ready`；
- 停机期间 attempt=0、tool loop=0、delivery=0；
- Core、Nekro、PostgreSQL 和 Wolfram 均未重启。

恢复同一 Provider 后，同一 interaction 自动继续：

- AgentRun：
  `1ec890d5-907a-4608-82c0-6fbbb1069c68`
- attempt/model request：
  `dfde9ef7-ca9a-4ef6-827f-395c55b39a6a /
  f2f26a52-52f1-40c4-a495-c606f09c33a7`
- usage：
  1,927 input、98 output、2,025 total tokens，814 USD microunits，
  2,259 ms
- tool loop：
  0
- delivery/platform message：
  `42a28c80-90ed-4bb9-ac61-ed096d461a7a / 357479962`

终态仍恰好 1 attempt、0 tool、1 delivery；没有停机期间越权或恢复后重复发送。

Wolfram rollback 使用正式控制面：

```text
active/rv2, 1/8
  -> paused/rv3, 1/8
  -> active/rv4, 1/8
```

pause preview `266ca76b-0b7b-45b2-a1d9-68c85f52a6c5`，hash
`01b171409d96bd16e1ec007ff7c84a7c69a39965e3c050f6cab30016fb908122`，
mutation `fd1ed3c5-511b-438d-8805-1f72b475ebc8`；pause 后 active exact plan=0、
未终态 invocation=0。

恢复 preview `48caaa5e-9dde-4451-8620-b55eff409221`，hash
`db8401a291467c93558912005bc715d9fda2a8304d320ac56c6c400f43b6db68`，
mutation `dddd61ef-f1e2-4639-8cb8-20fac7da3ca5`。两个 preview 都
`allowed=true, blockers=[]`，两个 session 都在 apply 后 logout。

首次临时控制客户端因 HTTP header 大小写处理错误，在 login 后、mutation 前退出；
plan 当时仍为 active/rv2 且 counter 1/8。该 session 最多 15 分钟过期，且最终将
移除临时 operator；没有直接数据库写入被当作替代方案。

发送成功但 completion 丢失的 `ambiguous` 不重试、非法 schema、禁用工具、prompt/
tool-result injection、预算耗尽、重复等价调用与 fence 失效由确定性合同测试覆盖。
本次生产没有故意制造可能重复骚扰群聊的 completion 丢失。

## 5. 稳定窗口与终态

稳定窗口从 bridge 1.0.0 完成启动的 2026-07-30 18:53:54 CST 开始。最终快照将在
至少 30 分钟且跨过完整 300 秒 inventory 周期后写入本节，再提交本签署。

当前验收后的业务账本为：

- 3 interactions，全部 succeeded；
- 14 interaction events；
- 3 text deliveries，全部 succeeded；
- 9 delivery events；
- active AgentRun=0；
- active tool invocation=0；
- exact rollout active/rv4、1/8。

Nekro spool 为 committed 3,207、pending 0、quarantine file 0、`last_error` 为空；
部署没有清空或重建 spool。三个执行 Provider 最新 heartbeat 均 healthy、并发 0/1；
Wolfram inventory hash 仍为
`f91ae5fbae7501febea9353748cb296901aebd0fb94f66776c4b0400b2cdf6af`。

发布后尝试按文件补跑聚焦 pytest 时，当前主机测试进程在第一个异步测试的 event-loop
wait 点无断言、无子进程地停滞，已用 Ctrl-C 终止；该次运行不计为通过。签署依赖的
代码回归仍是发布 commit 前完整通过的 SQLite/PostgreSQL 两套全量结果，生产运行态
另由本文 direct、Wolfram、幂等、Provider 故障与正式 rollback 证据覆盖。

## 6. 签署边界

完成本节最终稳定快照后，`0024_agent_product_flow` 只对
`qq:group:708309706` 签署。它不是全群 rollout，也不代表：

- 模型获得命令、admin、QQ 或数据库 authority；
- Nekro 的全部命令目录迁入 Core；
- status 成为 Agent 产品门槛；
- history/search、记忆或检索提前进入 Phase 5；
- 5c principal、confirmation 或写工具已经完成。

下一步只能在这一切片的真实使用数据上调优 persona、eligible 工具摘要与费用，
或另开 5c principal/authorization 工作包；不得用“测试群已签署”推导其他群或写操作
默认开放。
