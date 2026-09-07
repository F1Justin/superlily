# R5.4（C0-H）QQ 断线补洞

状态：Nekro 生产小窗口断线 canary 通过，Lily 与全范围验收待完成（2026-09-07）。
服务 MANIFESTO 第 2、3 条。

## 范围与身份

两套桥接器在启动及 OneBot 重连后，以本地持久连接水位为下界、重连时刻为上界创建
逐会话补采任务。首次启用仅回看有界窗口，并明确记录首次启用或超出回看上限的缺口。
任务、分页游标、重试次数和待上报进度保存于独立 SQLite；消息和进度通过既有 durable
event spool 送到 Core。任务游标仅在本页所有可接收消息成功进入 spool 后推进。

历史消息直接写入采集端，不送入 NoneBot matcher、Nekro 消息处理器或 claim/Agent 执行入口。
Core 保留平台发生时间和补采来源，不用旧消息回退实例的最新事件时间。去重只使用可验证
的会话、发送者、平台时间和 native identity；无法证明身份的消息计入缺口，不按文本猜测。

## 平台基线

已检查线上 `/app/napcat/napcat.mjs`，版本为 4.8.110。群和私聊历史返回 `messages`，
`message_seq` 会先按账号本地短消息 ID 映射到原生 ID；不是可自行加减的序号。
请求显式提供 `reverseOrder`，逐页检查时间是否向过去推进；重复页、反向分页、空页、
接口错误都不能证明“已全部恢复”。`get_recent_contact` 只有有界快照，不是完整会话清单，
还需结合群列表、好友列表和已经持久保存的会话。

官方参考：[扩展 API](https://doc.napneko.icu/develop/api/doc)。实施和验收以部署版本为准。

## 完整性与运行边界

默认关闭；限制回看时长、每页条数、每任务页数、重试次数、请求间隔和调用超时。
扫描到下界仅表示平台返回数据覆盖该时间边界，不能证明平台提供了断线期间所有消息。
记录状态为 `scanned`，而非无条件的“完整恢复”；平台保留期之外、撤回和不支持的通知无法重建。
成员快照与媒体下载属于其他阶段，本阶段不修改 ChatExporter。

验收包括普通消息与历史消息去重、私聊对端身份、分页方向/重复页、失败重试、崩溃续跑、
spool 拒绝时不推进水位、历史消息不进入行为执行，以及缺口和任务进度可在数据库查询。

回滚：关闭补采开关并恢复桥接器。保留本地任务与 spool，保留 Core 的消息及补采证据。
生产启用前核对备份、迁移和真实小窗口采样；不以单元测试代替生产签署。

## 配置与观测

Lily 使用 `LILY_CORE_HISTORY_RECOVERY_ENABLED`，Nekro 插件使用
`HISTORY_RECOVERY_ENABLED`，均默认 `false`。其余配置 Lily 带 `LILY_CORE_` 前缀、
Nekro 不带前缀：

- `HISTORY_LOOKBACK_SECONDS`：默认 86400，允许 60–604800；
- `HISTORY_PAGE_SIZE`：默认 50，允许 2–100；
- `HISTORY_MAX_PAGES`：默认 20，允许 1–100。

首次启动最多回看一小时并标为 `bootstrap_window`；重连窗口前置 30 秒重叠。
单 worker 串行读取，请求间隔 1 秒，超时 30 秒；失败后至少等待 60 秒，每任务最多
3 次失败。部分目录接口失败不会阻止已知会话的子任务。分页停止或预算耗尽时保留
`partial` 和原因，不自动无限重试。

任务库为既有 spool 路径加 `.history.sqlite3`，使用 WAL/FULL 和 0600 权限。
未配置 durable spool 路径时不启动补采；启动或会话记录失败不停止实时采集。
终止任务保留在本地，不做自动删除。单轮最多处理 1000 个活动任务。

DBeaver 中展开 `public → Tables → qq_history_recovery_progress`，双击后打开“数据”。
先按 `instance_id`、`conversation_id` 筛选，再看同一 `job_id` 的最大 `revision`：
`window_start/end` 是补采窗口，`state/reason` 是当前扫描结果。该表每次进度追加一行，
不是每任务覆盖一行。`captured` 是成功送入本地 spool 的次数（含重叠页），不等于
PostgreSQL 唯一消息数；最终入库还需核对 ingress receipt、水位和 quarantine。

消息保留平台时间、原生身份、历史来源与被省略字段列表。回复引用正常关联；临时 URL、
媒体实体和转发节点的进一步补全留给 R5.5。历史接口重复读取可能刷新昵称或内容：
不同返回包采用不同 spool key，由 Core 按原生身份去重。历史消息先到、实时消息后到时，
保留补采来源并接纳真实实时包，使实时 claim 和回复来源关联保持有效。

历史接口返回的昵称进入名称观测表，方法标为 `onebot_history`，名称的观测时间采用入库
接收时间；原消息时间另存 provenance。平台没有证明该昵称在旧消息发生时有效，
因此不能据此虚构过去的改名时间。

## 自动化验收

- 两套桥接器共用行为测试：群/私聊身份、两种到达顺序、历史 claim 拒绝、真实 spool
  重复包、分页续跑与拒收、重复页、页数上限、超时、部分接口失败、连接水位恢复，
  以及进度投影与篡改重放拒绝。
- 完整本地回归：659 passed、8 skipped、1 deselected。未运行的 PostgreSQL archive
  专项不作为本阶段通过证据。
- 独立 PostgreSQL 17 临时实例：真实 spool → Core → 回执确认专项及迁移/模型 drift
  检查合计 26 passed；SQLite 专项及迁移 27 passed、1 skipped。

## Nekro 生产断线 canary（2026-09-07）

经用户明确授权，Core 部署到 `0033_history_delivery_receipts`，Nekro 启用
300 秒回看、每页 50 条、每任务最多 5 页；Lily 不启用补采，ChatExporter 不改动。
Core 镜像为 `sha256:f0ec484d914de723dd448878d5e08699b728536438da9b6b8ecb00ff57a33b0b`。

北京时间 19:45:14.675 停止 `nekro_agent`，由独立 systemd 定时器在
19:46:16.064 自动启动（停止约 61.4 秒），19:46:45 OneBot 重连。
NapCat 始终在线，不退出 QQ 登录；测试的是消费端断线与恢复，不是 QQ 服务端离线。

- 19:45:15–19:46:15 的一分钟核心窗口，Lily 记录了 11 条消息。其中 4 条属于
  Nekro 本次群清单中的共同群，4/4 均以 `onebot_history` 补回；另 7 条属于
  不在该清单中的两个群，不计入 Nekro 可采范围。
- 覆盖重连等待的 19:45:15–19:46:45 窗口，共补回 8 条消息、8 个不同 source event。
  这些来源关联的 Nekro claim 为 0、回复为 0。
- 检查时 Nekro 连续/最高水位为 `274225/274225`，Lily 为 `749876/749876`；
  本次隔离补采包已全部确认，spool 无 pending/quarantined。
- 扫描到时间下界的会话不等于全量历史完整；空历史及不可用会话仍保留 partial/失败证据。
  本次没有可证明的私聊断线消息样本，也未做 NapCat 自身离线或 Lily 断线验收。
- 本次重连任务全部结束：24 个群、6 个私聊为 scanned；2 个群、4 个私聊三次
  `ActionFailed` 后标为 partial，1 个群为空历史。系统发现任务因有限会话清单而为 partial。

真实 canary 覆盖并修正了两个自动化盲点：PostgreSQL advisory lock 必须使用十六进制
摘要；同一原生消息的不同投递包必须分别确认 spool 回执。`history_delivery_receipts`
保存额外回执，原 `ingress_receipts` 仍每条 observation 一行，避免改变 archive 视图基数。
连续水位合并两张回执表；已绑定的包仍拒绝篡改序号、摘要或时间。迁移不重写原回执表，
额外回执非空时拒绝直接 downgrade，以免删除入库证据。

部署检查期间的 85 条 `durable spool binding` 隔离包在完整备份后按原包重投，已全部确认；
没有改写消息内容或假造回执。复核依据包含 PostgreSQL 时间窗对照、OneBot 重连日志、
任务 SQLite 和 spool 回执，不以“容器 healthy”代替补采成功。

备份与回滚文件：`/home/justin/backups/superlily/20260907-c0h-112806`，含约 2.6 GB
custom-format dump、旧桥接器/配置、重投前两份 SQLite 快照及 Core 回滚命令覆盖。
旧镜像 tag 为 `superlily/core:pre-c0h-20260907`。回退旧 Core 时须绕过旧 Alembic 启动步骤，
因为旧版本不认识新 revision；保留新增表和回执，不对生产库执行破坏性 downgrade。
