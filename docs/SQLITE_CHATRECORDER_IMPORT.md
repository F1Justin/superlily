# SQLite chatrecorder 历史归档合同

状态：accepted，2026-09-02。

本合同将三份 2022–2023 年 NoneBot chatrecorder SQLite 快照纳入现有
`archive` read model。它不重放在线事件，不修改原始 SQLite，不向名称历史写入无法证明的
昵称或群名，也不修改 ChatExporter 的 `archive.message_timeline_v2` 读取合同。

## 冻结来源

| CLI 来源 | `source_system` | schema revision | SHA-256 | 总行数 | 可导入 | 拒绝 |
| --- | --- | --- | --- | ---: | ---: | ---: |
| `sqlite-data` | `lily.nonebot.chatrecorder.sqlite.data1` | `2cad88d938f1` | `9743f83064b830c6a45509fd331ea00afec3f7bd68aad498355b492e3d80fc28` | 228,166 | 228,166 | 0 |
| `sqlite-data2` | `lily.nonebot.chatrecorder.sqlite.data2` | `9bca28bcb998` | `62fa3fa56b436334d011e2c586e0ef8f50f0f3c41e4750ea5153e023f3ebf880` | 172,159 | 171,944 | 215 |
| `sqlite-data3` | `lily.nonebot.chatrecorder.sqlite.data3` | `9bca28bcb998` | `5ab9c40766430c625e6c25bbf2760bee3d7003bac10e426030196f020b3961bd` | 341,860 | 341,854 | 6 |

三个数据库的 `id` 都从 1 重新开始，所以必须保留三个不同的 `source_system`。快照身份
同时绑定上述文件 hash；不能只依赖可重命名的文件名。

固定来源边界为当前 PostgreSQL chatrecorder v2 最早记录：
`2024-08-28 13:25:30+00`，导入谓词严格为 `time < boundary`。三份快照最晚记录为
`2023-11-13 14:44:47+00`，因此与现有 v2 archive 没有时间重叠。

SQLite `time` 是无时区列，按 UTC 解释。依据是插件的 UTC 时间合同以及三份快照一致的
小时活动分布；导出器不按主机本地时区转换它。

## 字段和会话映射

- `id` 是来源身份；`message_id` 只是平台消息 ID，不能作为幂等键。
- `message` 保留为 segments；旧 schema 的 `alt_message` 和新 schema 的 `plain_text`
  统一映射到 `content_text`。
- `message` 为 inbound，`message_sent` 为 outbound。
- 群聊 `conversation_id=group_id`。
- 私聊 inbound 的 `conversation_id=user_id`。
- outbound 的 `sender_id=bot_id`；inbound 的 `sender_id=user_id`。
- `data.db` 没有可证明的 bot ID，相关 inbound 行保留 `bot_id=NULL`。
- `source_persisted_at` 无来源字段可证明，保持 NULL。
- reply segment 只在同一来源会话和 bot 范围内解析。

`data2` 和 `data3` 的 221 条 outbound private 行只记录了 bot 自己的 `user_id`，没有
对端 QQ。它们以有限错误码 `missing_private_peer_id` 进入
`archive.source_message_identities` rejected ledger，不建立虚假私聊会话。

源库不包含昵称、群名片或群名称字段，因此本导入不写
`identity_name_observations`/`conversation_name_observations`。

## 执行协议

1. 使用 `sqlite_history_export` 以 `mode=ro&immutable=1` 打开源库，验证 SHA、
   `PRAGMA quick_check`、表列和 Alembic revision，再生成 mode `0600` JSONL。
2. 使用 `history_import legacy` 生成 write-free manifest，核对总数、月份、会话、方向、
   bot、拒绝码、时间范围和 manifest hash。
3. Alembic `0028_sqlite_chatrecorder_archive` 先把三份冻结来源加入 archive allowlist，并建立
   2022-12 至 2023-11 月分区。生产 default 分区必须在迁移前后都为空。
4. 每个来源依次执行 `sample -> month -> full -> full rerun`，使用原有 checkpoint、
   bounded chunk 和 source identity ledger。full rerun 必须 `writes=0`。
5. 导入期间监控等待锁、Core observation 延迟、容器健康和磁盘，不暂停在线采集。
6. 验证三个 completed batch、741,743 imported、221 rejected、月份/会话聚合、复合 FK、
   timeline 数量和真实 ChatExporter 导出。
7. 创建 post-import custom dump，并恢复到新的隔离 PostgreSQL 17 数据库比较 batch、
   checkpoint、hash、来源/月计数和代表性导出结果。

原始 SQLite 和导出 manifest 是恢复证据；没有独立数据处置决定不得删除。

## 生产验收（2026-09-02）

- 实现提交 `952608730aa8aaaa5988259ddd48879c7f81b515` 已推送到
  `F1Justin/superlily` 的 `master`。生产 Core 使用该源码构建并迁移到
  `0028_sqlite_chatrecorder_archive`，PostgreSQL 和桥接未重启。
- 三个来源均完成 `sample -> month -> full -> full rerun`。最终导入分别为
  228,166、171,944、341,854；`data2`/`data3` 分别以
  `missing_private_peer_id` 拒绝 215/6 条。三个 full rerun 都是 `writes=0`。
- 生产 archive 最终为 10,039,221 条，其中新来源 timeline 741,743 条；110 个历史
  会话，复合 FK 缺失 0，default 分区 0，导入结束时锁等待 0，Core healthy。
- Nitori 的 ChatExporter 保持未修改的
  `6aca5b345b755a9d33a1c67609865bf1479d84e9`，从群 `1080353942` 成功导出
  2022-12-04 的 84 条记录（14,468 bytes）。
- 恢复证据位于
  `/data/backups/superlily/20260902-sqlite-chatrecorder/`。生产前 custom dump 的
  SHA-256 为
  `b8eda7601cdd26e4c281e8c945ac74505b94480569c57e39f465374487b8ca4e`；生产后
  custom dump 为
  `fc78afadd7fef4939bc18e6f95f212ff9f8190bc1e7766ed655b626dcd368568`。
- 生产后 dump 已用 `pg_restore --exit-on-error --no-owner --no-privileges` 恢复到全新
  PostgreSQL 17 数据库。恢复库的 revision、三个 batch、imported/rejected、timeline、
  broken FK、default 分区和 archive 总数均与生产一致。
