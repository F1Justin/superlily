# R5.3（C0-G）OneBot 平台副作用操作账本

状态：生产启用并验收通过（2026-09-04）。

## 用户结果

Superlily 不仅要保存平台随后推送的状态事件，还应知道机器人主动做过什么：何时调用了哪个
OneBot/NapCat 副作用接口、针对哪个会话或对象、由哪条消息触发、使用了哪些安全参数，以及
调用成功、失败还是因超时而结果不确定。普通消息发送继续由现有 response/message-sent 账本
负责，避免重复建立第二套发送事实。

## 记录范围与模型

两套桥接器在 NoneBot `on_calling_api` / `on_called_api` 钩子上统一观察非消息发送副作用：

- 撤回、表态、戳一戳、转发，以及已读/输入状态；
- 群名、群名片、头衔、管理员、禁言、踢人、退群等成员和群设置；
- 精华、群公告、群文件/目录、群相册、收藏等内容操作；
- 好友/加群请求处理、好友备注/删除，以及头像、资料、在线状态等账号设置；
- NapCat 中其他以 `set/delete/del/upload/create/move/trans/rename/mark/forward/click/do`
  开头的副作用扩展，和经过显式列举的 `send_like`、`friend_poke` 等特殊接口。

每次调用使用独立 `call_id`。调用前先把 `started` 事件同步追加到现有本地 durable event
spool，调用结束后追加 `completed` 事件；Core 将两者汇总到 `platform_api_calls`。账本保存目标
会话、触发源事件、开始/完成观测是否齐全、结果、返回码、耗时、显式返回的消息 ID 和安全
错误码。超时为 `ambiguous`，不能等同失败，也不能重试或伪造最终平台状态。

## 安全和诚实边界

- 参数严格使用白名单；token、cookie、凭据、原始文件路径和 URL 不保存。文件/图片/URL 只记
  “已提供”，请求 `flag` 只保存 SHA-256；公告正文和备注等业务文本有长度上限。
- 异常只保存类型化安全错误码，不保存可能夹带凭据或路径的异常全文。
- 平台明确返回的消息 ID 可保存；平台后来主动推送的 notice 仍由 C0-E 独立记录。两者没有
  可靠共同标识时不得靠时间窗口猜成同一次操作。
- spool 不可用时继续 fail-open，不阻断机器人原本的 API；既有 spool failure/drop 指标负责
  暴露审计缺口。代码完成不代表该时段事实可被事后恢复。
- `get_*` 等只读接口不进入操作账本；ChatExporter 不修改。

## 验收门与回滚

1. 两套桥接器的副作用识别和字段清洗实现逐字节一致，普通 `send_group_msg` 等仍只走现有
   response ledger。
2. started/completed、重投、结果先到、超时和安全字段均有合同与数据库测试。
3. SQLite Alembic upgrade/downgrade/upgrade 和全量可运行回归通过。
4. 生产发布需另行迁移和更新桥接器，验证 spool 连续序号、API 调用延迟、账本 start/result
   完整率、错误日志及数据库增长后才能签署。

## 生产验收记录

2026-09-04 21:44 CST，生产迁移处于 `0031_platform_api_calls`。账本已观察到 18 次真实
`set_msg_emoji_like` 调用，全部同时具有 started/completed 观测且结果为 `succeeded`，没有
未完成或非成功记录。Lily 与 Nekro 的 durable spool 水位分别为 `706089/706089` 和
`248930/248930`，最高连续序号与最高已见序号一致；两实例均上报 online，Core 与数据库健康。

回滚时恢复上一桥接器版本，停止新增审计事件；如需回退 schema，再 downgrade
`0031_platform_api_calls`。回滚不得删除已经形成的操作证据。
