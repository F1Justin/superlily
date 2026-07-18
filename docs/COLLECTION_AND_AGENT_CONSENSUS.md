# Superlily 采集与 Agent 产品共识

状态：2026-07-18 用户确认的长期设计共识。本文记录产品意图和实施边界，避免这些决定只存在于一次对话中。具体生产权限仍以 `ROADMAP.md`、阶段验收和 accepted ADR 为准。

## 1. 总原则

Superlily 不只是一个当下回答问题的机器人，也承担社群数字史料的持续记录职责。在 bot 有权看到、且被明确配置为允许长期留存的会话范围内，采集原则是：

> 除明确排除的大体积二进制内容外，应采尽采；无法采到、只采到一部分或被限制截断时，必须把缺口本身记录下来。

“采集”与“使用”是两件事。reaction、撤回、合并转发等进入数据库，不代表系统已经决定把它们用于模型训练、反馈奖励、自动重答或用户画像。后续用途必须经过独立设计和授权。

## 2. 阶段归属与当前优先级

完整采集本质上属于 Phase 1 的可观测性、Phase 2a 的统一事件图谱和 Phase 6 `HA-0` 的持久入口基础，而不是 Phase 5 的自然语言 Agent 能力。

Phase 2 已经完成生产验收，不应改写既有验收结论。采集工作拆成两个边界不同的工作包：

```text
Phase 2 已验收
  -> Phase 3a 零权限 Tool Registry 已部署
  -> C0-D 采集可靠性底座
       -> Phase 3b 调用账本与 Provider lease
       -> C0-A 档案完整性扩展（可与 Phase 3b 分别推进）
```

`C0-D` 是 Phase 3b 的前置条件，只包含 capture profile、durable spool、commit receipt、watermark/lag/gap、幂等重放、sanitizer 和基础 action 事件。`C0-A` 承担多层合并转发、正式启用长期归档、离线导出、重建、保留/删除传播和旧历史导入。C0-A 在 C0-D 稳定后可以与 Phase 3b 分别推进，不能用极端档案完整性案例长期阻塞工具调用账本。

两个工作包都不改变 claim、命令执行、自然语言回复或平台发送行为。

## 3. 采集范围

### 3.1 应长期保留的结构化内容

- 收到和发出的普通消息、文本及原始消息段顺序；
- reply、quote、mention、forward、derived-from 等引用关系；
- QQ reaction/贴表情动作，包括目标消息、操作者、表情、平台给出的数量或状态和发生时间；
- recall、poke、群成员增减、管理员变化、群文件等 bot 实际收到的平台事件；
- 合并转发的完整节点树，包括多层嵌套合并转发；
- 每个 bot 账号各自观察到的平台局部身份字段和接收时间；
- 采集、展开、重试、截断、失败和补录状态；
- bot 自己发出的回复、发送尝试和平台回执。

同一平台事件被多个账号看到时，仍然先保留各自 observation，再按已经验证的强身份规则关联。不得因为文本和时间相近而把史料强制合并。

### 3.2 图片和其他二进制内容

第一版 C0-D 不下载和长期保存图片字节，但应保留：

- 图片在消息段中的位置；
- 平台提供的文件名、类型、尺寸、大小、哈希和平台资源 ID；
- 是否曾提供临时 URL，以及 URL 因安全或保留策略未保存的说明；
- `content_unavailable`、`metadata_only` 等明确状态。

图片 URL 可能短期失效或携带访问凭证，不能假装它是永久档案。文件、语音和视频等大对象也不应直接写入 PostgreSQL；如果以后决定保存其字节，应进入独立的内容寻址对象存储，受单对象、会话、每日和总容量配额约束。第一版只承诺消息结构和可得元数据完整。

### 3.3 会话范围

采集配置至少应区分：

- `operational`：维持当前运行所需的最小记录；
- `archive_full`：长期保存结构化事件、消息内容和嵌套转发，二进制按附件策略处理；
- `off`：除最低健康与拒绝审计外不保留会话内容。

配置以精确的平台、会话类型和会话 ID 为范围。群聊归档与私聊归档不能互相推导；新平台、新群和私聊不会仅因 adapter 能看到就自动获得永久保留资格。

## 4. 数据库设计

### 4.1 继续使用现有事件主干

现有三层仍然成立：

- `source_events`：平台事件的 canonical 身份；
- `event_observations`：某个 bot 账号实际看到并上报的内容；
- `event_links`：事件之间已解析或待解析的关系。

普通消息的 `text`、`segments_json` 和 `attachments_json` 继续保存在 observation。C0-D/C0-A 不另建一套平行消息历史库。

### 4.2 平台动作明细

为 reaction、recall、poke 和成员变化等事件增加规范化的 observation 明细，例如逻辑表 `platform_action_observations`：

```text
observation_id                 FK -> event_observations
action_kind                   reaction | recall | poke | member_change | ...
operation                     add | remove | update | observed_state | unknown
actor_principal_id            实际操作者；平台未提供时为空
subject_principal_id          被操作用户；与 actor 分开
target_platform_message_id    目标账号视角下的平台消息 ID
target_source_event_id        解析后的 canonical 目标，可为空
value_json                    emoji_id、count、sub_type 等有界结构
capture_status                complete | partial | unavailable
schema_version
```

reaction 是平台动作，不在这一层标注正面、负面或反馈权重。Nekro 自己贴的思考表情和群友贴的拳头都按事实记录，语义留给未来独立功能。

动作本身的幂等身份应包含事件种类、观察账号、会话、目标、本地原生序列、操作者、动作值和平台时间等可得强字段。目标消息 ID 是账号局部值，解析时必须绑定观察实例和会话，不能全局按数字相等连接。

### 4.3 合并转发树（C0-A）

普通 observation 中的 `forward` segment 只表示根消息引用了一个转发包。实际展开内容使用两类逻辑记录：

`forward_archives`：

```text
id
root_observation_id
root_segment_path
observer_instance_id
platform_forward_id
content_hash
capture_status               pending | complete | partial | unavailable
node_count / max_depth
first_attempt_at / completed_at
```

`forward_nodes`：

```text
forward_archive_id
node_path                    例如 0/2/1
parent_path
sibling_index / depth
platform_sender_id / display_name
platform_time
text / segments_json / attachments_json
nested_forward_archive_id
content_hash
capture_status
```

节点路径和兄弟顺序必须稳定，确保未来可以重建用户看到的聊天记录。节点中的 reply、quote 和 nested forward 继续形成一等关系。嵌套展开使用 `seen` 集合防循环，并设置最大深度、总节点数、总文本字符数和总结构化字节数；到达上限时保存已经取得的节点，并明确标记 `partial` 和截断原因。

### 4.4 展开与采集尝试（C0-A）

合并转发内容可能需要调用 QQ adapter 的 `get_forward_msg` 一类接口，Core 不应持有 QQ 凭证。bridge 在独立于聊天回复的后台采集路径中展开并上报，Core 保存每次尝试：

```text
capture_kind / target_id
observer_instance_id
attempt_number
started_at / finished_at
outcome
retryable
bounded_error
next_retry_at
```

根消息入库不能等待转发展开完成。平台接口超时、转发 ID 过期或嵌套节点暂时失败时，根 observation 仍先提交，展开任务随后重试并补齐。

### 4.5 未映射字段

“应采尽采”不能靠简单打开全局 `raw_json` 实现。C0-D 先固定 sanitizer、完整性状态和有界未映射字段，C0-A 再扩大长期档案覆盖。推荐保存：

1. 已知字段的规范化表示；
2. 从原平台事件中移除已规范化大字段后的 `platform_extra_json`；
3. sanitizer 版本、删除字段清单、原始 payload 哈希和采集 profile；
4. 对凭证、会话密钥、本地路径和带授权参数的临时媒体 URL 的强制删除或不可逆脱敏。

这样既保留未来才认识的新字段，又不重复存储整份正文，也不把平台凭证长期写入史料库。归档 payload 必须有大小上限；超限时留下哈希、原始大小和截断状态。

## 5. 持久采集链路

长期运行 bot 并不自动等于资料不会丢失。目前 bridge 的有界内存队列在 Core、网络或 PostgreSQL 长时间不可用时可能丢数据。C0-D 应把 `HA-0` durable ingress spool 一并提前：

```text
平台事件
  -> bridge 本地持久 spool（先落盘）
  -> Core 幂等写入 source event / observation / action / forward archive
  -> Core 返回 durable commit receipt
  -> bridge 才清理已确认 spool 记录
```

spool 至少需要：

- 单调本地记录号、校验和和独立 idempotency key；
- 原子追加、崩溃后尾部修复、容量与磁盘保留策略；
- Core/PostgreSQL 离线后的有界退避与自动重放；
- committed、pending、quarantined 三种可解释终态；
- 每实例 watermark、lag、gap 和最老未提交记录的诊断；
- 重放不产生重复 source event、observation、action 或 forward node 的测试。

聊天响应继续 fail-open；采集写盘和重放不能阻塞 NoneBot/Nekro 的正常处理线程。

OneBot 实现重新登录后可能在数秒内补投大量积压事件，控制台日志还可能把这些行都显示成
当前时刻。因此必须分开保存平台/adapter 报告的 `occurred_at`、bridge 实际原子落盘的
`ingress.captured_at`、Core 的 `received_at` 和数据库提交 `committed_at`。无法从上游
恢复原始发生时间时必须保留这种不确定性，不能用启动时间伪造历史时间；去重和顺序优先
依赖消息 ID、`real_seq`、idempotency key 与 spool sequence，而不是只比较时间戳。

## 6. 史料保存不应只依赖在线数据库（C0-A）

PostgreSQL 是工作数据库，不是唯一档案介质。`archive_full` 数据还需要：

- 定期一致性备份和实际恢复演练；
- 按时间和会话导出版本化 JSONL/Parquet 档案；
- manifest、schema 版本、行数、时间范围和文件 SHA-256；
- source event、observation、link、action 和 forward tree 的可移植关联键；
- 导出完成后的抽样重建测试，证明嵌套转发和消息段顺序可恢复；
- 明确的删除、排除和保留变更记录。

只有数据库里“现在查得到”还不够；升级数年后仍能解释和重建，才算真正保存下来。

## 7. 此前 Agent 与工具讨论形成的共识

### 7.1 绝大多数消息走快速路径

约 99% 的用户需求只是一个小问题，期待直接、快速的回复。系统不应把每条群消息都升级成 Claude Code 式长任务。默认路径使用轻上下文、低延迟模型和零工具或极少工具；只有任务确实需要时才进入多轮 agent run。

### 7.2 模型负责选择，系统负责边界

不应人工编排每一种“读图 -> Wolfram -> Python -> TeX -> 发图”的固定路径。模型可以根据问题和中间结果选择工具、回修代码、追加调用并决定最终表达方式。系统需要硬性规定的是：

- 工具与资源怎样被发现；
- 身份、权限、确认和副作用边界；
- 总轮数、时间、token、费用、并发和产物大小预算；
- 过程状态、失败、取消和最终发送怎样记录；
- 输出能力不匹配时怎样显式降级。

这保留了模型从规模和通用能力中获益的空间，同时不把安全与审计寄托在模型自觉上。

### 7.3 渐进式披露

初始上下文保持小而稳定：当前消息与引用图、短会话窗口、身份/权限摘要，以及当前真正 eligible 的工具短描述。详细 schema、历史、文档、配置、大段群聊和资源目录只在需要时通过工具获取。

工具目录也应分层：先给类别和少量候选，再按需读取完整 descriptor。不能一次把全部历史和所有工具说明塞进 prompt，也不能因为默认没给就让模型永远无法发现资源。

### 7.4 Unix 原语与领域工具并存

在只读或沙箱资源环境中，模型应能使用已经擅长的 `rg`、`grep`、`find`、`jq` 和管道组合，自主探索文件、导出数据和文档。这通常比为每个查询形态制造一个窄工具更有泛化能力。

但数据库会话范围、跨用户隐私、QQ 操作、配置修改和其他有副作用能力仍需要 typed tool、权限与审计。`history.search` 的价值不一定是替代 `rg`，而是提供经过授权、限定会话并可审计的只读数据视图；模型可以在其返回的导出或沙箱副本上继续使用 Unix 原语。

### 7.5 自然语言可以复用命令语义

“帮我换个老婆”可以等价于该用户执行现有“换老婆”命令，但应复用同一个结构化工具和权限语义，而不是让模型伪造一条聊天命令再注入 matcher。Core 记录原始自然语言、解析出的工具与参数、调用身份、权限快照和结果；命令入口继续作为确定性兼容路径存在。

### 7.6 群聊 Agent 与代码 Agent 的差异

代码 Agent 通常有明确 goal、可修改工作区并能通过测试验证；群聊中大量输入没有完整 goal，用户可能随时插话，也更在意首条响应延迟和公共频道噪声。因此 Superlily 需要：

- 默认快速直答；
- 只有较长任务才发送克制的进度消息；
- agent run 可取消、可被新消息修正，并有明确预算；
- 工具结果与最终发送分离，由 Renderer/adapter 根据平台能力、内容和已配置偏好选择文本、公式图、Markdown 图等形式。

主题或时间可以作为渲染偏好输入，但不是写死在工具里的规则，也不能让展示偏好改变计算结果。

### 7.7 模型路由以成本可持续为前提

不需要给所有群、所有消息常驻高端模型。默认使用经济模型；对明确的高难学术、强时效、医疗安全、复杂图片或低置信度任务，可以在预算内预先升级、搜索或调用确定性工具。特定高质量要求群可以配置更敏感的升级阈值，但仍受每日/会话费用上限约束。

模型路由、搜索和未来可能的纠错机制属于后续阶段。reaction 在 C0 中只是需要忠实保存的平台事件，不因可能具有反馈价值而提前绑定任何训练或自动行为。

### 7.8 为未知入口保留通用边界

未来可能接入目前无法预见的平台、实体设备或社群能力。Core 不为每种设备预写一套推理流程，而是让 adapter 声明事件、身份、能力、资源和回执，让模型按需看到经过权限过滤的能力摘要。新的入口不能因为形态新颖就绕过现有的 principal、tool、artifact、delivery 和 audit 边界。

## 8. 建议实施包

### 8.1 C0-D：采集可靠性

1. `C0-D1`：固定 capture profile、sanitizer/completeness envelope、commit receipt、watermark 和基础 action 契约；完成 SQLite/PostgreSQL 迁移。
2. `C0-D2`：实现两侧 bridge 的 durable spool、幂等重放、配额和 quarantine。
3. `C0-D3`：实现 Core receipt/watermark、lag/gap 与管理员诊断。
4. `C0-D4`：让 Lily 与 Nekro 都上报 reaction、recall、poke 等基础 notice，并按观察实例解析目标。
5. `C0-D5`：完成 Core/PostgreSQL 离线、bridge 崩溃、重复提交、坏尾部和磁盘配额故障测试。

只有 C0-D 是 Phase 3b 的前置条件。

### 8.2 C0-A：档案完整性扩展

1. `C0-A1`：实现合并转发异步展开、多层递归、去重、限制、重试与 partial 状态。
2. `C0-A2`：在容量和字段覆盖审计后，为首批会话正式启用 `archive_full`。
3. `C0-A3`：提供可移植导出、备份恢复、嵌套消息重建和保留/删除传播。
4. `C0-A4`：最后讨论旧 Lily/Nekro 历史的分源导入。

C0-A 在 C0-D 稳定后可以与 Phase 3b 分别排期；Phase 3b 不依赖两层嵌套转发或离线档案重建。

历史导入继续遵守 `HISTORY_DRY_RUN.md`：旧库缺少可靠跨账号强身份时，保留来源身份，不能用文本和时间伪造统一 canonical 事件。

`C0-D1` 使用下一个单线 Alembic revision `0013_collection_reliability`。尚未部署的 Phase 3 迁移顺延为 `0014_tool_invocations`、`0015_tool_attempts` 和 `0016_tool_confirmations_artifacts`；不能为了保留旧编号制造两个 migration head，也不能把 C0-D 表塞进已经部署的 `0012_tool_registry`。

## 9. 验收标准

### 9.1 C0-D

- Core 离线、PostgreSQL 离线和 bridge 重启期间的编号消息最终全部成为 committed、pending 或 quarantined，没有静默消失；
- Lily 和 Nekro 各自观察到的 reaction 均能入库，表情值、操作者和目标按平台实际字段保存，缺失字段明确为空而非猜测；
- 图片字节未进入 PostgreSQL，但图片占位和可得元数据仍在正确节点位置；
- 重放与多账号重复观察不会制造重复 observation 或 action；
- receipt、watermark、lag、gap、quota 和 quarantine 状态可诊断；
- C0-D 上线前后，命令、Nekro 回复、claim 和工具 Registry 权限完全不变。

### 9.2 C0-A

- 至少一条两层嵌套合并转发可按原顺序重建，限制触发时得到可解释的 partial 档案；
- 重放与多账号重复观察不会制造重复 forward node；
- 归档导出经过哈希校验，并能在独立临时数据库或离线读取器中抽样恢复；
- 保留与删除策略能传播到导出和对象存储，旧历史导入不伪造跨账号 canonical 身份。
