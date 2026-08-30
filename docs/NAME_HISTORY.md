# QQ 名称观测历史

`0027_name_observation_history` 将名称作为带来源的观测事实保存，而不是从消息表临时猜测。
它不修改 ChatExporter，也不向 ChatExporter 的只读角色授予新对象。

## 语义

- `account_name`：QQ 账号昵称；同一 QQ 号跨群共享。
- `conversation_display_name`：群名片/该会话显示名；按 QQ 号和会话分别保存。
- `effective_display_name`：旧采集链只能证明的最终显示名，不冒充账号昵称或群名片。
- 群名称保存在 `conversation_name_observations`。两个 bridge 启动后立即拉取群清单，之后默认每
  6 小时再观测一次；消息和群名称变更事件也会使用缓存或主动刷新后的群名。
- `observed_at` 表示来源真正观察到该值的时间。旧 Lily join 得到的成员名只标为
  `legacy_join_snapshot`，时间取导入快照时间，原消息时间仅放在 provenance 中。

每行包含 `source_system`、`source_record_type`、`source_record_id`、`observation_method` 和
`provenance_json`。因此查询结果能区分精确消息时观测、当前清单快照和旧数据快照。

## 按 QQ 号查询

```sql
SELECT
    observed_at,
    name_kind,
    name_value,
    conversation_type,
    conversation_id,
    observation_method,
    source_system
FROM archive.identity_name_timeline_v1
WHERE platform = 'qq' AND user_id = '目标QQ号'
ORDER BY observed_at, id;
```

把结果中的群号关联到同一时刻最近一次已观测群名：

```sql
SELECT identity.*, group_name.name_value AS conversation_name
FROM archive.identity_name_timeline_v1 AS identity
LEFT JOIN LATERAL (
    SELECT name_value
    FROM archive.conversation_name_timeline_v1 AS candidate
    WHERE candidate.platform = identity.platform
      AND candidate.conversation_type = identity.conversation_type
      AND candidate.conversation_id = identity.conversation_id
      AND candidate.observed_at <= identity.observed_at
    ORDER BY candidate.observed_at DESC, candidate.id DESC
    LIMIT 1
) AS group_name ON true
WHERE identity.platform = 'qq' AND identity.user_id = '目标QQ号'
ORDER BY identity.observed_at, identity.id;
```

没有任何采集账号在场时发生又恢复的改名仍不可知；数据库明确表达“已观测历史”，不会伪造完整
QQ 服务端历史。

## 历史回填

迁移只建表和视图，不在 DDL 中读取外部数据库。迁移、Core 和 bridge 部署健康后，再运行：

```bash
SUPERLILY_DATABASE_URL='postgresql+asyncpg://...' \
SUPERLILY_NEKRO_DATABASE_URL='postgresql+asyncpg://只读用户:...@.../nekro_agent' \
python -m superlily_core.name_history_backfill \
  --snapshot-id name-history-YYYYMMDD \
  --cutover 2026-08-31T06:53:00+08:00 \
  --nekro-source-cutover 2026-06-19T11:45:17.17105+00:00
```

回填会记录独立 batch、状态、游标和计数。Core 与 H2 archive 各自幂等回填；Nekro
post-cutover 源按 `send_timestamp,id` 每 10,000 行提交并可从 checkpoint 续跑。源库连接只读，
不会从 `/v1/events` 重放，也不会改写历史消息。
