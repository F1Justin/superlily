# ChatExporter QQ 动作与媒体修复（2026-09-27）

已部署到 Nitori `/Users/justin/0Projects/chatExpoter`，当前提交 `f318231`，
分支 `codex/actions-media-export`。Core 归档迁移为 `0035_export_action_details`。

## 最终行为

- 两套桥接使用 `onebot-v11-actions-v3`，按布尔 `is_add` 保存 `add/remove`；
  缺少或无效方向时保留 `observed_state` 并标记 partial，不从数量反推方向。
- Nekro 在有界缓存中保留原始图片的 `summary/sub_type/emoji_id/emoji_package_id`，
  用同消息中的文件哈希及出现顺序对应转换后的图片。不缓存 URL，也不改变 Runtime
  模型输入或附件下载。匹配不到时保留未知分类。
- 既有 `archive.message_timeline_v2` 增补 actions JSON 的操作方向、观察实例、已解析
  目标事件、目标作者和消息段，优先选择保留媒体元数据的观察；列结构和授权不变。
- 导出具体表情名称、目标消息摘要，区分操作人和被撤回消息作者；未记录方向的历史
  通知明确显示状态更新。图片摘要始终带类型标记，按消息段顺序呈现。
- 默认导出合并同一秒、同目标事件/操作者/原作者的撤回。唯一的同秒、同作者、
  同内容、不同观察实例的发送回执与接收记录可做展示匹配，并显式标注含匹配回执。
  这不是原消息身份认定；歧义时保留，`--all-sources` 完整保留所有记录。
- 完整格式保留匹配回执 ID 和来源实例。昵称回退使用群内名称映射；具体 face、mface、
  卡片、戳一戳不再丢弃必要类型或裸露内部段名。分钟/秒相对时间正确解析，开放结束
  范围在导出开始时固定截止时间，逐行文件写入日志降为 DEBUG。

## 验证

真实窗口：群 861651713，2026-09-25 22:39:20 至 2026-09-26 22:39:23（上海时间）。
Nitori 原生环境实际导出：

- 旧版 2058 条；修复默认导出 1881 条。
- 6 次撤回各显示一次；171 个唯一发送回执候选并入接收记录并加标记。
- 813 条表情通知全部保留。原始 30 种表情均可显示名称或 Unicode；145 条 count=0
  和其他旧通知不被编造成明确贴上/取消操作。
- 22 条在另一观察中保留动画表情标记的消息恢复分类；自定义图片摘要不再伪装纯文字。
- 完整来源模式 2834 条，与同窗口底层视图行数一致。
- 21 项 ChatExporter 测试在 Nitori 通过；103 项桥接相关测试通过。
- 独立 PostgreSQL 的迁移、回退、归档契约等 6 项测试通过，新增动作投影测试通过，
  验证 remove/count=2、目标原作者和媒体段可见。未向生产注入测试事件或发送 QQ 消息。

Core 镜像基于既有生产镜像，仅增加迁移文件：
`sha256:6eb13b136e0da889e37de04abc819419fd214fd7c9520cc3887633bec1c1b04b`。
先单独迁移、再重建 Core；数据库和 NapCat 未重启。Lily/Nekro 依次重启，均重新连接，
Core ready 为 database=ok。两者 reporter/spool worker 均 running、pending=0、quarantine=0。

12:37:37 后真实新反应已存为 `onebot-v11-actions-v3 / add`，不再省略 raw.is_add；
新 Nekro 图片已保留原始 sub_type。取消方向的接收/持久化由测试覆盖，验收时未观察到
上线后的真实取消通知。旧记录缺失的方向不回填猜测。

导出账号仍仅对 `archive.message_timeline_v2` 和 `archive.conversation_mappings`
具有 SELECT 权限。服务恢复证据不等同于证明重启窗口的每一条历史消息都已完整补齐。

## 产物与回退

Nitori Downloads：

- `第二桃花源_修复版_20260925-26.txt`
- `第二桃花源_完整来源_20260925-26.txt`

本机证据与副本：`run/chatexporter-20260927/`，包括原视图定义、桥接源码备份、
完整 Nekro 插件备份、真实窗口快照和重放脚本。Nitori Git bundle 备份位于
`/Users/justin/.local/state/chat-exporter/backups/before-20260927.bundle`，旧提交 `6aca5b3`。

需要回退时：先让 Nitori checkout 回到旧提交；恢复备份的 Lily normalizer，以及
Nekro 插件的 __init__/platform_actions 文件，依次受控重启桥接。新增媒体模块可保留
为未引用文件。使用当前带迁移的 Core 镜像执行 `alembic downgrade 0034_qq_media_archive`，
随后将 `deploy-lily-core:before-chatexporter-20260927` 标回 `deploy-lily-core:latest` 并
仅重建 Core。迁移只修改视图，不删除历史数据；无需覆盖数据库或 spool。


## 2 月旧历史跨来源合并

Nitori 提交 `f318231` 补齐旧 Lily/Nekro 历史记录的展示合并。仅对同群、同实际平台
发送者、规范化消息段相同、相差不超过一秒且双向唯一的候选合并；相同来源重复、
有歧义的匹配、无法核对媒体身份的图片均保留。Nekro 内部用户编号使用归档中实际
platform_userid/sender_nickname 呈现。采用短滑动窗口，保留已输出的邻近候选作为
歧义证据，避免分页/秒边界及 A(0), B(1), A(2) 场景错误合并。页脚显示合并数，
完整模式保留两路记录 ID、消息 ID 和时间；--all-sources 不合并。

实测用户提供的 2026-02-21 21:00–21:29：211 条（Lily 101、Nekro 110）合并为
114 条，97 对跨来源候选合并；完整来源仍为 211 条。一组机器人图片没有可靠的
可比对标识，以及同源重复戳一戳，继续保留。35 项测试在 Nitori 通过；9 月导出
正文逐字一致，仍为 1881 条。未修改数据库、未重启服务。

新文件在 Nitori Downloads：`第二桃花源_20260221_210000_去重版.txt` 和
`第二桃花源_20260221_210000_完整来源.txt`。本机副本及证据位于
`run/chatexporter-feb-20260927/`。本次回退只需恢复 Nitori 的 `19e6116`；备份为
`/Users/justin/.local/state/chat-exporter/backups/before-legacy-20260927.bundle`。
