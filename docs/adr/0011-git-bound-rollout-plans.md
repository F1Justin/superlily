# ADR 0011：Git-bound 精确 rollout plan 与执行权限上限

- 状态：accepted
- 日期：2026-07-19

## 背景

M0–M2 已分别建立控制会话/审计、descriptor lifecycle 和 Provider quarantine，
`0015_tool_attempts` 也已有可恢复 lease/fence/attempt 协议。但旧设计仍把
canary/enforce 精确 scope 放在环境变量中：它既缺少 Git 审阅链和持久 lifecycle，
又会形成与数据库控制面平行的第二套权限来源。进程重启、配置漂移或一次错误的
Compose 变量都可能改变执行范围。

这项治理也不应变成对模型行为的人工编排器。模型以后可以按渐进式披露自主选择
工具、读取资源，或在受控执行环境中组合 `rg`、`grep` 和管道；M3 只回答生产
authority 问题，不规定“遇到什么问题必须调用什么工具”。

## 决定

1. 新增 `0015d_rollout_plans`。Git 中的 plan 是不可变 authority，Core 只从精确完整
   commit 读取、规范化和校验后导入为 `reviewed`，不会自动激活。数据库保存 plan
   hash、commit、reviewer、原始 canonical authority、精确 item 和 lifecycle 证据。
2. 首包只接受最长 24 小时、带 1–1000 次全局调用上限、回退目标固定为
   `ledger_only` 的 `canary` plan；`enforce` 明确关闭。每项必须精确绑定
   tool/version/hash、canonical conversation、caller、Provider，以及 descriptor 和
   Provider 的 expected resource version；不接受通配符，也不能让同一执行目标选择
   两个 Provider。
3. `SUPERLILY_TOOL_EXECUTION_MODE` 只是权限上限：`off < ledger_only < canary`。
   环境 canary/enforce scope 被废止；非空旧变量会使 Core 启动失败，数据库 plan 也
   不能越过 `off`、`ledger_only` 或 global stop。
4. 同一时刻最多一个 plan 处于 `active`。operator 必须基于服务端 canonical preview、
   新鲜再认证、expected resource version、CAS、幂等键和有界原因执行
   `reviewed -> active`、`active -> paused` 或 `paused -> active`。break-glass 只能
   `active -> paused`，不能增加 authority。
5. 调用创建先锁定 active plan，精确匹配 item，并在同一事务中原子增加调用计数。
   无 plan、范围不匹配、计划过期/暂停、资源版本漂移、global stop 或额度耗尽时，
   请求安全降级成 `recorded_only / rollout_fallback_ledger_only`，不会排队；真正的
   输入、身份、能力、runtime 或限速错误仍是 `rejected`。
6. invocation 冻结 plan/item ID、plan hash/resource version、预期资源版本和选择的
   implementation hash。Provider 领取时再次锁定 plan 并重验 lifecycle、窗口、精确
   item、descriptor/Provider 版本和 runtime。计划暂停与 lease 使用同一 plan 行锁，
   因此暂停接受后不能产生新 lease，暂停前遗留的 queued 调用也不能绕过新版号。
7. 激活是增权，apply 时任何 preview 或 runtime 漂移都拒绝。暂停是降权：只要同一
   session、精确 plan 和 lifecycle/resource version 仍匹配，调用计数或 active
   invocation 的变化不得阻止暂停；接受证据同时保存操作者实际看到的 preview hash
   和服务端重算 hash。
8. plan authority、item 和 lifecycle event 只追加且不可删改；计数只能逐次加一，
   不得删除、回退或批量跳变。过期不会删除历史，恢复或再次激活也是新 mutation。

## 后果

M3 关闭了“环境变量临时拼 scope”这条旁路，并让每次 canary 调用都能追溯到精确
Git authority 和数据库 lifecycle。它仍不授权自然语言 caller、不激活 descriptor、
不开放 `enforce`，也不替模型做路由。没有 active plan 时把执行上限设为 `canary`
仍只会得到 `recorded_only`，Provider 领取不到工作。

首个真实 canary 仍需独立配置 operator、激活 `status.inspect@1.0.2`、提交并导入一份
短时单次 plan、演练 global stop/descriptor suspension/Provider quarantine/plan pause，
以及 Core/Provider/数据库中断恢复。M3 默认禁用部署本身不等于这些生产权限已经签署。

## 必需证据

- 严格合同、Git-bound 导入、无通配符、最长窗口、调用上限和 `enforce` 关闭；
- 两种数据库的 lifecycle、角色矩阵、CSRF/再认证、preview、CAS、幂等、限速、
  不可变 trigger、迁移往返和 drift；
- PostgreSQL 并发调用只有一个额度胜者，pause/lease 行锁证明暂停后零新 attempt；
- 无计划、范围/版本漂移、过期、暂停、global stop 与额度耗尽均降级为 ledger-only；
- 默认空 operator、零 rollout authority、零 attempt 的生产备份恢复与部署证据。
