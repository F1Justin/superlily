超极莉莉 Lily Harness 技术路线与阶段规划

1. 简要愿景

超究极莉莉不是一个单纯的 QQ bot，也不是把 NoneBot、Nekro Agent、Wolfram、LaTeX、词云、抽奖等插件简单堆在一起。它的长期目标是成为一个面向社群环境的 agent harness，也就是一个能够接收群聊、私聊、网页、线下设备、OBS、Fumo、皮套等多种入口事件，并通过统一核心进行理解、调度、工具调用、权限控制、渲染输出、记忆检索、灾备告警和多平台响应的社群智能运行时。

从用户视角看，莉莉既可以被命令精确调用，也可以通过自然语言理解需求。用户可以直接输入 /wf Limit[Sin[x]/x,x->0]，也可以说“莉莉，帮我求一下 sinx/x 在 0 的极限并排成公式图”。系统应当能够把自然语言转换成受控工具调用，调用 Wolfram Engine、LaTeX Renderer、Markdown Renderer、历史搜索、状态查询、活动系统等工具，然后根据平台能力返回文本、图片、公式图、语音、字幕或 OBS 画面。

从系统视角看，莉莉应当逐渐从“插件集合”变成“统一编排系统”。现有 Lily Bot 负责命令和确定性工具，Nekro Agent 负责自然语言聊天，未来的 Watchdog 账号负责健康检查、风控下线告警和灾备降级。长期还可以接入 Telegram、Web Admin、微信 Claw、Fumo 实体终端、Neuro-Lily-sama 皮套和活动现场系统。所有这些前端都不应拥有独立大脑，而应当作为 Lily Core 的 adapter 或 avatar，共享同一套工具、权限、状态、记忆和审计。

2. 当前基础

当前系统已经存在两套独立 QQ bot。Lily Bot 基于 NoneBot2、FastAPI Driver 和 OneBot V11，当前接入 QQ 3643287298（历史记录中仍包含旧账号 985393579），经 NapCat 提供 QQ 协议服务。它目前主要承担命令式工具箱功能，包括 LaTeX 公式渲染、Wolfram Engine 计算、东方运势、语音触发、梗图检测、词云、服务器状态等能力。Nekro Agent 独立运行于 Docker Compose，接入 QQ 2022692714，主要承担自然语言聊天、模型路由、沙箱代码执行、Qdrant 记忆和插件式 AI 能力。

目前 Lily 侧整体更接近确定性命令系统，Nekro 侧整体更接近 AI agent 系统。两者完全独立，拥有不同 QQ 号、不同数据库、不同配置和不同代码架构。管理员 QQ 号在两个系统中均为超管。这个分裂状态短期可用，但长期会造成工具重复、记忆分裂、权限分裂、响应抢答、灾备困难和多平台扩展困难。

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

第一版 Lily Core 推荐使用 Python、FastAPI、asyncio、PostgreSQL 和 Redis。Python 能最大程度复用现有 NoneBot 插件、Wolfram 调用、LaTeX 渲染、Playwright 和 Nekro 周边生态。FastAPI 适合作为核心 API 和 Web Admin 后端。PostgreSQL 负责长期结构化数据，包括事件、配置、权限、工具调用轨迹、响应记录和审计日志。Redis 负责短期状态，包括 claim lock、heartbeat、rate limit、任务队列和临时缓存。

向量数据库不应作为第一阶段核心依赖。未来如果需要记忆和历史语义检索，可以优先考虑 PostgreSQL 全文搜索、BM25、pgvector 或继续复用现有 Qdrant。第一阶段重点不是“让莉莉记住一切”，而是让它能够统一接入事件、记录状态、抽象工具和控制响应。

仓库结构可以先采用 monorepo，降低开发和部署心智负担。一个初始结构可以是：

superlily/
  core/
    api/
    models/
    router/
    permissions/
    storage/
  adapters/
    onebot/
    telegram/
    web/
    avatar/
  tools/
    wolfram/
    latex/
    render/
    history/
    status/
    fortune/
  agent/
    prompts/
    runtime.py
    router.py
  watchdog/
  webui/
  docs/
    LILY.md
    TOOLS.md
    SERVICES.md
    GROUPS.md
    ROADMAP.md

6. 第一阶段详细规划：Lily Core MVP

第一阶段的目标不是重写 NoneBot，也不是替换 Nekro，而是建立一个独立的 Lily Core MVP，使它能够接收、记录和观察现有系统事件，并为后续统一调度打基础。第一阶段完成后，现有 Lily Bot 和 Nekro Agent 仍然可以按原逻辑运行，但它们的消息、回复、心跳和工具调用应当开始进入 Lily Core 的统一视图。

6.1 阶段目标

第一阶段应当完成五件事。第一，建立独立 lily-core 服务，提供 HTTP API。第二，定义最小统一事件模型和响应模型。第三，让 NoneBot Lily 侧将收到的 QQ 消息上报到 Core。第四，让 Nekro Agent 或其适配层将自然语言消息和回复结果上报到 Core。第五，实现基础健康检查和实例心跳，为后续 Watchdog 做准备。

第一阶段不要求 Core 接管回复，不要求实现自然语言 tool calling，不要求迁移现有插件，不要求 Web Admin 完整可用，也不要求多平台。此阶段主要是“旁路观察”和“统一日志”。

6.2 最小功能范围

第一阶段 Lily Core 至少应提供以下 API。

POST /events
接收标准化事件，包括消息事件、命令事件、健康事件和系统事件。
POST /responses
记录某个 bot 实例对某个事件发出的响应，包括文本、图片、错误和耗时。
POST /heartbeat
记录 bot 实例心跳，包括实例名、平台、账号、进程状态、连接状态和时间戳。
GET /health
返回核心服务、数据库、Redis 和已注册 bot 实例的健康状态。
GET /events/recent
查看最近事件，供调试使用。
GET /instances
查看当前所有 bot 实例及最近心跳。

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

截至 2026-07-13，Phase 2a.2 与 2b.2 已实现并部署：Correlation v3、canonical decision policy v2、运行时命令清单、response outcome、唯一引用解析、平台 capability snapshot 和双桥 claim 均已上线。单测试群 canary 的 command/talk/reply/ordinary 样本已经通过，Core 两分钟故障回退也已通过；当前只等待最终部署后的 24 小时稳定窗口与安全审计。在这些证据写入 `docs/ACCEPTANCE.md` 前，不开始第三阶段工具执行。

8. 第三阶段：Tool Registry 与现有插件迁移

第三阶段目标是把现有命令插件逐步改造成结构化工具。wf 应当成为 wolfram.run，tex 应当成为 latex.render，Markdown 帮助图片应当成为 markdown.render_image，状态图应当成为 status.inspect，历史检索应当成为 history.search。命令入口仍然保留，但它们只是工具的一种调用方式。

每个工具必须声明名称、描述、参数 schema、返回类型、权限要求、超时、频率限制、是否允许自然语言调用、是否需要确认。自然语言 agent 后续只能申请调用已注册工具，不能绕过 Core 直接执行底层操作。

第三阶段进一步拆成四步。3a 只建立经过人工审阅的 tool descriptor 和经过实例认证的 runtime provider snapshot，不执行工具；3b 建立 invocation、attempt、confirmation、lease、fencing token、deadline、budget、artifact 的完整账本和 provider 拉取协议；3c 依次迁移 status.inspect、wolfram.run、latex.render 等低风险工具；3d 才让旧命令入口在 shadow/canary 后切到同一工具协议。运行时发现只证明“实现正在加载”，不能自动获得权限。工具 provider 不运行在 Core API 进程内，也不开放 Lily/Nekro 的入站执行端口，而是从 Core 拉取有界 lease。

第三阶段完成时，命令入口仍然存在，自然语言模型仍然没有工具执行权。详细字段、状态机、数据库表、API、迁移顺序和验收标准见 `docs/PHASE3_TOOL_REGISTRY.md`；跨阶段依赖和门禁见 `docs/ROADMAP.md`。

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

灾备策略应当支持降级矩阵。Command 号下线时，Talk 号可以接管部分低风险命令。Talk 号下线时，Command 号保留命令功能但关闭自然语言。两者均异常时，Watchdog 只进行管理员告警和基础状态查询，避免灾备号也被风控。

降级矩阵必须按工具声明主 provider、允许的 fallback、降级限额和禁止接管项，并使用 lease/fencing 防止故障恢复时双执行。Watchdog 只消费健康和 incident 事件，默认不观察普通聊天；恢复需要 cooldown、hysteresis 和管理员可见的事件时间线。

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

当前近期优先级已经从第一阶段推进到第二阶段最终验收：

1. 完成 2026-07-13 最终部署后的 24 小时 canary、安全和稳定性审计。
2. 固化 Phase 2 acceptance、项目 review、迁移/回滚证据并提交。
3. 按 `docs/PHASE3_TOOL_REGISTRY.md` 先实现 3a descriptor/registry，保持执行关闭。
4. 再实现 3b invocation ledger 与 provider lease/fencing，不接自然语言模型。
5. 依次迁移 status.inspect、wolfram.run、latex.render；每个工具单独 shadow/canary。
6. Phase 3 达标后进入统一 Renderer；自然语言 tool calling 继续后置。

不要因为 Tool Registry 已经有设计就同时启动 Renderer、自然语言 agent、Memory、Fumo 或 Web Admin 全功能。每次只提升一层 authority，并保留旧入口和回滚。

18. 项目判断

超究极莉莉的本质不是“一个更大的 bot”，而是一个面向社群、活动和群聊环境的 personal/social agent harness。它借鉴 Codex、Claude Code、Pi、Hermes 等 harness 的工具调用、权限、审计、沙箱和 agent loop 思路，但目标域不是代码仓库，而是 QQ 群、社群活动、线下现场、渲染工具、计算引擎、聊天记录、多账号灾备和虚拟/实体身体。

第一阶段的意义在于让莉莉从“两个独立 bot”开始变成“一个统一大脑”。只要这个核心立住，后续的 Wolfram、LaTeX、自然语言、Watchdog、多平台、Fumo、皮套和活动系统都可以作为能力逐渐接入。否则继续堆插件只会让莉莉越来越强，但也越来越分裂、越来越不可控。

当前执行路线以 `docs/ROADMAP.md` 为准，第二阶段证据以 `docs/ACCEPTANCE.md` 和 `docs/PHASE2_REVIEW.md` 为准，第三阶段协议以 `docs/PHASE3_TOOL_REGISTRY.md` 为准。愿景、合同、实现和验收由此分开维护。
