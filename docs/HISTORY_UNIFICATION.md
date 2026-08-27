# 历史统一合同（H0）

状态：accepted，2026-08-01。本文冻结主工作包 H 的来源、边界和安全不变量；它不
执行迁移，也不授予 ChatExporter 或 Agent 读取历史的权限。

本文与 `docs/ROADMAP.md`、`docs/HISTORY_DRY_RUN.md` 和
`docs/adr/0018-legacy-history-read-model.md` 一起构成 H0 的实施合同。H1 的
Alembic 迁移必须以本文为前置，不能从旧数据库的表结构直接推导一个未经审阅的导入器。

## 1. 目标和非目标

目标是把 Lily/NoneBot chatrecorder 和 Nekro 的历史消息，作为面向人类查询、查看和导出
的 read model 放进当前 Superlily PostgreSQL。统一后的读取入口最终由
`archive.message_timeline_v1`（或等价的版本化只读 API）提供。

以下事项不属于本合同：

- 不把历史行重放到 `source_events`、`event_observations`、`responses` 或其他在线热表；
- 不把历史自动注入 AgentRun、Nekro prompt、`history.search`、`memory.lookup` 或默认 RAG；
- 不依据“文本相同 + 时间接近”创建跨来源 canonical event；
- 不在本工作包中保存缺失的 reaction、媒体字节、capture completeness 或强跨账号身份；
- 不删除、清空、改表、改 collation、建索引或暂停两个旧数据库。

旧库在 H4 之后仍作为只读备份保留，直到另一个明确的数据处置决定。

## 2. 切换边界

边界是每个实例首次被 Core 观察到之前的历史范围。边界值固定为 UTC，导入谓词是严格
`source_occurred_at < cutover_boundary`；等于或晚于边界的源记录一律不导入，由当前
Superlily 事件链负责。边界不是一个可由导入器运行时猜测的时间窗口。

| 来源 | 固定边界 | 当前 Core 观察核对 |
| --- | --- | --- |
| Lily / `lily-command` | `2026-06-19 11:45:17.17105+00` | 首条 `event_observations.received_at` 为 `2026-06-19 11:45:17.175088+00` |
| Nekro / `nekro-agent` | Core 边界 `2026-06-19 11:49:44.696404+00`；来源谓词 `send_timestamp < 1781869784` | 首条 `event_observations.received_at` 为 `2026-06-19 11:49:44.698038+00` |

H2 开始时必须重新生成每个来源的 manifest，并在边界值、来源 schema、快照身份或
时区解释发生变化时停止导入，而不是自动调整边界。

Nekro 的 Core 边界有微秒，但真实 `send_timestamp` 只有整数秒；因此不能直接用
`to_timestamp(send_timestamp) < Core 边界`。来源侧必须保守排除包含首条 Core 观察的
整个秒，即使用 `send_timestamp < 1781869784`。已只读核对：`send_timestamp=1781869784`
只有源行 `id=1035299`（平台消息 ID `817661785`），正是首条 Core Nekro 观察；纳入它会
在 timeline 形成 legacy/Core 双份。

## 3. 来源合同

### 3.1 Lily / NoneBot chatrecorder

- 来源数据库：主机 PostgreSQL 17，数据库 `botmsg`，schema `public`；当前数据库会话
  时区为 `Asia/Shanghai`，并且报告有 collation version mismatch。导入不能借此修改来源
  数据库。
- 主消息表：`nonebot_plugin_chatrecorder_messagerecord_v2`，主键 `id`。
- 消息字段：`id`、`session_persist_id`、`time`、`type`、`message_id`、`message`、
  `plain_text`。
- 会话关系：`session_persist_id` 指向
  `nonebot_plugin_uninfo_sessionmodel.id`，再关联
  `nonebot_plugin_uninfo_botmodel`、`nonebot_plugin_uninfo_scenemodel` 和
  `nonebot_plugin_uninfo_usermodel`。`scene_type=0` 表示私聊，`scene_type=1` 表示群聊；
  原始 `scene_type` 和来源 key 必须同时保留。
- 账号范围已核对为 `985393579`（历史账号）和 `3643287298`（当前账号），两行均为
  `OneBot V11`/`QQClient`。导入必须从来源会话关系重建 bot 账号，不得把两个账号合成一个
  `bot_id`。
- `type=message` 是入站记录，`type=message_sent` 是 bot 出站记录。未知类型不得静默
  归入任一方向。
- `id` 是唯一的来源记录身份；`message_id` 只是平台/适配器视角的本地消息 ID，不能
  单独作为导入幂等键。已知历史中存在不同 bot 账号，因此来源身份至少包含来源系统、
  原表名和原主键。
- `time` 在数据库中是 `timestamp without time zone`，但当前安装的
  `nonebot-plugin-chatrecorder 0.7.0` 模型明确记录它保存 UTC 时间。导入时必须按
  `time AT TIME ZONE 'UTC'` 转成 `timestamptz`，不得按数据库会话时区解释。

2026-08-01 的只读核对值如下；它们是容量/边界基线，不替代 H2 开始时的 manifest：

| 指标 | 值 |
| --- | ---: |
| 主消息表总行数 | 8,742,661 |
| 严格早于 Lily 边界 | 8,262,010 |
| 其中 `message` / `message_sent` | 7,878,503 / 383,507 |
| 最早 / 最新 `time`（源列显示值） | 2024-08-28 13:25:30 / 2026-08-01 07:01:12 |

此前记录的 `8,267,828` 是把 UTC 边界误按 `Asia/Shanghai` 墙钟解释后多纳入 8 小时
所得；多出的 5,818 行仍在源表中，并非删除或 retention。H2 manifest 必须使用
`time AT TIME ZONE 'UTC' < timestamptz '2026-06-19 11:45:17.17105+00'`，当前正确的
边界前计数与 2026-07-12 dry-run 基线一致。

### 3.2 Nekro

- 来源容器：`nekro_postgres`，PostgreSQL 14.19，数据库 `nekro_agent`，schema `public`；
  数据库时区为 `Etc/UTC`。
- 主消息表：`chat_message`，主键 `id`。其消息行至少包含
  `sender_id`、`sender_name`、`sender_nickname`、`adapter_key`、`message_id`、
  `chat_key`、`chat_type`、`platform_userid`、`content_text`、`content_data`、
  `raw_cq_code`、`ext_data`、`send_timestamp`、`create_time` 和 `update_time`。
- `send_timestamp` 是平台消息实际发生的时间，H2 必须将其映射为
  `legacy_messages.occurred_at`，也是 timeline 的排序/边界时间；不能用落库时间代替它。
- `create_time` 是 Nekro 数据库捕获/持久化该行的时间，必须映射为
  `source_persisted_at`（在 capture-facing read API 中可别名为 `captured_at`），不能覆盖
  `occurred_at`。
- `send_timestamp` 缺失时，导入器必须拒绝该行并记录有限错误码（例如
  `missing_send_timestamp`）；不得静默回退到 `create_time`，也不得用回退后的时间通过边界
  检查。
- `chat_key` 是来源会话 key，`chat_type` 必须原样保留。当前同时存在
  `group`/`private` 和 `ChatType.GROUP`/`ChatType.PRIVATE` 值，规范化只能在映射层完成，
  不能删除 raw value。需要频道展示元数据时才读取 `chat_channel`，它不是消息幂等身份。
- `is_tome` 不等于“这是 bot 发出的消息”。如果来源语义不能可靠证明方向，`direction`
  必须为 `unknown`，不能从 sender 文本、时间或 `is_tome` 猜测出站。
- 来源幂等身份为来源系统、原表名和 `id`；`message_id`、`chat_key` 或文本都不能单独
  作为唯一键。
- 当前 Nekro Core 实例的账号是 `2022692714`（`onebot_v11`）；这只是实例身份，不是
  证明某条 `chat_message` 出站的充分条件。源行中的 `sender_id` 必须原样保存。

2026-08-01 的只读核对值如下：

| 指标 | 值 |
| --- | ---: |
| `chat_message` 总行数 | 1,205,359 |
| 严格早于 Nekro 来源秒级边界 | 1,035,247 |
| 早于边界的 group / private（按 raw `chat_type` 汇总） | 1,034,851 / 397 |
| 最早 / 最新 `create_time` | 2025-09-11 10:52:59.798943+00 / 2026-08-01 06:59:58.949289+00 |

### 3.3 当前 Superlily

2026-08-01 的冻结基线是 PostgreSQL 17、数据库 `superlily`、宿主端口
`127.0.0.1:5433`、Alembic `0024_agent_product_flow`，当时尚无 `archive` schema，数据库
大小约 2,323 MB。到 2026-08-12，生产已部署 `0025_legacy_history_archive`，archive 四表
仍全空；在线表已经包含大量事件观察和决策行，历史导入不得与这些热表共用一个大事务。

## 4. 目标 archive 边界

H1 迁移只创建结构，不连接或读取外部数据库。目标对象位于独立 `archive` schema：

1. `archive.import_batches`：来源系统、源快照身份、schema/映射版本、固定边界、开始/结束
   状态、计数、hash、拒绝计数和错误摘要。
2. `archive.conversation_mappings`：`source_system + source_conversation_key` 到规范
   `platform + conversation_type + conversation_id` 的显式、可审计、可修订映射。映射
   不得改变原始 key，也不得把两个来源的 key 强行合并。
3. `archive.legacy_messages`：按 `occurred_at` 月份分区的最小历史消息行，至少保存：
   来源系统、来源表、来源主键、批次、账号、原始会话 key、规范会话、发送者、方向、
   `occurred_at`、`source_persisted_at`（或对外的 `captured_at`）、平台消息 ID、文本、
   解析后的 segments、`reply_hint`、raw 字段引用和 `parse_warning`。对于 Nekro，
   `occurred_at` 只能来自 `send_timestamp`，持久化时间只能落在
   `source_persisted_at`/`captured_at`；无法安全解析的字段置空并记录原因，不能阻塞可读取
   文本，但缺失 `send_timestamp` 的行必须拒绝并记录有限错误。由于 PostgreSQL 分区表的
   唯一约束必须包含分区键，分区父表的物理唯一键包含 `occurred_at`；来源三元组的跨分区
   精确唯一性由同一 schema 内的 `archive.source_message_identities` ledger 强制。
4. `archive.source_message_identities`：不可分区的来源身份/幂等 ledger，以
   `(source_system, source_table, source_record_id)` 为主键，关联批次和可选的归档消息，
   使重跑不能跨分区产生同一来源的第二行。ledger 的 `imported` 行必须以真实的复合
   外键约束 `(legacy_message_id, occurred_at) REFERENCES archive.legacy_messages(id,
   occurred_at)` 指向消息分区父表；不能只靠状态 check、导入器约定或单列外键保证该关系。
5. `archive.message_timeline_v1`：版本化只读入口，合并旧 archive 行与当前 Core
   `event_links`/事件结果；它是展示 read model，不是新的 canonical event 表。timeline
   必须保留一个 Core 观察关联的多个 `event_links` 关系行，逐行保留 `event_link_id` 及其
   关系语义；每个 link 行还必须有稳定且唯一的 timeline `id`，不得用任一行或 `MIN(id)`
   静默折叠；旧消息行同时必须保留来源提供的 `reply_hint`，不能因合并或展示而丢失。
6. `archive.message_timeline_v2`：面向 ChatExporter 的版本化投影，在 v1 provenance 行上
   增加稳定 `display_key`、展示优先级、Core action、当前 Core reply target 和来源范围内
   的 legacy reply target。v2 不删除 v1 的 observation/link 行；默认折叠只在消费者展示
   时按 Core correlation 结果发生，`all_sources` 必须仍能读取全部关系行。

固定来源系统标识：

- `lily.nonebot.chatrecorder.v2`
- `nekro.chat_message`

来源三元组 `(source_system, source_table, source_record_id)` 必须在 archive 内精确唯一，
由 `archive.source_message_identities` ledger 强制；`legacy_messages` 分区父表的唯一
约束同时包含 `occurred_at` 以满足 PostgreSQL DDL 限制。同一来源的确切重复仍保留为
来源行，展示层才可折叠；跨来源疑似重复只能是非破坏性的 presentation cluster，并且
不能改变 provenance 或计数。

## 5. H2 导入安全协议

H2 只能按 `dry-run -> 小会话样本 -> 单月 -> 全量` 推进，顺序为 Lily 后 Nekro。每个
批次都要有可重跑的 manifest 和 checkpoint：

### 5.1 零写入导出与 manifest 合同

H2 dry-run 的唯一输入格式是 UTF-8、每行一个对象的 JSONL；不能让不同导出工具自行改变
JSON scalar 类型。Lily 展平行至少包含：字符串化的 `id`、`session_persist_id`、
`message_id`、`bot_id`、`sender_id`，整数 `scene_type`，`type`，无时区但包含完整时钟和
微秒的 UTC `time`，以及原始 `message` 和 `plain_text`。`bot_id` 来自 session 对应
botmodel 的 `self_id`；入站 `sender_id` 来自 usermodel 的 `user_id`，出站 `sender_id`
为该 session 的 `bot_id`，不能把会话中的对端用户误写成出站发送者。

Nekro 行至少包含字符串化的 `id`、`sender_id`、`chat_key`、原样 `chat_type`，来源库的
整数 epoch 秒 `send_timestamp`、带 UTC offset 和微秒且不得缺失的 `create_time`，以及
`message_id`、`content_text`、`content_data`、`raw_cq_code`、`ext_data` 等原始字段。
2026-08-12 只读 schema 核对确认 `send_timestamp` 是 32-bit integer；导出必须保持整数或
等值十进制字符串，禁止先转成 binary float。manifest 同时记录微秒级 Core cutover 与
保守的来源秒级 cutover，eligible 判断使用后者。

manifest schema 固定为 `history-dry-run-v1`。`manifest_sha256` 覆盖所有导出行和快照/
schema/映射/边界身份，并绑定上述 JSON scalar 渲染；相同快照的重跑必须复用同一导出
schema。报告至少包含 eligible/excluded/rejected 守恒计数、重复来源身份、按月、原始会话
key、群聊/私聊、方向、Lily bot 和 Nekro adapter 的计数、eligible 最早/最晚发生时间、
空文本数，以及不含消息
正文的确定性抽样。同一来源 ID 出现多次时整组拒绝，避免输入顺序决定哪一行进入历史；
`duplicates` 统计重复出现次数，是 `rejected` 的子集，不得与 `rejected` 再相加。

1. 在源库建立只读、可重复的快照或等价导出；不加表锁、不改 schema、不建索引、不改
   collation。两个来源分别取快照，不假设跨数据库原子一致。
2. 先测量目标库剩余空间、连接池、锁等待、写入延迟和分区大小；没有容量余量或备份
   恢复验证时不得开始全量。
3. 使用按来源主键的分块流式读取和 `COPY`/staging；禁止把百万级历史放进一个事务，
   禁止通过 `/v1/events` 重放。
4. 解析 JSON/segment 时隔离坏行、`\u0000`、未知类型和超长字段；每个拒绝行必须带
   原始来源身份和有限错误码。导入失败只能暂停该批次，不能回退或修改源库。
5. 重跑同一批次必须是零新增、零来源身份冲突；从 checkpoint 恢复后，前一批次的 hash、
   计数和抽样必须不变。
6. 每一步核对总量、按月份/会话/群聊私聊/方向计数、最早最晚时间、空文本、重复源键、
   拒绝原因和逐条抽样。备份恢复后这些值必须一致。

在 H2/H3 完成前，ChatExporter 继续使用原入口；不修改其 SSH tunnel、配置或旧库账号。

## 6. H0/H1 退出门和回滚

H0 已完成的判定是：来源表、主键、账号/会话关系、时间解释、固定边界、方向未知策略、
provenance 和禁止事项已由本文与 ADR 冻结，且 Git 工作树中的 H0 变更可独立审阅。

H1 只有以下四类证据全部满足后才可视为完成：

- **schema**：`0025_legacy_history_archive` 在重新确认 `0024_agent_product_flow` 单 head
  后，只创建本文规定的 `archive` 结构；不连接或写入外部旧库，不写入
  `source_events`、`event_observations`、`responses`、`event_links` 等在线热表，并包含
  ledger 到 `(legacy_message_id, occurred_at)` 的复合 FK；
- **迁移往返**：SQLite/PostgreSQL 均能完成 upgrade、回退到 `0024_agent_product_flow`
  后再 upgrade 的往返，空 archive 可以安全回退；
- **drift**：Alembic head 和 schema drift 检查通过，迁移没有未声明的分支或对象漂移；
- **非空 read-model fixture 行为**：最小非空 fixture 能证明旧消息的 `reply_hint` 被保留、
  一个 Core 观察关联的多个 `event_links` 关系行逐行保留，以及 ledger 的 imported 行
  不能引用不存在的 `(legacy_message_id, occurred_at)`。

checkpoint、拒绝隔离、批量幂等、容量验证和完整导入协议均属于 H2；它们不是 H1 已完成的
条件，必须在 H2 的 dry-run/样本/单月/全量门中单独验收。

本次 H0 不做数据库写入、迁移、备份切换或服务重启。未来 H1/H2 失败时，首选回滚是停用
archive 读取入口、保留旧库只读访问并从目标库备份恢复；不得通过删除旧库或直接删表来
“清理”失败状态。已经导入的 archive 数据在没有独立处置决定前保留为可审计证据。

## 7. H2–H4 生产验收（2026-08-27）

本节是上述合同的执行证据，不改变来源边界，也不授权 Agent、RAG 或 `history.search`
读取 archive。冻结 JSONL、manifest 和源库 dump 位于权限 `0700` 的
`/data/backups/superlily/20260827-h2-history/`；导入前后均未写入、建索引或清理旧源库。

H2 按 Lily 后 Nekro、`sample -> month -> full -> full rerun` 完成：

- Lily sample `33:100` 写入 100；2024-08 选择 22,519、复用 23、写入 22,496；full
  选择 8,262,010、复用 22,596、写入 8,239,414。最终群聊 8,261,972、私聊 38，
  inbound 7,878,503、outbound 383,507，范围为
  `2024-08-28 13:25:30+00` 到 `2026-06-19 11:44:46+00`。39 条 parse warning 全是
  有路径记录的 NUL 替换；0 拒绝、0 重复；full 复跑 `existing=8,262,010`、`writes=0`。
- Nekro sample `onebot_v11-group_928225852:100` 写入 100；2025-09 选择 68,499、
  复用 100、写入 68,399；full 选择 1,035,247、复用 68,499、写入 966,748。最终
  群聊 1,034,850、私聊 397，方向全部保持 `unknown`，范围为
  `2025-09-11 10:52:59+00` 到 `2026-06-19 11:49:10+00`；0 拒绝、0 warning、0 重复；
  full 复跑 `existing=1,035,247`、`writes=0`。
- Nekro `source_persisted_at - occurred_at` 为 58 微秒到 2.24058 秒，0 条相等，证明
  `occurred_at=send_timestamp`、`source_persisted_at=create_time` 的时间合同已实际生效。
- 两来源的逐月、群聊/私聊、方向、17,139/29 个来源会话、Lily bot 和 Nekro adapter
  聚合均与冻结 manifest 零差异；9,297,257 个 ledger 全为 `imported` 且都有复合外键
  指向实际 legacy 行。导入期间等待锁始终为 0，Core observation 延迟的短时峰值为
  16 秒并恢复到 1–5 秒，容器没有重启，最终核验时生产库约 23 GB。

H3 在 `nitori.local:/Users/justin/0Projects/chatExpoter` 的本地提交 `2906311` 完成。
Python 3.9 测试 6 项通过，日常 pyenv Python 3.14 命令直接加载该仓库；运行代码只查询
`archive.message_timeline_v2` 和 `archive.conversation_mappings`。权限切换后的真实导出
包括：群 `779593410` 跨 cutover 5,190 条、私聊 `2843657817` 123 条，以及群
`928225852` 四分钟窗口内两条带 quoted sender/text 的回复。默认展示折叠没有修改来源
provenance，`--all-sources` 可见 Lily/Nekro/Core 各行。

H4 使用 `scripts/chat_exporter_h4_access.sql` 将 `chat_exporter` 收敛为仅有 archive schema
USAGE，以及 timeline v2/mapping SELECT；`public.source_events` 与
`archive.legacy_messages` 的真实账号查询均返回权限拒绝。权限收缩后上述三种真实导出仍
通过，Nitori secret 文件保持 `0600`，配置只指向 Superlily PostgreSQL。
旧 `botmsg` 的 `config.env.bak` 已从运行配置目录移到
`/Users/justin/0Projects/chatExpoter-backups/config.env.botmsg-retired-20260827`，权限收紧为
`0600`；它只用于可逆恢复，不被代码或 tunnel 读取。

生产 post-import custom dump 为
`superlily-production-postimport-0026-20260827T151500Z.dump`，大小 2,454,207,457 bytes，
权限 `0600`，SHA-256
`940fbb46a7dbe352998bcfb587432983e60b15ca99997941bb4ee1a42f0ff3ef`。它已恢复到隔离
PostgreSQL 17 数据库 `superlily_h4_recovery_20260827`；Alembic head、两个完整
batch/checkpoint、33 个来源/月组合、ledger、mapping 和每来源 100 条确定性 payload
hash 均与生产快照完全一致，回复 fixture 也恢复为 21 行及两条完整引用。恢复库保留，
旧 Lily/Nekro 数据库和只读备份同样保留；删除旧库仍需另一次明确的数据处置决定。
