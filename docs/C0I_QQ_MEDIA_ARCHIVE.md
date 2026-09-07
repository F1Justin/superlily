# R5.5（C0-I）媒体与合并转发补全

状态：已部署，展开与下载未启用，待真实样本验收（2026-09-08）。服务 MANIFESTO 第 2、3 条，
不改变 Nekro 消息执行行为或 ChatExporter。部署不等于采集功能已启用或生产验收通过。

## 边界

两桥接器通过独立后台任务扫描 durable spool 的新消息，包括 R5.4 补采包。
首次启用从当前 spool 水位开始，不暗中扫描多年历史。扫描水位、任务与待投递报告持久化；
只保存平台标识和已清洗元数据，不在任务库或报告中保存临时 URL、签名或访问凭据。

合并转发使用 get_forward_msg，按树路径保存容器、节点、顺序、平台报告的发送者和时间；
不把转发节点重放成群消息，不将节点 group_id 视作可靠原群身份，不据此创建名称变更历史。
没有提供的时间保持 NULL。循环、深度/条数上限、过期和接口失败明确留证。
转发内文件没有可用直链时，不拿外层群号冒充原文件所属群，记录 `file_context_unknown`。

媒体优先通过 get_msg 刷新原消息、get_group_file_url/get_private_file_url 获取文件直链。
部署 NapCat 4.8.110 的 get_image/get_record/get_file 会在平台侧先下载到缓存；本阶段
不自动调用它们，也不调用 download_file，避免把未经大小检查的下载移到另一容器。
只下载明确允许的 HTTPS 媒体域名，不跟随重定向，不读取 API 返回的主机本地路径。
参考：[官方文件指南](https://doc.napneko.icu/develop/file)、
[接口兼容表](https://doc.napneko.icu/develop/api)；以实际部署代码为准。

Core 使用独立私有内容存储，按 SHA-256 去重；PostgreSQL 保存稳定内容引用、大小和来源。
受保护的下载接口只提供附件下载，不执行或内联预览不可信内容。稳定引用不等于永久保存
承诺：运维须备份内容卷与 PostgreSQL；功能开关关闭不删除已归档数据。

## 数据与读取

迁移 `0034_qq_media_archive` 新增两张表，不修改 `archive.message_timeline_v2` 或
`archive.conversation_mappings` 的读取约定，也不把转发内部节点混入普通聊天时间线。

- `public.qq_media_archive_items`：按原消息、任务版本和树路径记录媒体/转发节点。
  包含实例与外层会话、原消息 ID、发送者、平台时间 `occurred_at`、采集时间 `observed_at`、
  清洗后的段与文本、遗漏字段、状态/原因，以及已归档内容的哈希、大小、MIME 和 `content_ref`。
  路径是分段数字序号，应用按数字段排序；字符串字典序不能代表第 2 与第 10 个节点的顺序。
- `public.qq_media_blobs`：实例实际上传并经 Core 验证的内容哈希和大小。同一内容物理去重，
  但另一个实例不能只报一个已知哈希冒充自己成功下载，必须也完成上传校验。
- 每次采集版本通过 `audit.qq_media_archive` 和原消息建立 `derived_from` 关联，进入既有
  durable spool。通用事件 metadata 仅保留报告摘要与校验值，大节点正文保存于类型化表，
  避免通用 metadata 截断破坏重投一致性。失败版本保留，不覆盖已有成功引用。

鼠标查看：DBeaver 刷新 `public → Tables`，打开 `qq_media_archive_items → Data`，
先筛选外层群号 `conversation_id` 和 `parent_message_id`，再看 `revision`、`path`、
`state`、`reason`、`text`。两张表已在生产出现；本次未启用采集，部署检查时均无媒体记录。
`content_ref` 是 Core 的相对地址，GET 需要管理员鉴权；返回附件、`nosniff` 和不缓存头，
不是公共下载链接。当前 ChatExporter 不会自动导出新增节点或下载媒体。

## 开关、资源限制与恢复

两套桥接器均默认关闭，展开和下载分开配置：

| 配置位置 | 展开/元数据开关 | 内容下载开关 |
| --- | --- | --- |
| Nekro 插件配置 | `MEDIA_ARCHIVE_ENABLED` | `MEDIA_DOWNLOADS_ENABLED` |
| Lily NoneBot 配置 | `LILY_CORE_MEDIA_ARCHIVE_ENABLED` | `LILY_CORE_MEDIA_DOWNLOADS_ENABLED` |

桥接器必须启用已有 durable spool。Core 新增以下环境变量（Compose 已透传，默认不启用存储）：

- `SUPERLILY_QQ_MEDIA_ROOT`：默认空。部署时使用独立目录，例如已有持久 artifact 卷内的
  `/var/lib/superlily/artifacts/qq-media`，按实际挂载核对持久性、运行 UID 的写权限，权限为
  `0700`，目录及祖先不得为符号链接。不要把临时容器层作为永久归档。
- `SUPERLILY_QQ_MEDIA_MAX_BYTES`：默认 8 MiB；Core 配置上限 32 MiB，桥接器当前固定 8 MiB。
- `SUPERLILY_QQ_MEDIA_QUOTA_BYTES`：默认 1 GiB，总配额受跨进程文件锁保护；已存在内容不重复占用。

每个桥接器串行处理任务，API 调用间隔至少 1 秒、单次 20 秒超时、单任务 90 秒截止，
最多 3 次尝试，失败后至少等 60 秒。每次最多 64 个条目、展开深度 3，报告预算 512 KiB，
单节点段数/文本和全树文本另有上限；超限显式标记。Core 每进程最多 2 个同时上传，
单次请求体读取 30 秒截止。下载不接受压缩编码，不跟随跳转，按流校验大小。

当前 HTTPS 域名精确白名单为 `multimedia.nt.qq.com.cn`、`gchat.qpic.cn`、
`c2cpicdw.qpic.cn`、`grouptalk.c2c.qq.com`，仅默认端口/443。不在清单的文件服务器保留
`url_not_allowed`，不自动放宽域名。真实样本验收需要核对不同文件类型的实际域名。
本地展开限制不能约束 NapCat 在生成 `get_forward_msg` 响应时已消耗的内存或递归工作，
因此不是平台端大转发响应的硬隔离保证。

任务状态在 spool 同目录的 `.media.sqlite3` 中持久化（WAL、FULL，私有文件权限）。最多
128 个活动任务，主数据库达到 128 MiB 后停止扫描新增任务；这不是含 WAL 和在途更新的
硬磁盘配额，任务完成记录目前不自动清理。生产需监控任务库增长、扫描水位及原 spool
保留期。源记录已被清理导致的序列缺口计入本地 `state.source_gap_sequences`，不冒充
已归档内容；此计数不是精确丢失消息数，也尚未投射为 PostgreSQL 逐消息缺口。

首次启用不会回扫现有 spool；重启延续游标和持久任务，待投递报告按完全相同内容重投。
中断下载会从头重试，不支持字节断点续传。写入成功而报告尚未入库的内容可能暂时成为
孤立文件，重试按哈希复用；不自动清理孤立内容或残留临时文件，它们仍计入配额。
回滚先关闭桥接器两个开关，保留 spool、任务库、两张表和内容卷；Core 内容目录保持配置
才能继续读取既有引用。数据库 downgrade 会删新表，不应作为日常功能回滚手段。

## 自动化验证与剩余验收门

2026-09-08：本地回归及隔离 PostgreSQL 验证覆盖两套桥接器的嵌套/循环、节点缺时、
文件上下文未知、重复附件去重、过期/跳转/超限、凭据不跨域、私有目录与符号链接边界、
并发/配额、任务重启、三次重试上限、大报告无损入库及精确重投；测试关闭模拟媒体客户端后，
仍可从 Core 管理员接口读回相同字节。归档报告不能申请 claim，也不创建回复决策。
PostgreSQL 新迁移往返与模型漂移检查、既有聊天归档合同往返均通过。

验证结果：常规回归 679 passed、8 skipped（单独排除要求 PostgreSQL 环境的归档合同测试）；
隔离 PostgreSQL 的媒体与迁移组合 21 passed，另行执行归档合同测试 1 passed。
另有 `compileall` 与 `git diff --check` 通过；所有数据库验证均针对临时测试实例，非生产库。

下一步为受控启用与真实图片/语音/视频/文件及嵌套转发样本验收，确认回执、存储增量、权限、
失败原因、实时采集延迟和回滚。平台已经删除或无法返回的内容不承诺恢复；不以接口成功代替
内容验证，也不以自动化测试代替生产签署。

## 生产部署记录（2026-09-08）

经用户授权部署代码 `031df41849dde214bef811032dafd2c3481b860a`，仅更新 Core 与两套桥接器。
Core 镜像为 `sha256:99b0dcdf6777b232795095c598b7bfc0d4f69f30bcf8c571a0394f70c753128f`，
保留 tag `superlily/core:r5.5-031df41`；数据库从 `0033_history_delivery_receipts` 升到
`0034_qq_media_archive`。`alembic check` 无模型漂移，Core readiness/容器健康通过，
新旧镜像的主要依赖版本一致，重建镜像 `pip check` 通过。

私有内容目录为已有持久卷内的 `/var/lib/superlily/artifacts/qq-media`，权限 `0700`，
运行用户可写，单文件 8 MiB、总配额 1 GiB。未上传伪造媒体验收数据；只读接口探针确认
匿名 401、管理员读取不存在的哈希 404。部署检查时两张新表均为 0 行。

Nekro 07:36:36 CST 重启，07:37:06 OneBot 重连；桥接器加载成功，
`MEDIA_ARCHIVE_ENABLED=false`、`MEDIA_DOWNLOADS_ENABLED=false`，既有 R5.4 的
300 秒/50 条/5 页配置保留。Runtime 镜像仍为 `.10`，NapCat 未重启。
Lily 通过既有 `tmux-nb.service` 于 07:37:21 重启，07:37:27 新桥接器启动；默认两个
媒体开关保持关闭。两个进程的心跳及 spool 上报继续工作。

07:38:53 CST 复核：Core/Nekro 均 healthy、无重启循环或 OOM；两桥接器均
pending=0、quarantined=0、last_error=null。Nekro 回执水位为 `277091/277091`，
Lily 为 `753725/753725`；Lily 水位未增长只说明旧包已确认，不证明它正在接收消息。

**部署前已有的限制**：Lily 自 00:00:11 CST 后没有新消息，心跳报告 `degraded`、
`connected_bots=0`；此次重启后仍无 OneBot 连接。不能据心跳将 Lily 采集标为正常，
也不能签署 Lily 媒体验收。本次没有改动其 OneBot 登录/连接配置。Nekro 已恢复 online。
Nekro 启动还报告表情包插件无法访问 Qdrant；该容器自 09-03 起已停止，本次未将其启用。

后续更新：用户重启 Lily 的 NapCat 后，07:41:15 OneBot 重连，实时采集恢复；随后
另行授权启用了 Lily 的 R5.4 自动补采和午夜窗口回填，见
[C0H_QQ_HISTORY_RECOVERY.md](C0H_QQ_HISTORY_RECOVERY.md)。这不改变 R5.5 媒体开关仍关闭的状态。

ChatExporter 代码与权限未改；`archive.message_timeline_v2` 定义校验值前后均为
`3325f70893066204c7bd093ac87e38df`，其与 conversation mappings 的只读权限仍有效。

备份位于 `/home/justin/backups/superlily/20260908-c0i-073200`：约 2.6 GiB PostgreSQL
custom dump、artifact 卷、旧桥接器/私有配置、两份 spool 与 Nekro 补采 SQLite 快照。
dump 完整解码检查（未向数据库执行恢复）及三份 SQLite `quick_check` 均通过；dump SHA-256：
`0836fb740402e01e826313986ab29c19d0c864b96297ebbb198fc3f6eab69ca8`。
保留旧 Core `superlily/core:pre-c0i-20260908` 及跳过旧 Alembic 的回滚 Compose；
正常回滚保留新增表/内容与事实，不对生产库执行 downgrade 或整库覆盖。
