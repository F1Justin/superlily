超极莉莉 Lily Harness 技术路线与阶段规划

1. 简要愿景

超究极莉莉不是一个单纯的 QQ bot，也不是把 NoneBot、Nekro Agent、Wolfram、LaTeX、词云、抽奖等插件简单堆在一起。它的长期目标是成为一个面向社群环境的 agent harness，也就是一个能够接收群聊、私聊、网页、线下设备、OBS、Fumo、皮套等多种入口事件，并通过统一核心进行理解、调度、工具调用、权限控制、渲染输出、记忆检索、灾备告警和多平台响应的社群智能运行时。

从用户视角看，莉莉既可以被命令精确调用，也可以通过自然语言理解需求。用户可以直接输入 /wf Limit[Sin[x]/x,x->0]，也可以说“莉莉，帮我求一下 sinx/x 在 0 的极限并排成公式图”。系统应当能够把自然语言转换成受控工具调用，调用 Wolfram Engine、LaTeX Renderer、Markdown Renderer、历史搜索、状态查询、活动系统等工具，然后根据平台能力返回文本、图片、公式图、语音、字幕或 OBS 画面。

从系统视角看，莉莉应当逐渐从“插件集合”变成“统一编排系统”。现有 Lily Bot 负责命令和确定性工具，Nekro Agent 负责自然语言聊天，未来的 Watchdog 账号负责健康检查、风控下线告警和灾备降级。长期还可以接入 Telegram、Web Admin、微信 Claw、Fumo 实体终端、Neuro-Lily-sama 皮套和活动现场系统。所有这些前端都不应拥有独立大脑，而应当作为 Lily Core 的 adapter 或 avatar，共享同一套工具、权限、状态、记忆和审计。

2. 当前基础

当前系统已经存在两套独立 QQ bot。Lily Bot 基于 NoneBot2、FastAPI Driver 和 OneBot V11，当前接入 QQ 3643287298（历史记录中仍包含旧账号 985393579），经 NapCat 提供 QQ 协议服务。它目前主要承担命令式工具箱功能，包括 LaTeX 公式渲染、Wolfram Engine 计算、东方运势、语音触发、梗图检测、词云、服务器状态等能力。Nekro Agent 独立运行于 Docker Compose，接入 QQ 2022692714，主要承担自然语言聊天、模型路由、沙箱代码执行、Qdrant 记忆和插件式 AI 能力。

目前 Lily 侧整体更接近确定性命令系统，Nekro 侧整体更接近 AI agent 系统。两者仍然是不同 QQ 号、数据库、配置和代码架构下的独立运行时，但已经不再是互相不可见的两个孤岛：两个 bridge 会把事件、回复、心跳、平台能力和运行时命令清单上报 Lily Core，Core 负责 canonical correlation、确定性裁决、claim/ACK 协调和结果审计。bridge 上报与 claim 异常仍然 fail-open，不让 Core 故障阻断原有 bot；生产 claim 强制范围仍只限精确 allowlist，而不是全面接管两个运行时。

截至 2026-07-19，Phase 1、Phase 2、C0-D 与 Phase 3a 已完成生产签署。Phase 3b 的 invocation/attempt 账本、Provider 拉取协议、硬边界 `status.inspect@1.0.2`、M0–M3 控制面和 Git-bound rollout plan 已完成双数据库回归与生产部署。13 份精确计划每份只允许 1 次 `admin_api + qq:group:1080353942 + status.inspect@1.0.2 + provider-status-primary` 调用；生产已经证明四个独立 stop、成功 canary、safe retry、旧 fence、非法输出、时钟偏移、三种取消路径以及 Core/PostgreSQL 中断。所有计划均暂停并耗尽，生产恢复 `ledger_only`、无 active plan/lease；`status.inspect@1.0.2` 的修正版 Provider 已跨过稳定窗口。自然语言调用权、`enforce` 和平台发送能力仍未开放。

目前 Nekro Agent 虽然有记忆、情感、向量库等插件，但实际使用中效果不稳定，并且大量增加上下文成本。因此当前自然语言回复主要依靠 system prompt 和最近 32 条上下文。这一形态虽然 stateless，但在群聊环境中反而具有稳定、便宜、低污染、不翻旧账的优势。未来记忆系统不应恢复为默认注入式 RAG，而应当采用“memory as tool, not context”的方式，默认不检索、不注入，需要时再由 agent 主动调用历史、文档、状态或记忆工具。

3. 总体技术原则

第一，核心自研，边缘复用。Lily Core / Lily Harness 应当自研，因为现有 NoneBot 和 Nekro 的抽象层级无法完整覆盖多账号协同、统一工具注册、自然语言 tool calling、灾备、跨平台、Fumo 和皮套等长期目标。但外围能力不应立即推翻，NapCat、OneBot、NoneBot 插件、Nekro、Wolfram Engine、LaTeX、PostgreSQL、Qdrant、Playwright 都可以继续复用，逐步收归核心调度。

第二，先做模型和协议，再做功能迁移。系统长期资产不是某个命令插件，而是统一事件模型、响应模型、工具协议、权限模型、平台能力模型和审计模型。只要这些对象稳定，QQ、Telegram、Web、微信、Fumo、皮套都只是 adapter；Wolfram、LaTeX、词云、历史搜索、活动系统都只是 tool。

第三，默认轻上下文，按需工具调用。莉莉不应当每次对话都 RAG，不应当每次都塞长期记忆。普通对话默认使用 system prompt 和短上下文。用户明确提到“上次”“之前”“继续”“群里谁说过”“状态怎么样”“活动到哪了”等需求时，agent 再调用 history.search、docs.search、state.get、memory.lookup 等工具。检索是工具，不是默认上下文。

第四，模型可以申请工具调用，但执行权属于 harness。自然语言层可以判断意图、生成 tool call、解释结果，但所有工具调用必须经过 schema 校验、权限校验、频率限制、资源预算、超时控制和审计记录。群管、配置修改、跨平台转发、发公告、重启服务等写操作必须走更严格的确认机制。

第五，多平台从第一天设计，但不必第一天实现。QQ 仍然是主战场，Telegram 可以作为管理员告警和远程控制入口，Web Admin 用于调试和运维，微信 Claw、Discord、B 站直播、Fumo、皮套等可以作为后续 adapter。核心里不应写死 QQ 的 group_id、user_id 或 OneBot message segment，而应统一为 platform、conversation、identity、message、attachment 和 response。

4. 总体架构

长期架构可以分为五层。第一层是 Platform Adapter，负责接入 QQ / OneBot / NapCat、Telegram、Web、微信 Claw、Discord、B 站直播、Fumo、OBS、Live2D 等外部入口。第二层是 Lily Event Model，负责把各种平台事件统一成标准事件。第三层是 Lily Core，负责事件路由、claim lock、权限、工具注册、状态、审计、缓存、健康检查和灾备。第四层是 Agent Runtime，负责自然语言理解、tool calling loop、上下文构造、结果解释和响应组合。第五层是 Tool Host 和 Renderer，负责 Wolfram、LaTeX、Markdown、代码高亮、历史搜索、活动系统、词云、状态图等能力。

一个简化结构如下。

Platform Adapters
├─ QQ / OneBot / NapCat
├─ Telegram
├─ Web
├─ WeChat / Claw
├─ Fumo
└─ OBS / Live2D / Neuro-Lily-sama
        ↓
Lily Event Model
├─ MessageEvent
├─ CommandEvent
├─ MentionEvent
├─ ImageEvent
├─ MemberEvent
├─ AdminEvent
└─ HealthEvent
        ↓
Lily Core
├─ event router
├─ claim lock
├─ permission gate
├─ tool registry
├─ renderer dispatcher
├─ state manager
├─ audit log
├─ rate limit
└─ watchdog manager
        ↓
Agent Runtime
├─ intent parser
├─ tool planner
├─ tool calling loop
├─ context builder
├─ response composer
└─ result explainer
        ↓
Tools / Renderers
├─ wolfram.run
├─ latex.render
├─ markdown.render_image
├─ history.search
├─ docs.search
├─ status.inspect
├─ fortune.draw
├─ wordcloud.generate
├─ event_state.get
└─ qq.admin

5. 推荐技术栈

当前 Lily Core 已采用 Python、FastAPI、asyncio、SQLAlchemy/Alembic 和 PostgreSQL。Python 能最大程度复用现有 NoneBot 插件、Wolfram 调用、LaTeX 渲染、Playwright 和 Nekro 周边生态；FastAPI 提供核心 API；PostgreSQL 负责事件、引用、裁决、claim/ACK、响应、实例状态和审计。Redis 没有成为 Phase 1/2 的 correctness 依赖：当前 correlation 与 claim 由 PostgreSQL 事务、唯一约束和 advisory lock 保证，只有 Phase 3 后续的分布式 rate limit、lease 或 queue 出现经验证的职责时才考虑引入 Redis。

向量数据库不应作为第一阶段核心依赖。未来如果需要记忆和历史语义检索，可以优先考虑 PostgreSQL 全文搜索、BM25、pgvector 或继续复用现有 Qdrant。第一阶段重点不是“让莉莉记住一切”，而是让它能够统一接入事件、记录状态、抽象工具和控制响应。

仓库已经采用 monorepo，当前结构是：

superlily/
  apps/core/                 # Core API、模型、裁决、存储与迁移
  packages/contracts/       # 跨 Core/bridge 的版本化契约与共享向量
  bridges/lily_nonebot/     # Lily / NoneBot bridge
  bridges/nekro/            # Nekro bridge
  deploy/                   # Compose、约束与集成配置
  docs/
    adr/                    # 已接受的架构决策
  tests/                    # SQLite/PostgreSQL 与契约回归测试

6. 第一阶段详细规划：Lily Core MVP（已完成基线）

第一阶段的原始目标不是重写 NoneBot，也不是替换 Nekro，而是建立一个独立的 Lily Core MVP，使它能够接收、记录和观察现有系统事件，并为后续统一调度打基础。这个旁路观察基线已经完成；现有 Lily Bot 和 Nekro Agent 仍按各自逻辑运行，但它们的消息、回复、心跳和运行时能力已经进入 Lily Core 的统一视图。以下各节保留第一阶段的设计范围与取舍，当前 HTTP 和数据契约以 `docs/CONTRACTS.md` 为准。

6.1 阶段目标

第一阶段应当完成五件事。第一，建立独立 lily-core 服务，提供 HTTP API。第二，定义最小统一事件模型和响应模型。第三，让 NoneBot Lily 侧将收到的 QQ 消息上报到 Core。第四，让 Nekro Agent 或其适配层将自然语言消息和回复结果上报到 Core。第五，实现基础健康检查和实例心跳，为后续 Watchdog 做准备。

第一阶段不要求 Core 接管回复，不要求实现自然语言 tool calling，不要求迁移现有插件，不要求 Web Admin 完整可用，也不要求多平台。此阶段主要是“旁路观察”和“统一日志”。

6.2 最小功能范围

第一阶段初稿列出的 API 已收敛为带版本、认证和幂等边界的实际接口。

POST /v1/events
接收标准化事件，包括消息事件、命令事件、健康事件和系统事件。
POST /v1/responses
记录某个 bot 实例对某个事件发出的响应，包括文本、图片、错误和耗时。
POST /v1/heartbeats
记录 bot 实例心跳，包括实例名、平台、账号、进程状态、连接状态和时间戳。
GET /health/live 与 GET /health/ready
分别验证进程存活和 PostgreSQL readiness；实例状态不混入进程存活判断。
GET /v1/events/recent
查看最近事件，供调试使用。
GET /v1/instances
查看当前所有 bot 实例及最近心跳。

写接口使用实例绑定 bearer token，事件与响应写入带 `Idempotency-Key`；管理读接口使用独立 admin bearer token。Phase 2 另增加了 references、decisions、claims/ACK、command-registry snapshots、native identity、capability 和 outcome 审计接口。Provider 与 Tool Registry 的身份和接口属于 Phase 3，不能复用 bot ingest 或 admin token。

第一阶段可以不实现复杂权限和 claim lock，但数据库模型应当预留相关字段，避免后续迁移困难。消息事件至少要能记录平台、适配器、bot 身份、会话、发送者、消息 ID、文本、附件摘要、原始 payload 的安全截断版本和时间戳。

6.3 最小数据模型

第一阶段至少需要以下表或等价 ORM 模型。

bot_instances
记录 Lily Command、Lily Talk、Lily Watchdog、Nekro Agent 等实例。字段包括 instance_id、platform、adapter、bot_id、role、status、last_heartbeat_at、metadata。
events
记录进入 Core 的标准化事件。字段包括 event_id、platform、adapter、bot_id、conversation_id、conversation_type、sender_id、sender_name、event_type、message_id、text、segments_json、raw_json、created_at。
responses
记录 bot 对事件的响应。字段包括 response_id、event_id、instance_id、response_type、text、attachments_json、success、error、latency_ms、created_at。
health_checks
记录服务健康检查结果。字段包括 check_id、target、status、detail_json、created_at。
conversation_configs
预留群或会话配置。第一阶段可以只存 conversation_id、platform、name、enabled、metadata。
identity_mappings
预留跨平台身份映射。第一阶段可以只存 platform、external_user_id、display_name、person_id、metadata。

当前实现没有为了满足初稿名称而创建空的 `health_checks`、
`conversation_configs` 和 `identity_mappings` 表。健康证据由 readiness、
`bot_instances` 与 append-only `instance_status_transitions` 等价承担；当前
canary 配置仍是启动时环境配置。会话策略与跨平台身份映射必须等 principal、
权限和第二平台合同定型后再建，不能让昵称或未经验证的平台角色提前成为权限
依据。这个取舍不阻塞只读/公开工具的 Phase 3a/3b，但任何管理员写工具、跨平台
转发或第二平台上线前必须补齐正式模型与迁移。

6.4 统一事件草案

第一阶段的统一事件模型不必完美，但必须避免 QQ 特化。建议采用如下结构。

{
  "event_id": "qq:onebot:985393579:group:123456:msg:abcdef",
  "type": "message",
  "platform": "qq",
  "adapter": "onebot_v11",
  "bot": {
    "id": "985393579",
    "role": "command"
  },
  "conversation": {
    "id": "123456",
    "type": "group",
    "name": "东方济悠樱"
  },
  "sender": {
    "id": "2843657817",
    "name": "F1 Justin",
    "roles": ["owner"]
  },
  "message": {
    "id": "abcdef",
    "text": "wf Limit[Sin[x]/x,x->0]",
    "segments": [],
    "attachments": []
  },
  "raw": {},
  "timestamp": "2026-06-19T22:00:00+08:00"
}

这里的 raw 字段只用于调试和兼容，不应当成为业务逻辑依赖。后续所有路由、权限和工具调用都应优先使用标准字段。

6.5 NoneBot 接入方式

第一阶段建议在现有 NoneBot 项目中新增一个轻量插件，例如 lily_core_bridge。这个插件优先级较高，但不阻断现有 matcher。它只负责把收到的事件转成 LilyEvent 并 POST 到 lily-core。如果上报失败，只记录日志，不影响原有 bot 功能。

这个插件不应当在第一阶段改变 /wf、/tex、/fortune 等命令行为。它只是旁路观察器。后续第二阶段和第三阶段再逐步引入 Core 裁决和工具注册。

6.6 Nekro 接入方式

Nekro Agent 当前可以继续作为自然语言聊天后端。第一阶段只需要把 Nekro 收到的消息和最终回复记录到 Core。如果直接改 Nekro 成本较高，可以先通过外部日志、HTTP hook、反向代理或 adapter 层进行上报。目标不是控制 Nekro，而是让 Core 看到自然语言侧发生了什么。

如果 Nekro 的内部插件改造成本较低，也可以增加一个简单的 hook：收到消息时上报 event，发送回复后上报 response，模型调用失败时上报 error。这样后续可以分析自然语言响应质量、成本、上下文长度和失败原因。

6.7 Heartbeat 与健康检查

第一阶段应当实现最小心跳机制。NoneBot Lily、Nekro Agent、NapCat 实例和未来 Watchdog 都应定时向 Core 上报状态。心跳内容可以包括实例名、进程状态、连接状态、当前账号、最后一条消息时间、错误摘要和版本信息。

建议心跳间隔为 30 秒。Core 如果超过 90 秒没有收到某实例心跳，可以将其实例状态标记为 degraded 或 offline。第一阶段不需要自动告警，但应当在 /health 中体现异常，为后续 Watchdog 打基础。

初稿建议的 Redis 没有成为 Phase 1/2 依赖：当前规模下，PostgreSQL 事务、唯一
约束和 advisory lock 已覆盖 correlation 与 claim；短期队列由 bridge 的有界
内存队列承担。Redis 只有在 Phase 3 的分布式 rate limit/lease/queue 证明需要时
才引入，不能因为早期技术栈清单而增加一个尚无 correctness 职责的服务。

6.8 第一阶段完成标准

第一阶段完成时，应当能够做到以下几点。第一，启动独立 lily-core 服务后，可以看到 NoneBot 和 Nekro 的心跳。第二，QQ 群内发送任意消息后，Core 的 /events/recent 可以看到标准化事件。第三，现有 Lily Bot 的命令功能不受影响。第四，Nekro 的自然语言回复仍按原逻辑运行，但 Core 能记录其响应。第五，Core 可以输出一个简单健康状态，说明哪些实例在线、哪些实例最近异常。

第一阶段的成功标志不是功能变多，而是系统从“两个孤岛 bot”开始变成“一个核心能观察所有前端”。只有这一层稳定，后续 Tool Registry、自然语言路由、三账号协同和 Watchdog 才有可靠基础。

7. 第二阶段：统一事件模型与核心裁决

第二阶段目标是从“旁路观察”进入“核心裁决”。每条消息进入后先由 Lily Core 判断是否应由某个实例响应、是否应忽略、是否应转给命令工具、是否应转给自然语言 agent。此阶段需要引入 claim lock，避免 Command 号、Talk 号和 Watchdog 号抢答同一条消息。

第二阶段还应完善平台能力模型。QQ、Telegram、Web、Fumo、皮套等平台支持的发送文本、发送图片、回复、撤回、禁言、Markdown、按钮、文件上传等能力不同，Core 应当通过 capability 自动降级输出。虽然此阶段仍然可以只实现 QQ，但抽象上应当为多平台做好准备。

7.1 Phase 2a：事件引用关系与历史导入基础

在正式进入 claim lock 和响应裁决前，应先补齐事件图谱的基础。Phase 2a 的目标是让 Core 不只知道“发生了哪些消息”，还要知道“这些消息之间如何互相引用”。这包括 QQ reply、quote、forward、mentions 等关系的标准化入口；第一步可以只实现 reply_to，其他关系预留枚举和数据结构。

引用关系不应长期藏在 raw payload 里，而应成为 Core 的一等数据。事件上报可以携带 references 数组，Core 将其写入 event_links 表。每条 link 同时保留标准字段、原始线索和解析状态：如果能根据同实例、同会话、平台本地 message_id 找到目标 observation，则解析到 canonical source_event；如果暂时找不到，则保留 unresolved link，后续由 backfill/resolver 在旧数据导入或目标消息迟到后补连。

历史导入也应在此阶段打基础，但不应直接粗暴合并旧库。Lily 与 Nekro 旧记录应作为 observation 导入，保留 original_source、original_pk、原始 message_id、segments 和安全截断 raw。canonical source_event 仍由 Core 的相关性规则生成或复用；相关性规则必须优先使用平台 message_id 或更强的原生序列，不能只靠短时间窗口内的 sender+text 合并。Phase 2a 先实现 dry-run importer/解析报告，再决定是否执行真实导入。

Phase 2a 的完成标准是：新消息的 reply 引用可以被记录；能解析的引用连到 canonical source_event；不能解析的引用以 unresolved 状态留存；recent/debug API 能看到关系线索；测试覆盖同账号 reply、跨账号 canonical event 引用、未解析引用和旧数据导入 dry-run 的基础路径。

7.1.1 Phase 2a.1：跨账号原生消息身份验证

Lily 与 Nekro 通过两个 QQ 账号观察同一条消息时，NapCat 下发的 OneBot `message_id` 可能不同，因此在进入真实 claim lock 前必须先验证更强的原生身份线索。两个 bridge 应只采集内容无关的白名单字段，例如 `message_id`、`message_seq`、`real_id`、`real_seq`、平台时间、会话 ID、发送者 ID 和消息类型；不得为了关联而保存整份 NapCat payload、附件 URL 或额外消息正文。

Phase 2a.1 只负责采集和审计，不立即修改 canonical correlation。Core 应提供 recent/debug 视图，能够对照 Lily 与 Nekro 对同一可见消息记录的原生字段。如果实测证明 `real_seq` 或其他组合键跨账号稳定，再设计 correlation v3；如果不存在稳定强键，则保留独立 source_event，并使用显式的疑似同源关系或权威入口策略，不能退回 sender+text+短时间窗口的强制合并。

Phase 2a.1 的完成标准是：两个 bridge 均能上报 `metadata.native_identity`；Core 能展示字段来源和覆盖情况；受控样本能判断 `real_seq` 是否跨账号一致；采集失败保持 fail-open；在验证结论形成前，现有 correlation v2 和 bot 回复行为完全不变。

7.1.2 Phase 2a.2：Canonical Correlation v3 与确定性裁决输入

Phase 2a.1 的线上验证确认，NapCat `real_seq` 在同一 QQ 群会话内可以稳定标识 Lily 与 Nekro 共同观察到的同一条消息，而 `message_id`、`message_seq`、`real_id` 均为账号局部值。Correlation v3 应仅对已验证的群消息使用平台、标准化会话、`real_seq` 和发送者组成强身份键；私聊在获得独立证据前继续保持不关联。平台时间只用于冲突保护，文本和账号局部 ID 不得进入跨账号键。缺少强身份字段或发生冲突时必须 fail-open，保留独立 source event，不能退回 sender、text 和短时间窗口的模糊强制合并。

同一 canonical event 的 shadow decision 不得取决于 Lily 或 Nekro 哪个 observation 先到。Core 应在每次新 observation 或引用解析结果加入后，根据该 source event 的全部 observations、event links 和已知 bot response 重新生成确定性 decision。命令识别优先使用 Lily 的原始观察；引用路由则以被引用消息的发送实例为准，而不是以 QQ 自动附加的 `at` 是否仍然存在为准。

QQ 回复 Nekro 消息时，无论用户保留还是删除 QQ 自动添加的 `at`，都应路由为 `talk / nekro-agent`，允许 Nekro 继续回复。QQ 回复 Lily Command 消息、普通群友消息或无法安全确定目标的引用时，默认只记录为 `observe_only`。引用中的、指向被引用发送者的自动 `at` 只是展示装饰，不是独立触发器；非引用的直接 mention、引用中额外 mention 了不同 bot、明确命令或包含“莉莉”的文本召唤仍按各自规则处理。

Phase 2a.2 的完成标准是：共同消息稳定形成一个 source event、两个 observations 和一个 decision；快速连续发送相同文本但 `real_seq` 不同的消息绝不合并；decision 与 observation 到达顺序无关；回复 Nekro/Lily 且自动 `at` 保留或删除的四种组合均有测试；线上 shadow 验证不再出现跨账号 source event 与 decision 翻倍，并且身份冲突可见、可审计、不会阻塞现有 bot。

7.2 Phase 2b：核心裁决 Shadow Mode

在引入真正 claim lock 之前，Core 应先进入 shadow decision 阶段。此阶段 Core 对每个 canonical event 生成一条 event_decision，记录它认为这条消息应被忽略、交给命令号、交给自然语言号，还是只是潜在工具候选。这个判断只用于审计和调试，不会让 Lily Bot 或 Nekro Agent 改变现有行为。

Phase 2b 的第一版裁决应保持规则化和可解释，不引入 LLM。明确命令如 /wf、/tex、/fortune、/help 归 command / lily-command；@ 机器人、回复机器人、自然语言触发归 talk / nekro-agent；普通群聊默认 observe_only；notice、recall、poke 等非消息事件默认 ignore 或 admin_candidate。每条 decision 必须记录 policy_version、decision_type、target_instance_id、confidence 和 reason，方便后续对照实际响应。

Phase 2b.1 应将命令识别从代码里的少量硬编码前缀扩展为 command registry。registry 记录当前 Lily/NoneBot 运行面里的确定性触发器，包括 prefix、exact text、regex、所属插件、目标实例、权限等级和敏感标记。它仍然只作为 shadow decision 的输入，不代表 Core 已经拥有或执行这些工具。未确认正在加载的插件只能作为候选，不应默认参与裁决。

Command registry 在 2b.1 仍是静态快照，天然存在与 NoneBot 热更新插件树脑裂的风险。后续进入 2b.2/2c 前，应设计受认证的 registry sync 通道，由 Lily bridge 在插件 load/unload 或配置变化时上报候选变更；在此之前 registry 只用于 shadow 审计，不应用于强制拦截。

此阶段还应提供 recent/debug API，使管理员能查看最近消息、引用关系、Core 的 shadow decision、以及实际 responses 之间是否一致。只有当 shadow decision 在真实群聊中足够稳定后，才进入 Phase 2c 的 claim lock 和响应裁决执行。Phase 2b 不接管发送，不阻断任何现有 matcher，也不迁移工具。

7.2.1 Phase 2b.2：运行时命令清单与响应对照

Lily bridge 应从当前已加载的 NoneBot 插件树生成确定性运行时快照，覆盖能够安全内省的 CommandRule、ShellCommandRule、AlconnaRule、fullmatch、startswith、endswith、keyword 和 regex。快照通过现有实例 token 认证，并由 Core 重新计算内容哈希；相同插件树的周期上报只刷新存活时间。运行时候选只证明“这个 matcher 当前存在”，不能自动获得目标实例、权限或敏感级别，后者必须继续由人工审阅的静态 registry 覆盖。若 matcher 还带有无法表示的复合 rule 或 permission，必须标记为不完整而不能假装已完全识别。运行时存在但静态未登记的触发器必须可见，并在 2c 强制路径中导致 abstain。

Core 还应把 canonical decision 与实际 response 自动对照，区分 matched、missed、wrong_instance、failed、unexpected_response 和 pending。Lily 可使用当前事件上下文记录原生触发关系；Nekro 公共 hook 若拿不到原生 trigger，只允许使用一次性的、明确标注为 inference 的会话内 ToMe 关联，不能把后续主动消息长期挂到旧事件上。

Phase 2b.2 的完成标准是：运行时插件树新增、删除或不变都能在有界时间内反映到 Core；坏哈希被拒绝；同哈希刷新不会误判 stale；静态规则能区分已加载、未加载和未覆盖候选；未覆盖候选不参与强制裁决；管理员能直接查看 decision/response 对照结果。

7.3 Phase 2c：Fail-open Claim Lock Canary

2c 不应立即让 Core 执行工具，而只在两个现有 bot 的响应入口前增加短时 claim。claim 以 canonical source event 为单位，在 PostgreSQL 事务与 advisory lock 下记录每个实例的 allow、deny 或 abstain。只有 command/talk 这类明确有目标实例的 decision 可以进入 allow/deny；observe_only 在首轮 canary 中始终 abstain，避免未知被动 matcher 导致消息被吞。

强制 claim 必须同时满足 correlation v3、两个账号 observation、最低置信度、最新运行时 registry、没有未登记的运行时命令命中、引用目标确定、目标实例在线以及精确会话 allowlist。命令还必须是完整内省、公开且非敏感的 matcher；需要群管或超级用户权限的命令在 Core 建立发送者授权模型前一律 abstain。任一条件缺失、Core 超时、网络失败或响应格式异常都必须 abstain 并维持旧行为。Lily 的 deny 只抑制本事件产生的 send API，不停止 chatrecorder 和其他观察 matcher；Nekro 使用保留历史记录的 BLOCK_TRIGGER。

Phase 2c 先运行 shadow claim，再只对一个明确测试群启用 canary。完成标准是：命令只允许 Lily、自然语言召唤或回复 Nekro 只允许 Nekro、回复 Lily 不触发 Nekro、普通消息不被 claim 破坏、Core 停机时两个 bot 自动回到旧行为，并且所有 claim 与被抑制发送均可审计。通过稳定窗口后，第二阶段完成，方可进入 Phase 3 Tool Registry。

截至 2026-07-18，Phase 2 已完成最终签署。最终基线部署 Correlation v3、policy v6、运行时命令清单、response outcome、唯一引用解析、typed platform capability、claim/ACK 协调和长任务 response attribution；policy-v5 稳定窗口、policy-v6 counterfactual、无显式召唤/显式召唤实测、SQLite/PostgreSQL、迁移/漂移、备份和回滚证据均记录在 `docs/ACCEPTANCE.md`。Phase 3 入口已经开放，但签署本身没有启用任何工具执行权。

7.4 C0-D / C0-A：采集可靠性与史料持久化补充

Phase 2 的签署证明了当前消息路由和 claim 基线，但不代表平台事件和复合消息已经完整归档。最新产品要求是在 bot 有权看到且被配置为允许长期留存的会话内，对结构化消息与平台事件“应采尽采”：reaction/贴表情、recall、poke 等动作成为一等 observation；合并转发必须异步展开并保存多层嵌套节点、顺序和完整性状态；无法取得、被限制或截断的内容也必须留下明确缺口。采集事实不预设训练、反馈或自动行为。

图片字节第一版不长期保存，但消息段中的图片位置、平台资源 ID 和可得类型、大小、尺寸、哈希等元数据需要保留。文件、语音、视频等大对象不得直接写入 PostgreSQL；未来如需保存，使用独立的有界内容寻址存储。已知字段规范化入库，未映射字段经过版本化 sanitizer 后有限保存，凭证、会话密钥、本地路径和带授权参数的临时媒体 URL 不进入长期档案。

长期运行 bot 还要求把 Phase 6 设计中的 `HA-0` durable ingress spool 提前：bridge 先把事件原子落到本地持久队列，Core 幂等提交并返回 receipt 后才能清理；Core、网络或 PostgreSQL 故障后可以重放，并公开 watermark、lag、gap 和 quarantine。这个 Phase 3b 前置包命名为 C0-D，只包含 capture profile、durable spool、commit receipt、覆盖诊断、幂等重放、sanitizer 和基础 action 事件。

C0-A 随后负责多层合并转发、`archive_full` 正式启用、离线导出、重建、保留/删除传播和旧历史导入。它在 C0-D 稳定后可以与 Phase 3b 分别推进，不是 invocation ledger 的 correctness 前置条件。数据库之外的版本化导出及恢复重建仍是史料目标，但不能用罕见的嵌套转发极端案例长期阻塞核心工具迁移。

C0-D/C0-A 属于 Phase 1/2 观察基础的完整性回补。Phase 2 已签署结论不回滚，已部署的 Phase 3a 零权限 Registry 也不撤销；实施顺序是在 Phase 3b invocation/lease 之前先完成 C0-D，且不改变命令、Nekro 回复、claim 或工具权限。完整数据模型、此前关于快速回复、模型自主选工具、渐进式披露、Unix 原语、自然语言命令和成本感知模型路由的共识，见 `docs/COLLECTION_AND_AGENT_CONSENSUS.md`。

截至 2026-07-18 21:39 CST，C0-D1 至 C0-D5 已全部部署并签署完成：生产 Core 保持在 `0013_collection_reliability`，Lily 与 Nekro bridge 0.5.0 分别使用独立的 SQLite FULL spool，只有匹配 Core receipt 后才提交清理，并通过 heartbeat 暴露 pending、quota、quarantine、watermark、lag 与 gap。两个 spool 的目录权限为 0700，数据库、WAL 和 SHM 均为 0600；当前序号连续、pending 和 quarantine 均为零。平台/adapter 报告的 `occurred_at`、bridge 原子落盘的 `captured_at`、Core `received_at` 和数据库 `committed_at` 分开保存。对本机 NapCat 历史日志的只读审计确认，多次启动后 1–5 秒内会补投几十至两百余条事件，所以启动日志时间不能被当作可靠的历史消息发生时间，也不能单独用于去重或排序。

bridge 0.5.0 依据本机实际 OneBot/NapCat payload，把 `group_msg_emoji_like`、群/好友撤回和 poke 映射为一等 action observation。reaction 只保存操作者、账号局部目标消息 ID、emoji 和平台 count，并统一标为 `observed_state`，不推断增删意图、正负反馈或训练权重；recall 分开保存 operator 与原消息作者；poke 保存 actor、target 和有界的显示文本/action/effect 编号。拍一拍 jump/image URL 与 QQ 内部 UID 不入库，但其省略路径、原 payload 哈希/大小和 sanitizer 版本会明确保存。缺 actor、target、value 或平台时间时标记 `partial`/`unavailable`，不猜值。Lily/Nekro 使用逐字节一致的规范化实现；21:01:24 的同一条真实 poke 已由两个账号分别落成 observation，带相同 bridge action 身份和各自独立 receipt，随后自然到达的 recall 也已落库；21:14:48 至 21:15:32 又有 14 条自然 reaction 入库，目标消息 ID、操作者、emoji ID 和 count 均被保存且 completeness 为 `complete`。emoji 名称或图片映射属于可回填的展示元数据，不替换平台原始 ID；目标旧消息不在 Core 时保持 `unresolved`，不猜测关联。

C0-D5 在真实生产链路完成了两次有界故障演练。Core 停机窗中，Lily 的 `1508-1522` 与 Nekro 的 `200-201` 先落本地 spool，恢复后 17 条记录全部以原 hash 获得 receipt 并关闭连续 watermark；PostgreSQL 停机时 Core 保持 live、ready 明确返回数据库不可用，Lily 的 `1544-1545` 与 Nekro 的 `204` 同样在数据库恢复后无损重放。故障产生的累计 retry failure 保留为真实证据，当前 `last_error` 为空、pending/quarantine 为零。同期“今日老婆”命令被明确路由到 Lily 并成功返回，“莉莉 这是刘维尔定理吗”被明确路由到 Nekro 并成功返回图片和文字，证明命令与对话路径未因 C0-D 改变。全量 SQLite 与 PostgreSQL 17 测试均为 244 项通过，生产仍为零 descriptor、零 Provider/凭证、零 eligible tool，action 不进入 claim，C0-A `archive_full` 也未启用。详细签署证据见 `docs/C0D_ACCEPTANCE.md`。

8. 第三阶段：Tool Registry 与现有插件迁移

第三阶段目标是把现有命令插件逐步改造成结构化工具。wf 应当成为 wolfram.run，tex 应当成为 latex.render，Markdown 帮助图片应当成为 markdown.render_image，状态图应当成为 status.inspect，历史检索应当成为 history.search。命令入口仍然保留，但它们只是工具的一种调用方式。

每个工具必须声明名称、描述、参数 schema、返回类型、权限要求、超时、频率限制、是否允许自然语言调用、是否需要确认。自然语言 agent 后续只能申请调用已注册工具，不能绕过 Core 直接执行底层操作。

第三阶段进一步拆成四步。3a 只建立经过人工审阅的 tool descriptor 和经过实例认证的 runtime provider snapshot，不执行工具；3b 建立 invocation、attempt、confirmation、lease、fencing token、deadline、budget、artifact 的完整账本和 provider 拉取协议；3c 依次迁移 status.inspect、wolfram.run、latex.render 等低风险工具；3d 才让旧命令入口在 shadow/canary 后切到同一工具协议。运行时发现只证明“实现正在加载”，不能自动获得权限。工具 provider 不运行在 Core API 进程内，也不开放 Lily/Nekro 的入站执行端口，而是从 Core 拉取有界 lease。

第三阶段完成时，命令入口仍然存在，自然语言模型仍然没有工具执行权。详细字段、状态机、数据库表、API、迁移顺序和验收标准见 `docs/PHASE3_TOOL_REGISTRY.md`；跨阶段依赖和门禁见 `docs/ROADMAP.md`。

8.1 当前第三阶段状态（2026-07-19）

十二份 accepted ADR 已固定描述符/JCS authority、Provider 身份与动态状态、invocation/fencing 恢复、artifact 生命周期、控制面认证、descriptor mutation、Provider quarantine、Git-bound rollout plan 与确认/artifact 具体协议边界。Phase 3a 的 `0012_tool_registry`、Git-bound 本机导入、Provider inventory/heartbeat、desired/reported/effective 视图、共享 Provider SDK 和真实 `status.inspect` 报告运行时均已上线。`status.inspect@1.0.0` 仍是未激活的历史 authority，运行时发现没有自动扩大 authority。

2026-07-19，Phase 3b 的 `0014_tool_invocations` 已部署，execution mode 为 `ledger_only`。真实 `status.inspect` 提案只产生 `propose -> record_only`；幂等重放返回原 invocation，Provider 凭据不能创建调用。Lily 与 Nekro bridge 同日升到 0.5.1，心跳和普通/durable-spool reporter 均有监督与自恢复；两个实例线上心跳已恢复新鲜。

同日完成的下一实现切片是 `0015_tool_attempts`、Provider execution SDK、数据库时间 lease/fence/reaper 和独立 `status.inspect@1.0.1` 执行器；canary 前审查又将执行边界修订为不可变 `1.0.2`。四种执行模式、精确范围、三个 stop、并发领取、旧 fence、取消竞态、预算/输出校验、只追加事件和真实子进程端到端路径已在 SQLite 与 PostgreSQL 17 通过测试。子进程不接收 lease secret、Provider token 或平台发送能力；父进程硬性执行 wall-time 和输出字节边界。生产已经完成 `ledger_only` 迁移、hard budget inventory、健康 heartbeat、认证 lease=204 与零 attempt 签署。descriptor 仍为 `reviewed`；ADR 0005 所要求的 mutation 治理门完成前，不得直接改库激活或打开 canary。

最小控制面随后完成 M0 与 M1 两包。M0 的短会话、独立 operator authority、CSRF、精确 Host/Origin/JSON、数据库时间过期、再认证/退出 CAS、登录限速和只追加审计已默认禁用部署。M1 新增持久 canonical preview、descriptor 单调资源版本、reviewer 权限、新鲜再认证、apply CAS、幂等重放/冲突、runtime drift 重算和反向 suspension/restore；数据库直接拒绝 authority 改写、证据删改以及没有匹配 lifecycle event 的状态更新。SQLite 全量 334 项通过、1 项 PostgreSQL 专用测试跳过；PostgreSQL 17 分段全量 335 项通过。生产已经默认禁用迁移到 `0015b_descriptor_mutations`，5 张控制面表为零、两个 descriptor 仍为 `reviewed/resource_version=1`、工具仍是 `ledger_only` 且零 attempt。M2 的实现与上线证据见下一段；这一时点 M3 与真实 canary 尚未签署。

M2 Provider quarantine 随后完成实现与双数据库全量回归。security_admin 通过持久 preview、新鲜再认证、CAS 和幂等证据执行 `active <-> quarantined`；恢复必须重新验证 credential、inventory、heartbeat、协议和 implementation hash。lease 与 quarantine 共用 Provider 行锁，因此接受 quarantine 后不能产生新 lease；quarantined Provider 仍能上报恢复证据。SQLite 341 项通过、2 项 PostgreSQL 专用测试跳过，PostgreSQL 17 合计 343 项通过。同期 canary 前审查把 `status.inspect` 升为不可变 `1.0.2`：创建 worker 时不继承父环境，传输有界，并将无裕量的 256 MiB 申报修正为 320 MiB。M2 与 `1.0.2` 已于 2026-07-19 默认禁用部署：生产为 `0015c` head/no drift，三版 descriptor 均为 reviewed，Provider 为 active/rv1 且精确报告 `1.0.2`，控制面五表和 attempt/event 均为零。

M3 随后用 `0015d_rollout_plans` 和 ADR 0011 关闭环境 scope 旁路。首包只允许 Git-reviewed、最长 24 小时、带调用上限且回退到 `ledger_only` 的精确 canary plan，明确拒绝 `enforce`；operator 可激活/暂停，break-glass 只能暂停。调用创建与 lease 都锁定并重验 plan，非匹配和漂移安全降级；计划暂停接受后不能新增 lease。SQLite 最终全量 370 项通过、4 项 PostgreSQL 专用测试跳过，PostgreSQL 17 全量 374 项通过。M3 已部署到生产 `0015d`，前四份 Git-reviewed 单次计划于 06:25–06:26 CST 完成三个独立停止证明和一次只读成功 canary，第五份于 06:43 直接证明 rollout plan pause。成功路径为 `propose -> queue -> lease -> start -> complete_success`，只有一个 attempt/fence；四条受停止保护的队列均在零 attempt 下按既有契约终止为 `timed_out`。当时首批五份计划均为 `paused/rv3` 且无 active plan/lease，Core 已回到 `ledger_only`。

同日开始的第二批故障矩阵不再重复建设 Provider SDK 或 status 工具，而是补齐异常
收敛：短 lease safe retry 与单调 fence、旧 worker/重复完成拒绝、非法输出、快慢
Provider 时钟、取消确认、取消/完成竞态、取消未确认，以及 Core/PostgreSQL 中断。
这批实现使用正式、脱敏的单场景驱动器和八份 Git-bound 单调用计划；每次仍只允许
`admin_api + qq:group:1080353942 + status.inspect@1.0.2`，不发送 QQ，不扩大模型
authority。完整 SQLite 为 395 项通过、4 项 PostgreSQL 专用场景跳过；PostgreSQL
17.10 为 399 项全部通过。生产 8/8 场景已得到预期终态；空 lease 的 keep-alive
边界噪声也已修正并跨过完整 inventory 稳定周期。下一包转为
`0016_confirm_artifacts`，详细证据见 `docs/PHASE3_FAULT_DRILLS.md`。

`0016_confirm_artifacts` 随后已完成实现与双数据库全量回归。需要确认的调用不在等待
人类时消耗执行 deadline 或 rollout 次数，批准时会重新验证完整 authority；单人
`always/on_write` 可用，缺少强身份边界的 `two_person` 继续 fail closed。Artifact
使用 Provider/attempt/fence 绑定的 reserve、流式 upload、Core 独立 PNG 检查、内容
寻址 finalize 和 complete 精确引用，upload secret 与 lease secret 均不进入审计。
SQLite 为 439 项通过、4 项跳过，PostgreSQL 17 为 443 项通过；默认 root/pepper 为空，
没有新 descriptor、plan、自然语言 caller 或平台发送 authority。生产默认关闭签署后
才进入文本模式 `wolfram.run`。

9. 第四阶段：统一 Renderer

第四阶段目标是建立统一渲染系统。所有长文本、Markdown、LaTeX、Wolfram 图形、代码块、状态卡片、抽奖结果、活动流程、OBS 字幕卡片都应当通过 Renderer 生成标准输出。工具只返回结构化结果，不直接发送 QQ 消息。平台 adapter 根据自身能力发送文本、图片、HTML、Markdown、语音或字幕。

这一阶段会显著提升莉莉的输出质感，也会使 /wf、/tex、自然语言解释、帮助页、活动现场展示和皮套字幕共享同一套渲染能力。

Renderer 必须以版本化 RenderDocument 中间表示和内容寻址 artifact 为核心，记录 MIME、hash、大小、尺寸、来源、TTL 与访问范围。平台 adapter 按 Phase 2 capability snapshot 做显式降级，并记录 Markdown 转图片、图片转文本等降级路径。未受信 HTML/SVG/Markdown、远程资源和本地文件都必须经过独立安全策略。完成标准不是“能画图”，而是工具不再直接调用平台 send API，同一结构化结果可以在 QQ 及第二个平台得到可解释的输出。

10. 第五阶段：自然语言 Tool Calling

第五阶段目标是让自然语言号能够稳定调用受控工具。第一批意图可以包括解释命令、生成 Wolfram 表达式、执行 Wolfram、渲染 LaTeX、解释计算结果、查询状态、搜索历史和生成帮助。模型负责理解意图和生成 tool call，Core 负责校验、执行、记录和返回结果。

这一阶段不采用默认 RAG。历史、文档、配置和记忆都作为工具存在。模型需要时主动调用 docs.search、history.search、state.get 等工具。默认回复仍然保持轻上下文。

自然语言工具调用先经过 planner-only shadow，再开放受预算限制的只读调用，最后才开放绑定精确参数和有效期的确认写操作。Core 必须限制最大 tool turns、总时长、token/cost、provider 并发和结果大小；tool result 仍视为不可信输入。模型不可用时，命令路径和确定性工具不能受到影响。

11. 第六阶段：三账号协同与 Watchdog

第六阶段目标是实现 Command、Talk、Watchdog 三账号分工。Command 号负责命令和确定性工具，Talk 号负责自然语言和解释，Watchdog 号负责健康检查、告警和灾备降级。Watchdog 平时不参与普通聊天，只在实例离线、NapCat 断连、工具异常、风控下线或管理员查询时响应。

灾备策略应当支持降级矩阵。Command 号下线时，由 Reserve 在有界 role lease 下接管经过审阅的低风险命令；Talk 号下线时，由 Reserve 接管经过审阅的自然语言路径。第一版 Reserve 同时最多持有一个响应角色；两者均异常时默认进入缩减状态，由管理员选择单一接管角色，而不是让一个账号静默承担两套人格和全部权限。

降级矩阵必须按工具声明主 provider、允许的 fallback、降级限额和禁止接管项，并使用 lease/fencing 防止故障恢复时双执行。Reserve 的 adapter 为保证采集连续性会接收并上报受保护群消息，但 Watchdog/incident 逻辑只消费健康、coverage 和 incident 事件，不把普通聊天内容送入第三套对话大脑；恢复需要 cooldown、hysteresis 和管理员可见的事件时间线。

三账号的具体形态采用“采集全活、发言主备”：Command、Talk 和第三个 Reserve 账号平时都保持在线并持续采集受保护群消息，但 Reserve 默认严格静默，只在某个主账号确认不可用且取得有界 role lease 后替代该逻辑角色发言。采集侧必须增加持久 ingress spool、幂等重放和 coverage/gap 诊断；单纯再加一个内存队列无法保证消息不遗漏。启用自动接管后，采集仍然 durable fail-open，发言则必须 lease-required 并带 fencing，避免网络分区时主号与备号同时说话。详细方案见 `docs/PHASE6_THREE_ACCOUNT_HA.md`。

12. 第七阶段：多平台入口

第七阶段目标是接入 Telegram 管理侧和 Web Admin，并预留微信 Claw、Discord、邮件、B 站直播等平台。Telegram 优先用于管理员私聊、告警、远程状态查询和简单工具调用。Web Admin 用于查看事件、日志、配置、权限、工具调用、缓存、健康状态和灾备切换。微信 Claw 可以作为低频入口，不建议早期承担高风险或高频功能。

多平台扩展必须坚持 adapter 薄层原则。平台差异只存在于 adapter，不应渗透到 Core、Tool Registry 和 Agent Runtime。

接入顺序优先 Telegram 管理私聊和只读 Web Admin，再考虑微信、Discord、邮件和直播入口。跨平台身份必须显式绑定，不能用昵称等价；跨平台转发属于需要权限和确认的写工具。第二个平台验收时，同一事件、工具和 Renderer contract 不应在 provider 内出现平台分支。

13. 第八阶段：Memory as Tool

第八阶段目标是建立保守、可控、按需调用的记忆系统。莉莉不应默认向每次对话注入长期记忆，而应当将记忆视为工具。短期上下文用于局部话题，结构化状态用于群配置、活动状态和任务状态，历史搜索用于明确的“之前”“上次”“谁说过”场景，长期画像只保存稳定、低敏、长期有价值的信息。

优先实现 history.search、docs.search、state.get 和 memory.lookup。向量检索可以作为后续增强，不应成为第一版默认路径。能用精确查询就不用向量召回，能用结构化状态就不用自然语言记忆。

顺序应为 state.get、docs.search、带会话授权的 history.search、带来源和过期时间的 memory.lookup，最后才是 embedding/rerank。每个结果必须携带 source、scope、time、confidence 和 redaction；写记忆是单独的受审工具，不允许把模型推断的人物画像静默持久化。删除、导出、保留期和同意机制属于第一版验收条件。

14. 第九阶段：活动现场系统

第九阶段目标是把莉莉接入线下活动。她应当能够读取节目单、抽奖池、电子票、签到状态、OBS 场景、弹幕和 Staff 面板。现场可以通过自然语言、命令、按钮或网页触发报幕、抽奖、倒计时、群聊同步和状态展示。

这一阶段尤其适合东方济悠樱、上高联例会和校内活动。莉莉可以逐渐从群聊工具变成活动现场助手。

节目单、票务、签到、抽奖、Staff 权限、计时器、OBS scene 和公告都应成为结构化状态与工具。上线前必须有 rehearsal/simulation；抽奖输入与结果可复现，现场网络中断有离线/降级 runbook，所有写动作记录操作人、确认和补偿/回滚方式。

15. 第十阶段：Lily Fumo 与 Neuro-Lily-sama

第十阶段目标是给莉莉增加实体身体和虚拟身体。Lily Fumo 是线下实体终端，可以通过麦克风、扬声器、按钮、LED、小屏幕、摄像头扫码、NFC 等方式和 Lily Core 交互。Neuro-Lily-sama 是 OBS / Live2D / PNGTuber 皮套前端，可以负责语音、字幕、表情、动作、报幕、抽奖和现场展示。

Fumo 和皮套不应拥有独立大脑，而应作为 avatar adapter 接入 Lily Core。Core 输出统一的 speak、emotion、action、subtitle 和 display_card，不同身体自行表现。

身体 adapter 只消费带版本、时序和过期时间的 intent；需要 session lease、急停/静音、麦克风摄像头隐私指示、队列上限和离线安全状态。设备凭据不能获得 Core 管理工具权限，过期或重复动作不能在重连后再次执行。

16. 第十一阶段：自研 Runtime 替换旧系统

当 Lily Core、Tool Registry、Renderer、Agent Runtime、Watchdog 和多平台 adapter 基本稳定后，再考虑逐步替换 NoneBot 和 Nekro。此时可以自研 OneBot / Satori adapter、插件加载器、agent loop、tool runner、model router、sandbox 和 Web Admin。替换旧系统应当是长期结果，不应作为第一步。

最终目标是形成完整 Lily Harness。NoneBot 和 Nekro 在早期是可复用资产，在中期是外围器官，在长期可以逐步退役。真正的长期资产是 Lily Core 的事件模型、工具协议、权限系统、渲染系统、记忆工具、审计轨迹和多平台 adapter。

替换必须逐组件进行，每个自研 adapter、plugin host、agent loop、runner 或 sandbox 都要先 shadow 真实流量，比较行为、延迟、资源和故障边界，并保留即时回滚。只要旧 runtime 能作为健康 provider 服从稳定合同，项目并不以“全部重写”为完成条件。

17. 近期优先级

当前近期优先级已经从“完成恢复故障矩阵”推进到“补齐确认与 artifact 账本，再迁移计算工具”：

1. `0015_tool_attempts`、`status.inspect@1.0.2` 的 `ledger_only` 与 M0 默认禁用生产签署已完成，继续观察零 attempt、inventory/heartbeat 与旧命令不变。
2. M1 descriptor lifecycle 已完成实现、双数据库回归、生产备份/恢复和默认禁用迁移；继续观察零 mutation、零 attempt 和无 drift。
3. M2 Provider quarantine、M3 Git-bound rollout plan、四种独立 stop 和首个单次 `admin_api` canary 已完成生产证明。继续保持角色、短会话、重认证、服务端 preview、CAS、幂等、只追加 before/after 审计和可测回滚，禁止直接 SQL 代替。
4. 当前 13 份单次计划均已暂停且耗尽；Core 保持 `ledger_only`，不将“descriptor 已 active”误解为仍有执行 authority。只有新的 Git-reviewed plan 才能再次开放有界调用。
5. safe retry、旧 fence、非法输出、时钟偏移、取消与 Core/PostgreSQL 中断已完成 8/8 生产故障演练；两个 `unknown_completion` 作为真实不确定性保留。
6. `status.inspect` 修正版 Provider 已跨过完整 inventory 稳定周期；`0016_confirm_artifacts` 的实现与双数据库回归已经完成，下一步是默认关闭的生产迁移、备份恢复和零 authority 签署。
7. 生产签署后迁移文本模式 `wolfram.run`；图像输出和 `latex.render` 必须先走独立的 finalized artifact canary。通用工具还需要操作系统级 sandbox，不能复用当前进程监督器冒充完整隔离。旧命令入口始终保留为回滚路径，自然语言 tool calling 继续后置到 Phase 5。

不要因为 Tool Registry 已经有设计就同时启动 Renderer、自然语言 agent、Memory、Fumo 或 Web Admin 全功能。每次只提升一层 authority，并保留旧入口和回滚。

18. 项目判断

超究极莉莉的本质不是“一个更大的 bot”，而是一个面向社群、活动和群聊环境的 personal/social agent harness。它借鉴 Codex、Claude Code、Pi、Hermes 等 harness 的工具调用、权限、审计、沙箱和 agent loop 思路，但目标域不是代码仓库，而是 QQ 群、社群活动、线下现场、渲染工具、计算引擎、聊天记录、多账号灾备和虚拟/实体身体。

第一、二阶段已经让莉莉从“两个互不可见的 bot”进入“独立运行时共享一个裁决与审计核心”的状态。第三阶段要继续把工具 authority 和执行账本收归 Core，而不是把插件代码搬进 Core API 进程。只有这些合同和 authority gate 稳定后，Wolfram、LaTeX、自然语言、Watchdog、多平台、Fumo、皮套和活动系统才可以作为能力逐渐接入；否则继续堆插件只会让莉莉越来越强，但也越来越分裂、越来越不可控。

当前执行路线以 `docs/ROADMAP.md` 为准，第二阶段证据以 `docs/ACCEPTANCE.md`、`docs/PHASE2_FINAL_AUDIT.md` 和 `docs/PHASE2_REVIEW.md` 为准；第三阶段协议、验收和已接受决策分别以 `docs/PHASE3_TOOL_REGISTRY.md`、`docs/PHASE3_ACCEPTANCE.md` 和 `docs/adr/` 为准。愿景、合同、实现和验收由此分开维护。

第四至第十一阶段的共享契约、内部工作包、故障模型、权限提升点和退出门槛见 `docs/FUTURE_PHASES_DESIGN.md`。该文档用于提前消除架构歧义，不代表允许跳过当前阶段门禁并行上线后续功能。
