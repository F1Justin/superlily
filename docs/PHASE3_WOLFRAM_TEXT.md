# Phase 3：文本 `wolfram.run` 实施与上线验收

本文把 ADR 0013 落成可执行检查表。首版目标不是“把 `/wf` 的全部功能搬进 Registry”，
而是证明第二个真实 Provider 能在不获得平台发送和模型调用权限的情况下，复用既有
Wolfram 15.0 隔离 worker，完成一条有界文本计算。

## 本包交付物

- `wolfram.run@1.0.0` 不可变 descriptor 与 `provider-wolfram-primary` authority；
- 独立 Provider SDK 进程、私有 Unix socket client、严格文本结果校验和安全错误分类；
- `subprocess=sandbox_only` 合同；
- worker deployment identity 与 Provider implementation hash；
- Compose 中默认不启动、没有有效 token/identity 就 fail closed 的 Provider 服务；
- 单元/合同/SDK、SQLite/PostgreSQL 全量、镜像和真实 worker 探针证据；
- reviewed -> active -> 精确单次 canary -> paused 的生产治理证据。

## 已冻结的 authority

| 项目 | 值 |
|---|---|
| tool | `wolfram.run@1.0.0` |
| descriptor SHA-256 | `aa6e9b1c930406bab11500de6c7653219aa9e8b831ee5fc7d08b1ab3d239ddaa` |
| provider | `provider-wolfram-primary` |
| protocol | `superlily-provider-pull-v1` |
| caller | `command`, `admin_api` |
| natural language | 关闭 |
| 输出 | 最多 2000 字符、16 KiB 的文本对象 |
| artifact / 平台发送 | 无 |
| timeout / concurrency | 60 秒 / 1 |
| rate limit | 每 sender 每 60 秒 5 次 |
| memory | worker cgroup 总上限 4 GiB |
| worker identity | `edaed08c24d55e213f2d005c7a758c46f3ec76641ae2389e74bf0ce13e2ce030` |
| implementation hash | `32996c572eb8f364463666e0126a35b77efa21ae03fe29d710bfa7377645a241` |

implementation hash 只要 `main.py`、`runtime.py` 或 worker identity 改变就必须重算；
上表不是允许用旧值覆盖新代码。生产导入必须来自包含这些精确文件的完整 Git commit。

## 实现前与本地门

1. 读取并保留 `/home/justin/lily` 的 dirty worktree，不覆盖既有 worker 修改；核对宿主
   文件与运行容器里的 `server.py`/`kernel_wrapper.py` 是否一致。
2. 核对 worker image ID、Wolfram 版本、uid/gid、capability、NoNewPrivs、rootfs、
   memory/swap/PIDs、网络、license 和 socket 权限，生成规范化 sandbox profile hash，
   再计算 worker identity。
3. descriptor、Provider authority 和实现必须通过定向合同测试；固定 `2+2` 探针只读
   现有 worker，不发 QQ 消息。
4. 跑完整 SQLite 与 PostgreSQL 17 套件、migration head/drift、descriptor/Provider
   authority 校验、Compose config、镜像构建和 `pip check`。
5. review secret 泄漏、表达式/worker 原始错误泄漏、客户端重试、错误取消 ACK、socket
   替换、传输超限、非文本和预算谎报。任一项不清楚就不进入生产。

## 生产上线顺序

1. 保持 Core 为 `ledger_only`、global stop 为 false、artifact 默认关闭，确认无 active
   rollout plan、lease 或 running attempt；PostgreSQL 不需要新迁移。
2. 为新 Provider 生成独立随机 token，同时原子加入 Core 的 provider token map；不得
   复用 status、ingest、admin 或 bot token，也不得把 token 写入 Git/日志/文档。
3. 从完整 Git commit 导入 provider registration 和 `wolfram.run@1.0.0` 为 reviewed。
   此时即使 Provider 健康也不应 eligible，更不能领取 lease。
4. 只启动 `wolfram-provider`，验证 inventory/heartbeat 的 descriptor、implementation、
   worker identity、max concurrency、hard budget 和健康探针；观察零 lease/attempt。
5. 通过 M1 reviewer 控制面 preview/CAS 把精确 descriptor 激活。禁止直接 SQL 更新。
6. 提交并导入一份完整 Git commit 中的单次 rollout plan，精确绑定 descriptor hash、
   provider、`admin_api`、一个指定会话、资源版本、短窗口和 `max_invocations=1`；通过
   operator 控制面激活。
7. canary 只提交固定、快速、纯文本表达式，例如 `2+2`，检查一个 invocation、一个
   attempt、单调 fence、heartbeat、usage、结构化输出 `4` 和零 artifact/零平台发送。
8. canary 后立即 pause plan，确认再调一次不会新增 attempt；再与旧 `/wf` 对等表达式
   做串行结果/延迟比较，不双执行同一请求。
9. 至少跨一个完整 inventory 周期观察 Provider/Core/worker 日志、queue、deadline、
   worker requests、内存与旧 `/wf`。证据签署后仍保留旧命令，不在本包切流。

## 失败与回滚

- 最小回滚是 pause 精确 plan；其次是 descriptor suspension 或 Provider quarantine；
  再其次把 Core 保持/退回 `ledger_only` 并停止 `wolfram-provider`。
- 不停止、不重建、不升级既有 Wolfram worker；旧 `/wf` 应继续可用。
- 不删除 invocation、attempt、inventory、heartbeat 或事件；不直接 SQL 修成功状态。
- 取消或链路中断后没有 worker 停止证明时，允许账本留下
  `unknown_completion`，不得为了“好看”改成 `cancelled` 或自动重跑。
- worker image/source/隔离配置任一漂移时，旧 implementation 必须自然失配；重新审阅
  identity、inventory 和 rollout，而不是复用旧哈希。

## 本包退出门

以下项目全部成立才把文本 Wolfram 标为 Phase 3 已迁移：

- 双数据库全量、镜像和真实 worker 探针通过；
- 生产 Provider 身份与 hard budget 诚实，`ledger_only` 空转零执行；
- 精确一次 canary 成功，账本、fence、usage、输出和无副作用证据完整；
- plan pause 后零新 lease，旧 `/wf` 无回归；
- 至少一个完整 inventory 稳定周期无异常；
- 代码、中文 ADR、验收证据、部署记录来自可追溯 Git commit。

完成本包后，Phase 3 尚剩 `latex.render` 和至少一个真实 artifact canary。Wolfram 图片、
TeX/Markdown 渲染成图、夜间主题、进度消息和模型自行选工具分别属于 artifact、Phase 4
和 Phase 5，不因文本 canary 自动开放。

## 2026-07-19 生产签署

本包已按上述顺序完成。descriptor 为 `active/rv2`、Provider 为 `active/rv1`；唯一
Git-bound plan 精确消费 1/1 后停在 `paused/rv3`。invocation
`27614162-8c70-42e3-af5a-db3f72a2a55e` 只用一个 attempt/fence 返回文本 `4`，
wall=8 ms、input=20 bytes、output=26 bytes、artifact=0；旧 `/wf` data source 串行
对比也返回 `4`，没有 QQ 发送。

Core 已恢复 `ledger_only`，active plan/attempt 为 0，临时控制面关闭，明文临时凭据
销毁；PostgreSQL 与既有 worker 均未重启。发布中发现的 Compose scrypt `$ -> $$`
转义和强制 HTTPS Origin 已写入 `CONTROL_PLANE.md`。完整镜像、提交、账本、回滚与
C0-D 连续性证据见 `DEPLOYMENT.md` 第 17 节。最后 300 秒窗口收到两份相同 hash 的
inventory 与 10 次 healthy heartbeat，Provider 零新日志、相关容器零重启。

## 2026-08-13 worker 恢复与身份轮换

这次事件不改写 2026-07-19 的签署记录，也不扩大 `wolfram.run@1.0.0` 的执行
authority。故障根因是宿主权威 `mathpass` 已刷新，而容器仍挂载旧的运行时副本；
同时容器实际 restart policy 漂移为 `no`。宿主和生产等价容器都实测
`$MachineID=6520-06891-19277`，因此没有证据支持“每次重启 MathID 改变”。

恢复后 worker 使用 Wolfram 15.0.0、镜像
`sha256:a3063934e96aabc8bac4824129e7ce3e8de91457d85dd18cf6654bfd02c5bc7d`，
reviewed worker identity 为
`e1e6a7132f8f7cfc27ee8c63544fab455c182748bcbc3a0d5e3fc0aa312b68db`；
与本次 Provider 源码绑定的 implementation hash 为
`0c897466009aba222d123931a3da296fcb0d3898912841200f11af1d193e5258`。
旧 identity/hash 只保留为历史证据，不能用于新 inventory 或 rollout。

worker 仍保留独立 OS 沙盒：只读 rootfs、私有 tmpfs、uid/gid 1000、有效
capability 为 0、NoNewPrivs=1、内部网络断路、许可证就绪后不可读。AgentRun 的
lease、预算、fence 和 caller 限制不能替代这层边界，因为它们约束“谁可调用和调用
多少”，不约束被调用的 Wolfram 表达式在内核进程中能读取或连接什么。

加固后的 Docker 路径包括：许可证权威预检与原子 root-only 副本、live-kernel
healthcheck、计算超时和静默内核死亡后的容器级重引导、连续失败最多五次、镜像/
源码/compose/capability/tmpfs/挂载/网络/稳定态的部署漂移检查，以及仅在开机时启动
既有且精确匹配 reviewed deployment 的容器。宿主真实身份 systemd worker 作为
离线候选保留，未安装、未切流；切换它仍需完整 smoke、性能和显式生产门禁。
