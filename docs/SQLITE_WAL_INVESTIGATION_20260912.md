# Lily / Nekro SQLite/WAL 只读调查（2026-09-12）

结论：存在可减少的小事务和周期写入，但本次证据不能将整盘 267 GB/天归因于
Lily/Nekro。优先考虑单次 ACK 原子提交、history 在线时间用 UPSERT 替代 REPLACE；
保留 WAL/FULL 和“先持久采集、再发网络请求”的顺序。以下方案均未应用生产。

## 范围和方法

- 已阅读 `/home/justin/maintenance-2026-09-12/result-and-storage-investigation.md`。
  其中 267.2 GB 是此前整盘 24 小时增量，不是本项目逐文件数据。
- Lily 实际写库进程 PID `1443555`，不是 Playwright 子进程；Nekro PID `1445311`，
  不是容器入口 `uv` PID `1445210`。用实际 fd 核对四个 SQLite 及其 WAL。
- Lily 插件目录指向本仓库 `bridges/lily_nonebot/lily_core_bridge`；Nekro 部署目录是
  `/home/justin/nekro/plugins/workdir/superlily_bridge`。两个部署版本的 spool/history 源码
  哈希与仓库一致，调查前后未改变。两个 spool 实现相同，history 实现也相同。
- 运行库：Lily SQLite `3.51.2`，Nekro SQLite `3.34.1`。源代码明确设为 WAL/FULL、
  `isolation_level=None`；同解释器/镜像的隔离实验也验证 FULL=2、自动 checkpoint=1000 页。
  没有用调查者新建只读连接的同步参数冒充运行服务连接的配置。
- 先做 60 秒无追踪基线，再做 60 秒文件级 strace。只记录路径、长度、时间和同步调用，
  `-s 0` 不保存写入内容；没有追踪 read/readv 或消息正文。
- SQLite 只执行短时 `mode=ro` 聚合查询；提交计数来自只读 SHM 的双份一致头部
  `iChange` 差分，checkpoint 观察使用 `mxFrame/nBackfill/salt`。
  **没有对生产执行任何 wal_checkpoint、VACUUM、写 SQL 或配置更改。**
  `iChange` 和 WAL 索引字段定义见 [SQLite WAL 格式](https://www.sqlite.org/walformat.html)。
- strace 已结束，两目标的 `TracerPid=0`。没有重启服务、修改项目代码或生产配置。
  只新增本报告及 `/tmp` 调查工具/合成测试数据；保留已有工作区修改。

## 1. 实际贡献：三种计量口径必须分开

### 无追踪基线：12:52:56–12:53:56 CST，60.001 秒

| 对象 | `/proc/PID/io` 的 write_bytes 增量 |
| --- | ---: |
| Lily 整个 Python 进程 | 3,833,856 B = 3.656 MiB |
| Nekro 整个 Python 进程 | 3,878,912 B = 3.699 MiB |
| 两进程合计 | 7.355 MiB |
| 根分区 diskstats（不同层级） | 309.449 MiB |

两进程分别新增/确认 9、12 条 spool 记录；没有新增失败或隔离记录。
Lily spool 的 WAL 写提交 28 次 = 9×3 + 1 次保留期清理；Nekro 37 次 = 12×3 + 1。
history 各 12 次写提交。基线开始、结束均无活动恢复任务。

### 文件级追踪：12:54:07–12:55:07 CST，60.002 秒

| 数据库 | 新采集/ACK | WAL 写提交 | WAL 写入 | 主库 checkpoint 写回 | sync 调用 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Lily spool | 9 / 9 | 28 | 535,632 B | 249,856 B | 31 |
| Lily history | 无活动任务 | 12 | 98,880 B | 0 | 12 |
| Nekro spool | 4 / 4 | 13 | 238,992 B | 118,784 B | 16 |
| Nekro history | 无活动任务 | 12 | 98,880 B | 0 | 12 |
| 合计 | 13 / 13 | 65 | 972,384 B | 368,640 B | 71 |

- 四个 SQLite 的 WAL + 主库系统调用写入合计 **1,341,024 B = 1.279 MiB / 60 秒**。
  同窗口两个进程 write_bytes 合计 5.164 MiB，根分区 247.777 MiB，swap 0。
- 上表 sync 包含 checkpoint/WAL 重用所需同步，不能把 71 次都当成消息事务。
  Lily 实际调用 fsync，Nekro 调用 fdatasync；此处统一统计为 sync。
- Lily spool 130 个 WAL frame、主库写回 61 页；Nekro 58 个 frame、主库写回 29 页。
  两个 history 各 24 个 frame。页大小 4096 B，每个 WAL frame 另有 24 B 头部。
- 同时捕获 Nekro app.log 7,043 B、一个图片文件 36,770 B，不计入 SQLite 合计。
- 若机械假设这 60 秒流量和清理状态持续整天，SQLite 文件调用量约 **1.93 GB/天**，
  两段样本的整个进程记账量约 **7.80–11.11 GB/天**。这些只是短窗等效速率，
  **不是已测得的实际日总量，也不是 SSD/NAND 写入量或 SQLite 的物理写放大倍数。**
- 进程记账涵盖其他插件和文件；文件调用与进程记账、Btrfs 异步写回/元数据、块设备
  计数的时点和归属不同，不能相加、相减后将差额全部归给 SQLite。四库当前均在
  Btrfs 根分区，没有 NOCOW 标志，但本次不具备测量某库独立 CoW 放大倍率的条件。
  ptrace 会引入少量时序扰动，调查工具/隔离实验也可能产生少量背景 I/O；两段不同
  流量窗口不是受控性能对照，不用它们的差值宣称优化收益。
- 当前只有两个短窗，流量分别是 21、13 条新记录，不足以推断早前 267 GB 的构成。
  如需实际日贡献，需要另行安排有明确结束时间的低开销长窗观测，覆盖繁忙、空闲、
  补采和保留期清理；不建议为此连续运行 24 小时 strace。本次没有部署常驻监控。

## 2. 事务、空轮询和重复更新

以下结论在两种运行库版本的隔离数据库中一致。

### spool

- `spool.py:347`：入队已有显式 BEGIN IMMEDIATE/COMMIT；记录、永久幂等身份、
  next_sequence 在同一事务中，成功后才返回并允许网络请求。这一持久性边界应保留。
- `spool.py:456`：ACK 更新记录、增加 replay_successes、清除 last_error，三条写 SQL
  各自自动提交。普通健康 ACK 实际是 **2 次 WAL 写提交、3 个 frame**；首次建立
  last_error 或清除非空错误时可为 3 次提交。加上入队，一条成功记录通常共 3 次提交。
  Python RLock 不是 SQLite 事务；`isolation_level=None` 下也不能以普通连接上下文
  管理器代替明确的 BEGIN/COMMIT。
- 已 committed 的重复 ACK、相同幂等入队：隔离实测 0 WAL 写提交；两条投递路径
  可能重复请求 Core，但不能把它们直接算成两倍本地 SQLite 持久化。
- 重复把 last_error 写成空字符串：虽然执行 UPSERT，隔离实测 0 WAL 写提交。
  仅删去这条 SQL 不会节省“每条消息一次 fsync”。
- `retry()` / `quarantine()` 也把状态、计数、错误分成自动提交；首次不同错误的
  隔离样本各 3 次写提交。当前两段生产样本没有新增失败，不是本次主要写入来源。
- `next_pending()` 无记录时通常 SELECT 后返回，每 5 秒或被事件唤醒再次检查；
  `status()` 和 `next_retry_delay()` 均为读。无到期记录时隔离实测空轮询 0 WAL 提交。
  但空轮询会触发每分钟的 compact；有旧 committed 记录到期或待写回 WAL 时，
  即使没有新消息，也可能发生清理写入或 checkpoint。这不能笼统说成“空轮询从不写盘”。
- 启动的 quick_check、schema 检查不是常驻周期写入。保留期清理仅删 committed
  payload，不删 pending/quarantined，也保留 spool_identities 的长期幂等证据。

### history

- `history_recovery.py:179` 的 connections_loop 对每个在线 bot 每 5 秒执行
  `INSERT OR REPLACE INTO online`，更新时间变化，因此真的写；每次 1 提交、2 WAL 页。
  两端各 1 bot，实测均为 12 提交/分钟、98,880 B WAL/分钟。
  若在线状态全天持续，这部分固定机制本身约 142.4 MB/端/天、17,280 次同步/端/天，
  仍不含文件系统物理放大和偶发恢复任务。
- `work_loop()` 每 5 秒查询活动任务；样本中 Lily 129、Nekro 343 个任务全部 inactive，
  无活动任务的轮询没有写库。history 固定写入来自 online，不是无限重试中的任务。
- `remember()` 的 INSERT OR IGNORE 遇到已有 peer，实测 0 WAL 提交。
- `save(job)` 使用 REPLACE，连内容完全相同的 job 也会产生写提交（合成样本 3 页）；
  checkpoint 保存待报告进度，报告成功进入 spool 后又保存 report=None。
  这些通常是两个不同、与故障恢复相关的状态，不能当成可无条件删除的重复写。

## 3. WAL checkpoint

- spool `compact()` 最多每 60 秒执行一次清理并主动 PASSIVE checkpoint；调用点包括
  入队前、ACK 后、空队列轮询。`close()` 才请求 TRUNCATE；本次没有调用 close。
- 追踪中两端 spool 各一次 checkpoint 和一次 WAL 重用。Nekro 的 `mxFrame=nBackfill=49`
  后 salt 改变、帧号回到小值；主库写回与 WAL 重用也由 pwrite/fsync 直接印证。
- 自动 checkpoint 默认 1000 页，两种运行库均在隔离连接确认；代码未覆盖此阈值。
  history 没有额外的每分钟主动 checkpoint。仅有在线时间写入时，2 页/5 秒意味着
  大约 41 分 40 秒达到 1000 页；这是代码与速率推算，本次没有连续观察完整周期。
- history 的 WAL 文件约 4.13 MB、spool 的 WAL 约 2.91–4.17 MB，并不意味着文件每天
  净增加这些大小；SQLite 正常复用 WAL。追踪中 history 没有主库写回，spool 正常重用，
  没有 checkpoint 受阻、无限增长或每 5 秒强制 TRUNCATE 的证据。
- checkpoint 把页从 WAL 写回主库，本身需要同步；改变频率不是取消 FULL 的提交同步。
  原理见 [SQLite WAL/checkpoint](https://www.sqlite.org/wal.html)。

## 4. 建议顺序与安全条件（均未实施）

### P1：单次 ACK/retry/quarantine 内合并事务

将单次操作的条件状态更新、计数、last_error 放进一个明确事务，失败整体回滚；
compact/checkpoint 放在 COMMIT 之后，事务内不 await 网络调用。保留 receipt 的
spool_id/sequence/hash/outcome 校验及重复 ACK 分支。

隔离实验：ACK 写提交从 2 降到 1，WAL 仍为 3 页；正常端到端从 3 提交降到 2，
约减少三分之一提交同步，而不是减少三分之一全部写入字节。对当前 13 条记录的
追踪样本，预计减少约 13 次提交同步，不能据此承诺固定磁盘写入降幅。
这也消除了“记录已 committed、计数尚未更新”之间的崩溃窗口。

### P1：online 用定向 UPSERT，先不降频

只更新已有 bot 的 at，不 REPLACE 整行。两版本隔离实测：同样 5 秒一次、同样 1 次
FULL 提交，WAL 从 2 页降到 1 页。在线水位时效、恢复窗口和启动行为不需要改变；
优先于将 5 秒粗暴改成一分钟。当前固定 online WAL 流量理论减半，同步次数不变。

### P2：checkpoint/过期清理按实际需要调度

考虑跳过没有待回填帧的主动 checkpoint、按最早到期时间唤醒清理，或对 committed
记录做有上限的小批清理并适当延长间隔。必须保留 WAL 大小上限、磁盘/配额压力下
及时清理和故障退出恢复。不能关掉自动 checkpoint 后任由 WAL 增长，也不要用
频繁 TRUNCATE/VACUUM 或删除 WAL 的方式降写入。当前没有证据需要紧急改此项。

### P2：online 降频或跨消息批量，单独做恢复验收

- 在线水位改为 30/60 秒，理论可减少固定同步，但崩溃后的恢复起点会更陈旧，补采重叠
  更大；当前 Lily 回看 86400 秒/100 页，Nekro 仅 300 秒/5 页。必须验证额外扫描、
  配额和 partial 边界，保持重连创建任务与持久水位更新的安全顺序。不能只在关机时写水位。
- 多条 ACK 批量提交可在 Core 已持久确认后进行；崩溃至多重放，但需要处理未落盘 ACK
  的内存集合、队头重放、失败重试和有界 flush，复杂度高于单次 ACK 原子化。
- 多条入队 group commit 只有在每个调用都等待该批 FULL 提交成功后才返回“已持久采集”
  时才可考虑；不得先在内存接收成功、后台延迟刷盘。历史页可以设计批量入队接口，
  但游标必须在本页所有消息已持久进入 spool 后推进，批次失败不可跳页。
- history 与 spool 是两个数据库。保留“spool 提交 → history 游标/待报告状态 → 报告
  入 spool → 清除待报告”的可恢复顺序，不能假定 WAL 下 ATTACH 跨库能提供整体原子性。

### 不采用的捷径

不改 synchronous=NORMAL/OFF、不关闭 fsync、不删除 WAL/幂等身份、不靠平台历史接口
弥补主动放弃的本地持久性。SQLite 明确说明 WAL/NORMAL 可能在掉电或系统崩溃后回滚
已提交事务；本项目要求保留这一层持久性。参见
[SQLite synchronous](https://www.sqlite.org/pragma.html#pragma_synchronous)。

上线前须用故障注入覆盖：入队提交前/后崩溃、Core 已确认但本地 ACK 未提交、ACK 中途
失败、双路径并发 ACK、错误回执、磁盘满/锁忙、重启顺序及连续水位、历史页部分入队、
游标/进度报告之间崩溃。隔离 SQL 收益实验不是完整实现或生产崩溃恢复验收。

## 证据位置及调查结束状态

- 原始无正文追踪：`/tmp/superlily-sqlite-trace-20260912.log`（0600，162,654 B）。
- 只读采样工具：`/tmp/superlily-sqlite-io-survey.py`；汇总器：
  `/tmp/superlily-sqlite-trace-summary.py`；隔离复现：
  `/tmp/superlily-sqlite-transaction-repro.py`。`/tmp` 证据会随清理失效，本报告保留关键数值。
- 复现仅使用合成数据；Nekro 测试容器无网络、只读根文件系统、数据库位于 tmpfs，
  结束自动删除，未挂载生产数据库。宿主临时合成库未影响生产。
- 调查结束两端 online，queue/pending/quarantined 均 0。生产进程未更换、源码哈希未变，
  没有安装常驻采样、创建补采任务或发 QQ 消息。
