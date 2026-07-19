# ADR 0013：文本 Wolfram Provider 与既有隔离 worker 的边界

- 状态：accepted
- 日期：2026-07-19
- 细化：ADR 0002、0003、0007、0011、0012

## 背景

第三阶段已经用 `status.inspect@1.0.2` 证明共同 descriptor、Provider inventory、
调用账本、lease/fence、Git-bound rollout 和故障恢复协议，并用 `0016` 建立确认与
artifact 账本。下一个真实工具是 Lily 已长期使用的 `/wf`：它通过私有 Unix socket
调用持久 Wolfram 15.0 worker，可以返回文本、图片或音频。

直接把旧命令函数登记为工具会把命令解析、OneBot 图片下载、计算、图片压缩、语音
转换和平台发送重新揉在一个 Provider 中，也无法诚实声明网络、文件、artifact 和
取消边界。另一方面，为了 Registry 重写已经稳定运行的 Mathematica kernel 生命周期，
会引入与本次迁移无关的许可、启动延迟和隔离风险。

## 决定

1. 首版新增不可变 `wolfram.run@1.0.0`，只接受一段精确 Wolfram Language 表达式，
   只返回 `{kind: "text", text: ...}`。它拒绝图片输入、远程抓取、图片、音频、任意
   URL、平台发送和 artifact；`natural_language=false`，caller 只开放 `command` 与
   `admin_api`。模型规划仍属于 Phase 5。
2. 新的 `provider-wolfram-primary` 是独立 Superlily Provider。它持有自己的 Core
   credential，通过 `superlily-provider-pull-v1` 领取精确 lease，但不持有 bot、平台
   发送或 Core 管理 credential。它不在 Core API 进程内加载 Wolfram 插件代码。
3. Provider 不启动新 kernel，而是通过只读 bind mount 暴露的 `0700` 目录和 `0600`
   Unix socket 调用现有持久 worker。Provider 以 uid/gid 1000、只读 rootfs、空 capabilities
   和 `no-new-privileges` 运行；每次连接前拒绝软链接、宽权限、非 socket 或非当前 uid
   所有权。请求和响应都是单行、有界、严格 JSON；不做客户端自动重试。
4. 计算表达式只进入现有 worker。worker 不得到 Provider token、lease secret、bot
   token、Core URL 或平台发送能力；其 eth0 关闭，只保留无默认路由的 dummy renderer
   接口。descriptor 中的 `network=deny` 指计算 sandbox 的数据面；Provider 到 Core 的
   bus 连接是 lease 控制面，不是表达式可使用的联网能力。
5. `filesystem=subprocess=sandbox_only` 表示 Wolfram kernel 及其必要子进程只能在已审阅
   的 worker 容器中运行；合同因此把 `subprocess` 从只有 `deny` 扩成
   `deny | sandbox_only`。这不是允许 Core、Provider 或模型在宿主机自由执行 shell。
6. worker 部署身份绑定精确 Docker image ID、`server.py` SHA-256、
   `kernel_wrapper.py` SHA-256、Wolfram 引擎版本和规范化隔离配置 SHA-256。Provider
   implementation hash 再绑定自己的 `main.py`、`runtime.py` 与 worker 身份。Core
   仍把它视为经认证 Provider 的 inventory 声明，不把哈希误称为硬件远程证明；生产
   导入和 canary 前必须由运维重新核对实际容器。
7. 当前审阅的隔离配置是 uid/gid 1000、有效 capability 为 0、
   `NoNewPrivs=1`、只读 rootfs、4 GiB memory、8 GiB memory+swap、512 PIDs、
   worker 并发 1、私有 socket 权限和 worker 降权后不可读 license。其规范化配置哈希为
   `fb22eb99ca232233129d365f3b0ec644a6dfbe09e28b4d526f6f9b1a5a1cd083`。
   当前 worker identity 为
   `edaed08c24d55e213f2d005c7a758c46f3ec76641ae2389e74bf0ce13e2ce030`。
8. descriptor 将 wall time、input bytes、output bytes 和 memory 标为 hard。前三者由
   Provider 的本地 deadline、严格传输/JSON/schema/字节上限执行；memory 由单并发
   worker 的 4 GiB cgroup 上限执行。当前只能证明容器级上限，不能精确观测单次请求
   峰值，因此 usage 不伪造逐请求 memory 数字。旧 `/wf` 与新 Provider 共用 worker
   期间，4 GiB 仍是总硬上限，但旧请求可能争用容量；对比应串行，不做双执行 shadow。
9. Provider 只在初始 lease 尚新鲜且绝对 deadline 足够时发出 start。start 后定期
   heartbeat 续租，本地执行上限取 descriptor 60 秒与 Core 绝对 deadline 的较小值，
   不把初始短 lease 错当作总执行时间。
10. 旧 socket 协议在客户端断开后不能证明 kernel 已停止。因此 heartbeat 看到取消、
    Core/Provider 通信变得不明确或 Provider 自身被取消时，只会断开客户端并停止续租，
    让 Core 保守收敛为 `unknown_completion`；绝不伪造 `cancelled` ACK。普通超时可按
    worker 内部 timeout 与本地 wall-time 报为 `timeout`，但不会自动重跑。
11. worker 原始错误、表达式、本地路径和任意二进制结果不进入 Core 错误详情或日志。
    Provider 只发稳定、有限的 `timeout/execution_failed/invalid_output/internal_error`
    分类。非文本、额外字段、错误类型、畸形 JSON、超长响应和输出 schema 漂移全部
    fail closed。
12. 既有 `/wf` 在迁移期间保持原路径和原发送行为，作为即时回滚。新路径先只报告，
    再以完整 Git commit 导入 reviewed authority，随后通过 M1 激活、一份精确单次
    `admin_api` plan 和无平台发送 canary。稳定窗口和串行结果对比签署前，不替换旧命令；
    任何异常优先 pause plan、退回 `ledger_only` 或停止新 Provider。

## 后果

这一包把真实数值计算纳入共同执行账本，但没有完成图片 Wolfram、LaTeX、统一渲染、
自然语言选工具、进度消息或 QQ 发送。图片与音频必须先经过 `0016` artifact 的独立生产
canary；选择文本、TeX、Markdown 图片或夜间主题属于 Phase 4 renderer 与 Phase 5
planner 的组合，不得塞回 `wolfram.run@1.0.0`。

现有 worker 不是通用不可信代码执行平台。未来 Python/shell、任意文件访问、联网数据
或用户上传 notebook 必须另做 sandbox descriptor 和独立威胁模型，不能引用本 ADR
自动获得权限。

## 必需证据

- descriptor 规范化/hash、Provider 身份、`sandbox_only` 合同和 required hard budget
  双数据库回归；
- Unix socket 所有权/类型/权限、严格 health、文本成功、非文本、原始错误、额外字段、
  畸形/超长响应、UTF-8 字节上限和真实 wall-time 测试；
- heartbeat 跨初始 lease、取消不发假 ACK、Core/Provider/worker 中断与 late completion
  收敛测试；
- 生产重新核对 image/source/version/cgroup/capability/network/license/socket，并记录
  worker identity 与 implementation hash；
- `ledger_only` 零 lease 空转、精确一次 `admin_api` canary、旧 `/wf` 串行结果对比、
  plan pause/Provider stop 回滚和至少一个完整 inventory 稳定周期。
