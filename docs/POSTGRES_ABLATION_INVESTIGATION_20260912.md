# Superlily PostgreSQL 写入与深层 Ablation 调查

日期：2026-09-12；下文现场时间使用北京时间，LSN 日志时间另注明 UTC。

后续获批实施及实验结果见 `POSTGRES_WRITE_ABLATION_20260912.md`；本文保留调查时的测量窗口与结论。

## 结论与范围

本轮只调查。未修改应用代码、生产配置、数据库参数或表结构；未重启服务、重置统计、执行手动 checkpoint、VACUUM、REINDEX 或删除数据。只新增本报告和临时诊断脚本/统计文件；另用内存 SQLite 做了合成 ORM 实验。

结论：Core PostgreSQL 确有可削减的重复更新和索引 WAL 成本，但当前证据不支持它独自造成整盘每天 200 多 GB 写入。不能进一步推出“Superlily 完全没有贡献”，也不能把 Core PG 的计数当成 Lily、Nekro、QQ/NapCat 以及整个文件系统的总成本。

- 近 24 小时 checkpoint 日志中的 redo LSN 跨度为 **2,624,345,328 B（2.624 GB）**；相邻这批 checkpoint 日志累计写出 **2,408,259,584 B（2.408 GB）** 数据页。
- 5 分钟现场样本新增 WAL **5,644,506 B**，没有新增临时文件；已捕获 PG 进程 `write_bytes` 合计 **21,082,112 B**，同期整盘 **1,132,843,008 B**。
- 另一个 60 秒样本，PG 容器 cgroup 归属块写入 **31,477,760 B**，整盘 **342,528,000 B**，占 **9.19%**。
- 这 60 秒 WAL 解码中，整页镜像（FPI）占 **92.70%**，可归属索引的记录占 **93.02%**。两个比例有重叠，不能相加；也不是物理盘写入比例。
- 最明确的应用冗余：68 条 ingress 回执触发 136 次采集水位 UPDATE；两行 `bot_instances` 在 5 分钟内更新 171 次。

优先方向：先合并同一事务内的重复 UPDATE；对 WAL 整页镜像压缩做独立对照；再审计特定冗余索引和非消息派生决策。不是继续关闭已冻结组件，也不是牺牲采集、去重或持久性。

## 1. 实际运行对象与代码一致性

- PostgreSQL：`deploy-postgres-1`，17.10，启动于 2026-09-09 07:11:19 UTC；数据库 `superlily` 约 26 GiB（`pg_size_pretty` 显示 26 GB）。
- 数据卷：`/var/lib/docker/volumes/deploy_superlily_postgres_data/_data`。
- Core：`deploy-lily-core-1`，镜像 `sha256:2d57b4c23a8ba6bc5f1f8b87f8c91b401e2d4fb6fddacd4d76b3e2a3f19d1a59`；2026-09-12 03:55:44 UTC 启动，检查时 restart count 为 0。
- 实际导入的是 `/usr/local/lib/python3.13/site-packages/superlily_core/service.py`，不是容器 `/app/apps/...` 的源码副本。实施前进一步核实，实际模块 SHA-256 `670e61857591d1f5da77ee01fddba9ef2d7b7721e5435d440d2f76eed456d668` 与调查时的工作区一致；`models.py`、`app.py` 也一致。工作区其他改动仍不能据此推定已部署。
- 生产模式仍是 Agent off / product off、tool ledger_only、Renderer all；此前的 claim 消融仍生效。本轮 5 分钟 `event_claims` 新增/更新/删除均为 0，但仍有 58 次索引扫描，不等于完全不读取。

关键参数：

| 参数 | 实际值 |
|---|---|
| `fsync` / `full_page_writes` / `synchronous_commit` | 全部 on |
| `wal_compression` | off |
| `checkpoint_timeout` / completion target | 300 秒 / 0.9 |
| `max_wal_size` / `min_wal_size` | 1024 / 80 MiB |
| `shared_buffers` | 128 MiB |
| `track_io_timing` / `track_wal_io_timing` | off / off |
| `shared_preload_libraries` | 空；没有 `pg_stat_statements` |
| autovacuum / analyze threshold / scale factor | on / 50 / 0.1 |

PG 编译选项支持 lz4、zstd；没有复制槽或归档器活动。没有逐语句长期统计，因此本文不冒充完整的 SQL 排行榜。计时开关关闭时，IO time 为 0 不能解释为没有 I/O。

## 2. 写入量的测量口径

### 2.1 接近一天的 checkpoint 日志

2026-09-12 05:05:14 UTC 读取过去 24 小时容器日志，解析到 288 条 checkpoint complete：

- 首条完成于 2026-09-11 05:08:33.788 UTC，redo LSN `37/14287298`。
- 末条完成于 2026-09-12 05:04:05.206 UTC，redo LSN `37/B094C788`。
- 两条 redo LSN 相差 2,624,345,328 B。这是 redo 点之间的 WAL 地址跨度，包含 WAL 格式开销；完成时间不是 redo 点的精确发生时间。
- 288 条日志的 buffers written × 8192 合计 2,408,259,584 B；这一求和与上述 LSN 区间的边缘并非完全重合。
- 大多为每 5 分钟的定时 checkpoint。最大一条 distance 51,484 KiB，写 6991 个 buffer。
- 日志的 write 阶段可持续接近 269 秒：在 completion target 0.9 下是分散写入的过程，不能说数据库卡死 269 秒；需要另看 sync 时间、请求延迟。

这两类计数**不能简单相加后宣称就是 PG 的全天物理盘写入**：尚有 WAL 页写入对齐/重写、文件初始化、其他进程写脏页、文件系统 COW/元数据、延迟写回等差异。

原维护报告的约 267.2 GB 主机写入窗口为 9 月 11 日 11:40 至 12 日 11:40，和这里日志窗口不同。没有同窗口全天 cgroup/文件系统归因，不能计算精确全天百分比。

### 2.2 五分钟统计与进程差分

窗口：13:03:31.080—13:08:31.186，约 300.10 秒；LSN `37/B0B450E0` → `37/B10AD2E0`。

| 指标 | 增量 |
|---|---:|
| `pg_stat_wal.wal_bytes` | 5,644,506 B |
| WAL records / FPI | 2700 / 876 |
| WAL write / sync | 135 / 133 |
| WAL buffers full | 0 |
| 定时 / 请求 checkpoint | 1 / 0 |
| checkpoint 写出数据页 | 827 × 8192 = 6,774,784 B |
| PG 已捕获进程 write_bytes | 21,082,112 B（20.105 MiB） |
| 整盘 nvme0n1 写入 | 1,132,843,008 B（1080.363 MiB） |
| Superlily temp files / bytes | 0 / 0 |
| Superlily commit / rollback | 285 / 562 |
| deadlocks / sessions fatal | 0 / 0 |

每 2 秒枚举 postmaster 子进程，短命 backend/autovacuum 可能遗漏，因此进程合计是有遗漏风险的已观测量，不是全盘物理归属上限。数据库统计刷新也存在边界延迟。

约 0.95 commit/s、1.87 rollback/s 包含诊断连接及正常只读会话收尾。代码的 AsyncSession 退出路径没有为只读 SELECT 主动 commit，因此 rollback 不能直接视为业务失败；本轮不把它称为“错误风暴”。合并 UPDATE 也不等于减少已经只有一次的事务 commit。

### 2.3 独立 60 秒 cgroup 与 WAL 样本

窗口：13:15:01.582—13:16:01.628；已 flush 的 LSN `37/B16F5488` → `37/B18AADE8`。

- PG cgroup：`/sys/fs/cgroup/system.slice/docker-641b1f298a39fc81f9d6c91dbc35631699571488a7cb2e9dfbf094757e06ca37.scope`。
- `io.stat` 的 `259:0 wbytes` 增加 31,477,760 B，wios 增加 1891。
- 同期整盘增加 342,528,000 B，归属于该 cgroup 的计数占 9.19%。cgroup 归属并不保证包括全部共享文件系统间接开销，且写回可能对应窗口前产生的脏页。
- WAL 解码 864 条，总记录与 FPI 合计 1,784,640 B；FPI 1,654,304 B，占 92.70%；单一索引关系可归属记录 1,660,021 B，占 93.02%。
- 所有解码出的关系引用都属于数据库 OID 16384（superlily）；没有把其他数据库的表误记到它名下。
- 其他五个遗留测试数据库在这一分钟各增加 2 次 commit，不能称为绝对零活动；该增量本身不证明它们发生业务写入或构成热点。本轮保留这些数据库。

WAL 部分记录按“关系引用集合”归组，跨关系记录不重复归属；无关系记录另列。未输出消息正文、原始 tuple 或整页内容。

最初在 13:03:31 起的早期子窗口已经成功解码 1556 条记录，FPI 占 92.15%。随后想补全五分钟 WAL 时，起始 B0 段已被正常回收，无法重解码；因此**五分钟统计保留，但不能把 92.70% 冒称为整个五分钟或全天的比例**。后续一分钟在结束时立即完成了解码。

## 3. 可以深入消融的具体对象

### A. 合并采集水位的同事务双 UPDATE：高确定性、小范围

现场 68 条 `ingress_receipts` 对应 136 次 `collector_watermarks` UPDATE，其中 68 次 HOT、68 次非 HOT。

原因在 `service.py:_advance_collector_watermark`：先赋值 highest_seen / last_receipt / updated_at，再查询普通和历史回执的 UNION；ORM 自动 flush 第一组变更；随后推进 highest_contiguous，提交时又写一次。

已在内存 SQLite 中运行与生产 AST 一致的函数，用 SQLAlchemy 事件监听实际 SQL。回执先 flush，再比较原函数与调用期间局部 no_autoflush：

| 合成用例 | 原 UPDATE 数 | 延后 flush 后 | 最终 contiguous / seen |
|---|---:|---:|---|
| 连续序号 1 | 2 | 1 | 1 / 1，结果相同 |
| 缺口，直接收到 3 | 1 | 1 | 0 / 3，结果相同 |
| 历史回执 1 + 普通回执 2 | 2 | 1 | 2 / 2，结果相同 |

建议实现时计算局部变量后一次赋回，或严格限定 no_autoflush 范围；不能全局禁用 flush，也不能让尚未落到当前事务中的新回执对补洞查询不可见。必须保留每个 instance/spool 的串行保护、两种回执联合补洞、提交成功后才返回可靠回执。

这只是 ORM 机制实验，不是 PostgreSQL 并发、首次建水位、乱序重投、事务回滚或断电恢复的完整验收。[SQLAlchemy flush 说明](https://docs.sqlalchemy.org/en/20/orm/session_basics.html#flushing)

### B. 去掉实例元数据的相同值 UPSERT：高确定性、小范围

`ensure_instance` 每次执行 `ON CONFLICT DO UPDATE`，平台、adapter、bot_id、role、display_name、version 没变化也更新；事件或心跳随后又更新 last_event/last_heartbeat 等状态。

两行 `bot_instances` 在五分钟内 UPDATE 171 次，全部 HOT；期间 autoanalyze 4 次。可以研究条件 UPSERT（逐字段 IS DISTINCT FROM）或把元数据和确有变化的心跳状态合为一次更新，保留创建竞争、状态转换记录与心跳新鲜度。

条件 UPSERT 即使不改值也可能锁行，因此不能承诺完全零 WAL；现有 HOT 已避免大多数索引维护。它是确定的冗余治理，不是整个磁盘大头。

### C. 水位更新时间索引：明确候选；不要泛化为“零命中索引都删”

`ix_collector_watermarks_updated(updated_at)`：56 KiB、idx_scan=0、非唯一。表只有两行；真实 `/v1/ingress/watermarks` 查询的 EXPLAIN 是 Seq Scan + Sort，而不是该索引。

每次更新时间改变会妨碍 HOT。副本中移除该索引并结合 A，比只调整填充因子更贴近当前原因；仍需检查未来多 spool 规模与升级迁移，并保存重建 DDL。[PostgreSQL HOT 条件](https://www.postgresql.org/docs/17/storage-hot.html)

反例：`ix_event_decisions_updated_at` 约 52 MiB、idx_scan 同样为 0，但 `/v1/decisions/recent` 的 EXPLAIN 明确使用它做反向 Index Scan、取最近 100 条。不能删它换取更便宜 UPDATE 后让百万行最近查询退化。

收益必须分开计：在后一分钟中，水位表 WAL 4091 B、其 updated 索引 1532 B，实例表 WAL 10563 B；这些明显小于该分钟 1.785 MB WAL 总量。减少一半水位 UPDATE **不等于减少一半 PG 或整盘写入**。

### D. WAL 整页镜像压缩：最值得独立测量的 WAL 大头优化

当前 wal_compression=off。两个 WAL 子样本均有约 92% 字节属于 FPI，大头关系包括 event_observations、ingress_receipts、source_events、event_decisions 的主键、去重和关联索引。

其中许多索引是幂等性/引用关系的保障，不应直接消融。优先在隔离 PG 副本比较 off / lz4（必要时 zstd），同时保持 fsync、synchronous_commit、full_page_writes 全开。PG 支持压缩 FPI，并在恢复时解压；代价是记录和恢复阶段的额外 CPU。**92% 是可压缩对象占比，不是预计节省率。** [PostgreSQL WAL 参数](https://www.postgresql.org/docs/17/runtime-config-wal.html)

第二个独立变量才是 checkpoint_timeout，例如副本测试 5 分钟与 15 分钟，观察完整周期 WAL、块写入及恢复时间。随机键插入未必反复访问同一页，延长周期不保证同比减少 FPI。max_wal_size、磁盘余量和恢复时长要一起验收，不能只改数字。

### E. 非消息事件的派生决策：更深入，但存在契约变化

五分钟内新建 64 条 source event / decision，其中 53 条 message→observe_only、10 条 meta_event.heartbeat→ignore、1 条 notice.group_recall→ignore。

当前 `decisions.py` 对非 message 明确返回 non_message_event/ignore，但调用路径仍读取观察、关联、注册表并持久化决策。可以分两级研究：

1. 保留现有 decision/审计契约，只把非消息路径提前判定，跳过不会改变结果的重计算；必须逐项核对 features 与调用副作用。
2. 对纯心跳等非触发事件不再持久化冗余派生 decision，保留原始 source/observation/receipt。这会改变决策查询与审计覆盖，必须单独产品确认，不能现在直接做。

不能把撤回、历史回执、状态事件混在一起全丢；也不能取消 raw capture、回执或消息持久性。两实例的观测汇聚会正当改变 provenance/decision revision，不能仅因为 action 仍为 observe_only 就跳过所有决策更新。

### F. 两行小表的 autoanalyze：次级放大，不是第一刀

水位表五分钟 autoanalyze 2 次、autovacuum 2 次；实例表 autoanalyze 4 次。默认 analyze threshold 50 对频繁更新的小表很敏感。早期 WAL 子窗口中 pg_statistic 占 52,195 B（约 1.65%），可以看到统计目录更新成本。

先做 A/B 减少冗余更新；再在副本评估仅这些小表的 analyze threshold。不能全局关闭 autovacuum，也不能因行数少就忽略死元组清理或事务 ID 冻结。

### G. 历史索引与容量：新的深层候选，不能混作当前写入收益

最大的 `archive.source_message_identities` 总计约 6314 MiB，其中索引约 3440 MiB，约千万行；档案 schema 在五分钟统计中没有表读写/维护增量，WAL 子样本也未出现其关系。

值得副本验证的重叠：

- `ix_archive_source_message_identities_legacy_message(legacy_message_id, occurred_at)` 约 **847 MiB**，累计 idx_scan=2。
- 已有 `uq_archive_source_message_legacy_id(legacy_message_id)` 唯一索引约 734 MiB，累计 idx_scan 约千万；对非空 legacy_message_id 至多定位一行，理论上可再过滤 occurred_at。
- **但**实际复合外键检查形状的 EXPLAIN 当前选择 847 MiB 复合索引。因此它不是已证明可直接删除的孤儿索引：必须在副本移除后验证外键检查、时间线/恢复查询的替代计划与成本，包括 index-only 场景。

保留源身份复合主键、legacy id 唯一约束及指向 `(legacy_message_id, occurred_at)` 的真实复合 FK；它们承担跨分区幂等和恢复完整性。候选仅是非唯一辅助索引，不是账本或约束。其他按 import_batch_id 的分区索引也有恢复/审计消费者，不能因低命中就整批删除。

`event_claims` 虽占约 1404 MiB，但当前无新增写入。清历史行可能涉及审计/去重并产生大量 DELETE WAL，既不是本轮授权，也不是治理当前写入的优先方向。

### H. 暂不推进的更大改造

- 新生成的随机 UUID 主键可能加大 B-tree 页面分散，未来可以对仅内部 opaque ID 做时间有序键的副本对照；不能改 deterministic source/correlation 身份、重写历史主键或承诺未经测量的收益。
- Provider 心跳/凭据 last_authenticated_at 也写库，但这五分钟只有 10 条 heartbeat、11 次凭据更新；若改“最新状态 + 稀疏历史”，需要重新定义健康新鲜度与审计，不是首要对象。
- Btrfs 上 WAL 初始化/回收选项值得独立实验，但不能把一分钟 cgroup/WAL 的比值当成确定放大系数，更不据此直接迁移卷或修改文件系统策略。

## 4. 已排除或必须保留

- Agent product 循环和 tool lease reaper 在当前 off / ledger_only 模式下不进入 DB 工作分支；循环存在不等于一直写库。
- Artifact reaper 每 30 秒仍有检查。本轮表扫描可见，但制品仅两条且内容已删除，未见该表 WAL；不能宣称它是当前写入热点。未来 SELECT FOR UPDATE 即使没有 UPDATE 也可能产生 WAL，需按实际候选判断。
- 不关闭 fsync / full_page_writes，不将已确认消息的事务改成异步不可靠提交，不使用 unlogged 消息表，不删 spool/receipt/去重约束，不弱化 H2/H3 恢复证据。
- 不把累积 211 GB WAL、58.4 GB temp 或数据库 26 GiB 体积当成每天写入。没有证据显示本样本正在发生临时文件洪峰、复制槽积压或 forced checkpoint 风暴。

## 5. 建议的下一阶段验收顺序（尚未实施）

1. 在隔离测试库单独验证 A+B：每事件 UPDATE 数、回执提交边界、双实例竞争、重投、缺口/历史补洞、失败回滚和重启恢复；随后才申请小范围部署。
2. 以相同语料、相同数据规模和多个 checkpoint 周期做 off/lz4 对照：记录 WAL/事件、FPI 字节、cgroup 块写、CPU、提交 P95/P99 与 crash recovery 时长。再单独改变 checkpoint 周期，避免多个变量混改。
3. 在副本验证 C 与 G 的索引移除，覆盖所有消费者、外键检查、重建时间与回滚 DDL；索引删减与应用优化分批。
4. E 的派生决策删减先做契约/审计评审。观察期连续至少覆盖完整业务高低峰；每条已 ACK 消息仍必须能够从 PG 查到可靠回执，重投不得产生额外 canonical event。
5. 如果目标仍是解释整盘 267 GB，需另做同窗口完整日的分容器/设备归因。本报告已经深入 Core PG，但不代替全机调查，也未自动启动长期监控。

## 证据文件

- 本机维护：`/home/justin/maintenance-2026-09-12/result-and-storage-investigation.md`。
- 上一轮：`docs/SQLITE_WAL_INVESTIGATION_20260912.md`、`docs/ABLATION_20260912.md`。
- 采样：`/tmp/superlily-pg-survey-20260912.json`、`/tmp/superlily-pg-cgroup-20260912.json`。
- WAL 聚合：`/tmp/superlily-pg-wal-relations-20260912.json`（早期子窗口）、`/tmp/superlily-pg-wal-cgroup-20260912.json`（完整一分钟）。
- 诊断脚本：`/tmp/superlily-pg-write-survey.py`、`/tmp/superlily-pg-checkpoint-history.py`、`/tmp/superlily-pg-wal-relations.py`、`/tmp/superlily-pg-cgroup-survey.py`、`/tmp/superlily-pg-code-check.py`、`/tmp/superlily-pg-orm-probe.py`。

临时证据可能被系统清理；关键参数、窗口、数值、局限和结论已在本报告固化。所有本轮采样均已结束，没有遗留常驻 tracer 或新服务。
