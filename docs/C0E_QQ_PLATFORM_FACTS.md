# R5.1（C0-E）QQ 平台事实完整性

状态：已上线并有真实运行验证；待补齐事件类型、字段覆盖和阶段验收记录（2026-09-12）。
尚未完成整阶段签署，不代表尚未部署或没有真实测试证据。

## 用户结果

Superlily 应把已经由 OneBot/NapCat 主动推送、且对长期身份或群聊状态有价值的事实结构化
保存，而不是只让它们短暂经过 Runtime。数据库应能回答同一 QQ 号在不同时间、不同群聊
使用过哪些昵称、群名如何变化，以及成员、权限和群内容状态发生过什么变化。

## 本阶段范围

本阶段只补齐现有接收链路中已经出现的事实，不主动轮询平台：

- 群名片与群名称变化；
- 入群、退群、踢出，管理员设置/取消，禁言/解禁，群头衔变化；
- 精华消息设置/取消与群文件上传；
- 好友新增、好友/加群请求，以及机器人离线通知；
- 普通消息发送者当时可见的群头衔和等级。

桥接器继续通过既有 `EventIn.actions` 和 `EventIn.sender` 合同提交事实。Core 把动作写入
`platform_action_observations`，把消息发送者的头衔和等级保存在原始 observation 上；群名片
和群名称事件还必须进入既有名称观测表，形成按实际事件时间排列的历史。

## 失败与隐私边界

- 缺少主体、目标或平台时间时不得猜测；记录为 `partial`/`unavailable` 并保留明确原因。
- 只保留业务字段和有界文本。Cookies、rkeys、鉴权头、完整原始 OneBot payload 和未知嵌套
  字段不进入数据库。
- 请求 `flag` 作为有界的不透明平台标识保存，以便将来关联处理结果；本阶段不新增自动同意
  或拒绝请求的外部副作用。
- 本阶段不做好友/群成员全量轮询、不下载或物化群文件、不回填历史上从未采集到的变化，
  也不改 ChatExporter。

## 验收门

1. Lily 与 Nekro 两套桥接器对上述事件生成相同、可通过 wire contract 校验的结构化动作。
2. 群名片和群名称事件分别产生可追溯的 identity/conversation name observation，时间取平台
   事件时间；空值和缺失时间不被伪造。
3. 普通消息的发送者群头衔、等级经合同进入 Core，并通过线性 Alembic 迁移落库；旧桥接器
   不提供新字段时仍可正常写入。
4. 完整、缺字段、未知字段和重复投递均有聚焦测试，迁移 upgrade/downgrade 可验证。
5. `bridges/**/platform_actions.py` 保持一致，ChatExporter 工作树无改动。

2026-09-04 实现验证记录：620 项可运行回归通过，8 项按环境标记跳过；另有 1 项要求显式配置
`SUPERLILY_TEST_DATABASE_URL` 且数据库名以 `_test` 结尾的 PostgreSQL 破坏性往返测试未运行。
SQLite Alembic head/降级/再升级已通过，不能以生产 PostgreSQL 替代上述专用测试库。

## 生产运行复核（2026-09-12）

北京时间约 11:52，只读查询生产 PostgreSQL `platform_action_observations`，按
`observer_instance_id`、`action_kind`、`capture_status` 聚合；排除本阶段以前已存在的
`poke`、`reaction`、`recall`，得到以下真实观测快照。数量是各机器人观测数，不是跨实例
去重后的平台事件数，也不是人工控制测试用例数。

| 事件类型 | Lily | Nekro |
| --- | ---: | ---: |
| 群成员变动 `group_membership` | 118 | 92 |
| 群名片 `group_card` | 40 | 27 |
| 群文件 `group_file` | 64 | 45 |
| 禁言 `group_ban` | 26 | 0 |
| 精华 `essence` | 5 | 1 |
| 好友申请 `friend_request` | 3 | 0 |
| 群头衔 `group_title` | 1 | 0 |
| 合计 | 257 | 165 |

共 422 条，其中 421 条 `complete`，Lily 的 1 条精华事件为 `partial`。本阶段样本自
9 月 4 日起出现，名片和成员事件持续记录到 9 月 12 日。两端心跳在核查时均为 `online`。
这些证据确认本项已进入生产真实采集，不能继续仅标为“实现完成、待发布”。

仍未完成的验收覆盖：

- 本次聚合未见管理员变动、群名称变化、好友新增、加群请求或机器人离线通知等类型；
  未见样本不等于实现失败，也不能算该类型已通过生产验收。
- 最近 24 小时 `event_observations` 中，两端 `sender_title`、`sender_level` 均未见非空值。
  后续链路排查确认 Nekro 上游普通消息未填写这些字段，详见下节；Lily 上游生成端未独立检查。
  `group_title` 动作或成员快照存在不能替代普通消息字段的生产验证。
- 名片与名称观测的关联已按下节完成只读对账；其他类型、两端同类样本语义及重复投递的
  生产覆盖仍需补齐。
  单条记录的 `complete` 仅表示采集状态，不是整个阶段的签署。

本次只读复核没有制造群事件、修改配置、重启服务或重跑上述自动化测试。

## 链路排查与最小修复（2026-09-12）

### 普通消息头衔与等级

两桥接器分别读取事件 sender 对象和当前事件字典中的 `title`、`level`，经有界标量
转换进入 `SenderRef`；Core 直接将合同字段写入 `sender_title`、`sender_level`。
未提供的值保持 NULL，不能用当前成员快照回填成旧消息发生时的事实。

只读检查 Nekro 正在运行的 `nekro_napcat:/app/napcat/napcat.mjs`：普通消息 sender
初始化仅含 user_id/nickname/card，`handleGroupMessage` 只补 role/nickname，不设置
title/level；独立成员接口的 `OB11Construct` 则读取 memberSpecialTitle/memberRealLevel。
包 SHA-256 为 `b3784a47d32fe27003cb01ce2983654fd564398b48ad1fbd792cbb3d8dab7f50`，
容器镜像 ID 为 `sha256:2df04f8f31d87dc247aabf7ca3660e7d1332ef36ce8145839746b425315f51b1`。
这确认了该部署的普通消息上游字段缺失，不是仅由数据库 NULL 推测桥接丢失。
Lily 上游生成端未在本次独立检查，不把 Nekro 的源码结论扩大到所有平台版本。

生产成员快照另有真实数据：Lily 155,071 条成员快照中 12,162 条头衔非空、155,071 条
等级非空；Nekro 229,739 条中分别为 10,385 和 229,739。这些是快照行数而非独立人数，
属于 C0-F 的时间语义，不是 C0-E 普通消息字段已验收的证据。
本次没有增加按消息查询成员资料的 API 调用，也没有修改 NapCat 或回填生产数据。

### 名片与名称历史关联

对前述 67 条名片动作按 observation ID、实例、成员、会话、名称类型及时间核对：

- 52 条直接关联 `conversation_display_name` 观测（Lily 32、Nekro 20），名称与 card_new
  一致，名称观测时间等于平台动作时间。
- 10 条非空名片没有新增名称行（Lily 7、Nekro 3），但都有此前已存在的同值名称记录；
  6 条来自消息/事件，3 条来自成员快照，1 条来自历史导入。这与当时同值去重行为一致，
  不能把未新增名称行视为动作丢失，也不宣称当前版本会跨所有来源类型去重。
- 5 条是清空名片（Lily 1、Nekro 4），旧值与空新值仍保存在动作事实中；名称表不以空字符串
  新建名称行。读取清空事实应查动作表，不能只凭最后一个非空名称宣称仍在使用该名片。

以上是已有真实事实的关联验证，没有为满足一对一行数而重复插入名称。群改名通知本次仍
没有生产动作样本，其关联由自动化覆盖，不能冒充真实场景已签署。

### 精华事件与修复范围

生产 1 条 partial 精华动作的原因是 `essence message_id missing`，resolver 为 unavailable。
原始 payload 未保留（raw_json 为 JSON null），因此只能确认进入规范化链路时没有可用
目标 ID，不能断言该次上游究竟缺字段还是返回空值。NapCat 精华事件构造依赖本地消息
短 ID 查询；这是可能的来源限制，不是对该历史包的确定归因。不得按发送者或时间猜 ID。

代码检查另发现：精华 sub_type 缺失或未知时曾默认记录为 add。两桥接器已在工作树修复为
`operation=unknown`、`capture_status=partial`，分别记录 missing/unsupported 原因；
add、delete、remove 的已有语义保持不变，未知子类型的有界原值保留在动作 value 中。
这不是对生产那条缺 ID 记录的修补，不重写历史或动作身份，不新增接口或迁移。

本次修复状态：2026-09-12 已部署至两端，短时发布验证通过；两桥接器的
`platform_actions.py` 保持相同。不是整个 C0-E 剩余场景的生产签署。
新增回归覆盖缺失/未知操作、缺目标 ID 不猜测、Core 落库，以及两桥接器发送者字段映射。
自动化测试不调用模型、QQ 接口或生产数据库，不改变上述剩余生产验收边界。

验证结果：`test_platform_action_mapping.py`、`test_c0e_sender_profiles.py`、`test_api.py`、
`test_bridge_payloads.py`、`test_bridge_identity_safety.py` 合计 169 passed（14.26 秒）。
运行时显式移除 `SUPERLILY_TEST_DATABASE_URL`，数据库用各测试的临时 SQLite；
两桥接器动作文件字节一致，`git diff --check` 通过。发送者映射测试执行生产映射表达式，
不启动真实 NoneBot hooks，故不作为 Lily 上游消息字段的生产采样证据。

## 修复发布记录（2026-09-12）

用户明确授权部署后，仅发布上述精华操作语义修复。发布前再次运行 C0-E 映射、发送者
字段与节日规则组合，93 passed；Core、Runtime 镜像、Renderer、数据库 schema 和功能
开关均不变，未混入 R2 或工作树中的其他实现。

- 私有检查点：`/home/justin/backups/superlily/20260912-c0e-1ayzCE`（0700），保存
  `platform_actions.before.py` 和 `platform_actions.after.py`。
- 两端原动作文件 SHA-256：`461bbf31ca19a5070d360b4a617be089b642f97af3cd1ef47dfb5702ff3b4882`；
  本次发布 SHA-256：`4a339d752346135929e02f1606e364fd6157f863107dfd0c30e83ab64a03b0dd`。
- Lily 插件目录是指向工作树 `bridges/lily_nonebot/lily_core_bridge` 的软链接；编辑后磁盘文件
  已变，但旧进程未重新加载。12:23:31 CST 重启既有 `tmux-nb.service`，确认新在线心跳和
  实际入库后再操作 Nekro，不同时停止两端。
- Nekro 仅原子替换 `/home/justin/nekro/plugins/workdir/superlily_bridge/platform_actions.py`，
  保持 root:root/0600；12:24:37 CST 正常重启原容器，12:24:41 bridge 启动，12:25:06
  OneBot 重连。现有插件热重载不能保证移除子模块缓存，因此本次未用热重载代替重启。
- Nekro 镜像仍为 `sha256:cf6f66a458d7754cdaa50c74493c2e28cfd54529f17e878207a5b861f142920a`；
  bridge 入口哈希仍为节日发布版本 `6892c53653d4755e18ccf12805fd0202a75ca24ce0b82e8167ddb39bc27783b4`。
  两端重启后均使用各自部署环境对动作模块执行纯函数检查，未知类型返回 unknown/partial，
  不向生产事件 API 上报该合成样本。
- 12:25 只读复核：两端 online、各 1 bot，queue/pending/quarantine 均为 0，last_error=null。
  连续/最高水位从 Lily `826418/826418`、Nekro `319450/319450` 推进至
  Lily `826518/826518`、Nekro `319519/319519`。04:24:37 UTC 后观察到 Lily 16 条、Nekro
  41 条新 observation；这些包括正常采集及恢复链路记录，不全当作新增聊天消息或 C0-E 用例。
- Nekro healthy，启动窗口未见 ERROR/Traceback；Core、Renderer、worker 镜像和启动时间
  均未变，生产 schema 保持 `0034_qq_media_archive`。节日开关 true、媒体展开/下载关闭，
  两端历史补采沿用原配置。没有额外制造 QQ 测试事件、付费模型请求或故障演练。

以上为文件身份、重新加载和采集恢复的短时发布验证；没有在真实群内制造未知精华通知，
也不将普通流量恢复表述为该罕见事件已完成生产场景验收。

仅回滚此修复：先确认当前目标仍为本次 after 哈希，再用检查点 before 文件恢复两端
`platform_actions.py`，保持各自权限并按同样顺序错开重启、核对在线和水位。
Lily 目标实际位于工作树，回滚会回退该文件，必须保留之后新增的修改；不要覆盖整个
bridge 目录、回退 Runtime/Core 镜像或恢复整库。检查点与已采集数据保留。

## 发布与回滚

生产已有两端真实入库证据；后续补齐验收时核对实际部署身份、心跳、spool pending、错误
日志和对应真实样本，不以代码完成、在线时长或单类事件成功代替整阶段签署。
正常回滚恢复兼容的桥接器版本，保留生产 schema 和已提交事实；不得为回退功能直接
downgrade 或删除已有采集证据。需要迁移回退时另行评估兼容性、备份及数据保留方案。
