# ADR 0018：旧历史的统一 read model

状态：accepted，2026-08-01。

## 背景

当前 Lily/NoneBot 的 `botmsg` 和 Nekro 的 `nekro_agent` 是两套仍在运行的历史库。
2026-08-01 的只读核对显示，Lily chatrecorder 有 8,742,661 行，Nekro `chat_message`
有 1,205,359 行；两者各自有明确的 Core 切换边界，但 schema、时间类型、会话 key 和
方向语义不同。Superlily 当前 PostgreSQL 是在线事件热库，不能把旧行重放成 Core 事件。

`docs/HISTORY_DRY_RUN.md` 已证明旧历史没有可靠的跨账号 `real_seq`，而且历史数据量和
当前热库相当甚至更大。直接按文本和时间去重会丢失 provenance，也会把不确定性伪装成
事实。

## 决定

1. 将历史统一实现为当前 Superlily 数据库中的独立 `archive` schema，而不是污染
   `public` 在线热表或另建一个运行时依赖库。
2. H1 只创建 `import_batches`、`conversation_mappings`、按月分区的
   `legacy_messages`、跨分区来源身份 ledger `source_message_identities` 和版本化只读
   timeline 入口；H1 迁移不连接、读取或写入外部旧库，也不写入在线热表；H2 才在只读快照上分批导入。由于 PostgreSQL 分区表的唯一约束
   必须包含分区键，来源三元组的精确唯一性由 ledger 强制；ledger 的 `imported` 行必须以
   `(legacy_message_id, occurred_at)` 到 `legacy_messages(id, occurred_at)` 的真实复合
   外键约束落到同一条消息上。
   timeline 必须保留同一 Core 观察关联的多个 `event_links` 关系行，不得只取一条；旧
   消息的 `reply_hint` 也必须在 legacy timeline 中保留。
3. 每条旧消息永久保留 `(source_system, source_table, source_record_id)`、原始会话 key
   和映射版本。跨源相似消息不得变成一个 canonical event；最多在展示层形成 cluster。
4. 使用固定的、严格小于的两个 UTC cutover boundary。对 Nekro，平台消息发生时间
   `send_timestamp` 映射为 `occurred_at`，数据库落库/捕获时间 `create_time` 映射为
   `source_persisted_at`/`captured_at`；缺失 `send_timestamp` 的行必须拒绝并记录有限错误，
   不能静默回退 `create_time`。边界后的源行不导入，当前 Core 事件继续由现有 ingestion
   链负责。
5. 无法证明方向、时间、segment 或身份的字段采用 `unknown`/`NULL` 加有限
   `parse_warning`，不得依靠文本、sender 名或时间邻近猜测。
6. 历史 read model 只服务人类查看、查询和导出；不得自动进入 AgentRun、Nekro 上下文、
   `history.search`、`memory.lookup` 或默认 RAG。
7. ChatExporter 在 H3 前不切换；H4 通过后才允许它只读 timeline/API。旧库和其只读备份
   在另一个明确的数据处置决定前保留。
8. H1 的退出门只验收 schema、迁移往返、drift 和非空 read-model fixture 行为；checkpoint、
   拒绝隔离、批量幂等、容量验证和导入协议全部属于 H2，不作为 H1 已完成条件。

详细的源表、时间转换、计数基线和导入协议见 `docs/HISTORY_UNIFICATION.md`。

## 后果

- 需要一次新的 archive schema 迁移和一个有 checkpoint 的、分来源分批次导入器；不能复用
  当前 `EventIn` dry-run helper 作为写入器。
- H2 会占用额外磁盘和 I/O，必须先做容量、锁、连接池和恢复测试；旧库在线时只读冻结边界
  前的数据。
- 展示层会同时面对精确来源重复和跨源疑似重复，必须把折叠标记与 provenance 分开；同一
  Core 观察的多个 `event_links` 关系行和旧消息 `reply_hint` 也必须可见。
- 历史导入不会扩大任何 Agent、工具、平台发送或写操作 authority。

## 实施顺序

```text
H0 本 ADR + HISTORY_UNIFICATION.md
  -> H1 0025 archive schema
  -> H2 dry-run / sample / month / full import
  -> H3 ChatExporter timeline-only read
  -> H4 old database dependency exit
```

H1 留下 schema、迁移往返、drift 和非空 fixture 的证据；H2 的每个导入批次再分别留下
来源 manifest、批次计数、拒绝原因、抽样、checkpoint、幂等和恢复证据。

## 回滚

H0 本身无运行时副作用。H1 失败时停用 archive 读取入口，保留旧库和目标库备份；空
archive 结构可由迁移的受控 downgrade 处理，但含有导入证据后不得自动 `DROP SCHEMA`。
H2/H3 失败时回退 ChatExporter 到原只读来源，保留 archive 批次和旧库只读访问，不删除
已导入行。只有新的数据处置 ADR 才能改变这一保留策略。
