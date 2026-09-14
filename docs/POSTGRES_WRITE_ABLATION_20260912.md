# PostgreSQL 重复写入消融：实施与验收

2026-09-12，用户在调查后授权“照你说的做”。本轮范围：生产部署水位/实例重复更新修复，隔离实验 WAL 压缩；不删除索引或历史，不修改生产 PostgreSQL 参数。

## 最终行为

- `ensure_instance` 的 PostgreSQL / SQLite UPSERT 仅在 platform、adapter、bot_id、role、display_name、version 中至少一项发生 NULL-safe 差异时更新已有行。实例首次注册和元数据变化仍持久化；真实心跳时间、状态转换、first_seen 与 pending ORM 状态保留。
- `_advance_collector_watermark` 在读取普通/历史回执、计算连续水位后一次赋回。避免查询触发 autoflush，把一次水位推进拆成两次 UPDATE。首次创建水位仍先 INSERT，随后一次 UPDATE。
- 保留 receipt flush、instance/spool advisory transaction lock、幂等和冲突校验、普通/历史回执联合补洞及原 commit/ACK 边界；没有全局关闭 autoflush，没有跨消息缓存或延迟确认。
- 仅修改 `apps/core/src/superlily_core/service.py` 的这两个函数。没有 schema migration；新建回归文件 `tests/test_ingress_write_amplification.py`。

## 测试

- SQLite 相关回归：**198 passed，2 skipped**；跳过项为 PostgreSQL 专用并发锁测试。
- 独立 PostgreSQL 17.10：**135 passed**，含新测试的 14 项、API、claim/ACK、durable spool 和 history recovery。
- 覆盖相同值 UPSERT 的实际 rowcount=0、六个字段分别变化、可空字段清空、pending ORM 状态保留、连续/缺口/历史补洞一次 UPDATE、重复回执零水位 UPDATE、提交前失败全部回滚、连接池重建后可见、首次注册竞争、同 spool 并发乱序、过期心跳不覆盖新状态。
- 新增测试在旧代码上能捕获冗余，修复后通过。测试使用临时 SQLite 或独立 PG 容器，没有使用生产数据库做 ORM create/drop schema。
- 新镜像实际 import 路径/哈希及 `pip check` 通过；目标补丁与工作区的既有变更分别审阅。

### 强制终止恢复验证

独立容器 `superlily-pg-ablation-lab-20260912`，同生产 PG 17.10；数据卷独立，仅包含合成数据，端口仅绑定 `127.0.0.1:55439`。

分别以 wal_compression=off / lz4 运行真实 Core ingest：

1. 第一条事件已 commit，记录其 receipt ID。
2. 第二条事件已执行写入，但在调用 commit 前阻塞。
3. 对**实验容器** SIGKILL，然后启动并重新连接。
4. 第一条回执仍在，第二条回执不存在，水位仍是 1/1。
5. 第一条重投被识别为 duplicate；第二条重试成功，最终两条回执、水位 2/2。

两组均通过。容器启动到可连接分别约 1.824 / 1.818 秒，仅代表小型实验库；不是 26 GiB 生产库恢复时间或真实断电/硬件缓存可靠性验收。生产 PostgreSQL 未重启。

## WAL 压缩实验：已完成，未改生产参数

固定随机种子，先建 30,000 组 source / observation / receipt / decision 的合成数据及典型主键、去重、关联索引；每组从同一模板复制，追加相同 750 组事件，每个事件一个同步事务，覆盖三个显式 checkpoint 周期。

采用 off → lz4 → off 对照，三组均保留 fsync / full_page_writes / synchronous_commit=on。模板复制与填充不计入下表；块写入包含各周期结束的 checkpoint。

| 指标 | off 第一次 | lz4 | off 复验 |
|---|---:|---:|---:|
| 新增事件组数 | 750 | 750 | 750 |
| WAL LSN 增量 B | 35,409,776 | 29,605,528 | 35,409,776 |
| FPI B | 32,635,728 | 26,845,730 | 32,635,728 |
| 容器归属块写入 B | 160,886,784 | 142,147,584 | 155,418,624 |
| 容器 CPU usec | 705,465 | 577,762 | 703,408 |
| 各周期事务 P95 ms | 4.06–4.09 | 4.06–5.99 | 4.08–6.00 |
| 各周期事务 P99 ms | 5.48–5.85 | 6.06–7.95 | 5.98–6.93 |

结论：此合成负载的 WAL 稳定减少 **16.39%**。块写入相对两次 off 减少约 8.54%–11.65%，但宿主机写回/调度带来噪声，不能当成生产保证。总体 CPU 差分包含 I/O 等待相关执行差异，不证明压缩本身没有 CPU 开销；尾延迟未显示稳定改善。

这是带代表性索引结构的合成模型，不是生产全库副本或真实消息回放。没有据此启用生产 lz4，也没有更改 checkpoint 周期；后续若批准生产压缩灰度，应测完整高低峰、每事件 WAL、CPU/尾延迟与恢复预算。

## 部署

私有回滚检查点（0700）：

`/home/justin/backups/superlily/20260912-pg-ablation-b7lnqd`

保存变更前实际运行版本的 service.py、production.env、compose.yml、WAL 实验/恢复结果及复现脚本。配置含秘密，不得提交。

- 旧镜像标签：`superlily/core:pre-pg-ablation-20260912`。
- 旧镜像 ID：`sha256:2d57b4c23a8ba6bc5f1f8b87f8c91b401e2d4fb6fddacd4d76b3e2a3f19d1a59`。
- 新镜像标签：`superlily/core:pg-write-ablation-20260912`。
- 新镜像 ID：`sha256:0f55e7d510ac14dfae469f4f85807ed963531551d9bc1eea0623bc526660884d`。
- 新 service.py SHA-256：`335930a4b0733a67975b336ec1c881a0d068a34b6f2b59394619ae42aa7e177f`。

`deploy/Dockerfile.pg-write-ablation` 基于当前生产镜像，仅覆盖 site-packages 中的 service.py，没有把其他脏工作区文件复制进镜像。切换前比较 Compose 与运行容器：环境变量及 command 差异均为空。

只重建 `lily-core`，使用 `--no-deps --no-build --timeout 60 --wait`；Core 于 **2026-09-12 05:37:14.13363499 UTC（13:37:14 北京时间）** 启动，新镜像 healthy，实际导入模块哈希已核实。

PG 容器仍自 2026-09-09 07:11:18 UTC 运行；schema 保持 `0034_qq_media_archive`。生产 `wal_compression=off`、checkpoint_timeout=300、fsync/full_page_writes/synchronous_commit=on 均未改动；索引、历史、SQLite/WAL 配置均未修改。

### 上线健康与写入验证

- 切换前：Lily 水位 827307/827307，Nekro 320020/320020；两端 online、pending/quarantine/lag=0。
- 切换后：Lily 827325/827325，Nekro 320025/320025；两端继续推进，无缺口。历史 capture/replay failure 累计值未增加，不能将其已有非零值称为本次故障。
- 新幂等键 Renderer smoke：创建 HTTP 201、产物下载 200；PNG 134,360 B、2048×1191，SHA-256 `a59180dff43f6b84516a35047b2b2bc26f8a161dbfae704f2092aecc9b5a0413` 验证通过。没有创建投递意图，没有发送 QQ 消息。
- Renderer、LaTeX worker、两端采集进程和 PostgreSQL 未重启。Wolfram 原有内核故障未修复、未重启；H2/Qdrant/DeepSeek/Status/LaTeX Provider 保持原停机状态。
- 13:42 左右的健康快照：Lily 827403/827403，Nekro 320045/320045，pending/quarantine/lag 仍为 0；上线以来 HTTP 200 共 227 次、201 共 34 次，没有 ERROR、Traceback 或其他状态。Agent off / product off、tool ledger_only、render all、festival=true、claim observe-only outside canary=true 均保留。

### 五分钟生产差分结果

上线后窗口 **13:38:46.666—13:43:46.803**（300.14 秒）；对比调查窗口 13:03:31—13:08:31。采样期间未运行实验库，未重置 PostgreSQL 统计。

| 指标 | 调查窗口 | 上线后窗口 |
|---|---:|---:|
| 新 observation / ingress receipt | 68 / 68 | 101 / 101 |
| 水位 UPDATE | 136 | 101 |
| 每回执水位 UPDATE | **2** | **1** |
| 水位 HOT UPDATE | 68 | 0 |
| bot_instances UPDATE | 171 | 114 |
| bot_instances autoanalyze | 4 | 1 |
| 新 claim | 0 | 0 |
| 新历史 delivery receipt | 0 | 0 |
| WAL bytes | 5,644,506 | 8,036,647 |
| 定时 / 请求 checkpoint | 1 / 0 | 1 / 0 |
| checkpoint buffers written | 827 | 1474 |
| 新 temp bytes / deadlocks | 0 / 0 | 0 / 0 |
| 已捕获 PG 进程 write_bytes | 21,082,112 | 40,529,920 |
| 整盘 write bytes | 1,132,843,008 | 1,769,476,096 |

生产结果确认水位双更新被消除。实例表在回执流量增加约 49% 时，实际更新次数仍从 171 降至 114；由于心跳、状态等负载不同，不将这两个短窗推算成精确全天节省率。

水位保留的 updated_at 索引仍使合并后的那次更新成为非 HOT；减少的是原先额外的 HOT 更新，**不是把该表全部更新变成 HOT**。索引删除不在本轮实施范围。

两个窗口的消息量与 checkpoint 写回不同；上线后 WAL 和整盘原始字节数反而较高，不能宣称整盘写入已下降，也不能把合成实验的 16.39% 当成已上线收益（生产压缩仍 off）。本轮已验证的效果是具体冗余 UPDATE 消失和采集正确性，全天物理写入改善仍需同负载/更长窗口证据。

完整前后统计归档于回滚检查点的 `superlily-pg-survey-20260912.json` 与 `superlily-pg-post-ablation-20260912.json`，健康快照为 `final-health.json`。所有本轮采样已结束。

## 回滚

只回滚本次代码，不恢复旧配置覆盖后来新增变更：

```bash
sudo docker image tag superlily/core:pre-pg-ablation-20260912 deploy-lily-core
sudo docker compose --env-file .env -f deploy/compose.yml up -d --no-deps --no-build --timeout 60 --wait --wait-timeout 90 lily-core
```

检查实际镜像 ID、health/ready、两端水位/缺口、pending/quarantine 和 Renderer。没有 schema 回滚，不回滚数据库或删除上线期间新数据。

## 清理与边界

独立实验容器及唯一合成数据卷 `superlily_pg_ablation_lab_20260912` 已在归档结果后移除；合成数据库本体未保留，可用脚本重建。没有删除用户数据、生产卷或回滚镜像。没有自动启动长期监控，未提交或推送代码。

本轮是短期正确性及运行效果验收，不是整盘全天写入归因，也不是对索引删除、非消息审计删减或生产 WAL 参数变更的批准。
