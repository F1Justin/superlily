# ADR 0014：LaTeX Artifact Provider 与无凭据渲染 worker 边界

- 状态：accepted
- 日期：2026-07-19
- 细化：ADR 0002、0003、0007、0011、0012、0013

## 背景

`0016_confirm_artifacts` 已经建立 Provider/attempt/fence 绑定的
reserve/upload/finalize 协议，但生产 artifact 存储仍默认关闭，也没有真实工具走完
这条路径。Lily 现有 `/tex` 可以调用宿主 XeLaTeX 和 Poppler 后直接发 QQ 图片；它把
命令解析、模板拼装、子进程、图片字节和平台发送放在同一个插件进程里，还会把原始
公式和编译输出写入日志。直接把这段函数登记为 Provider，会绕过共同 artifact 账本，
也无法诚实声明文件、进程、网络、日志和取消边界。

第三阶段的最后一个代表性工具必须证明“结构化调用 -> 隔离渲染 -> 内容寻址产物 ->
账本成功”能够成立，而不是提前实现第四阶段的主题、通用 RenderDocument 或平台发送。

## 决定

1. 新增不可变 `latex.render@1.0.0`。输入只有一段精确 `latex` 字符串；输出只是一份
   已终结 PNG 的 artifact 引用及 MIME、SHA-256、字节数和尺寸。`natural_language`
   继续关闭，caller 只允许 `command` 与 `admin_api`，工具本身没有平台发送能力。
2. `provider-latex-primary` 持有独立 Core Provider credential，通过
   `superlily-provider-pull-v1` 领取 lease。Provider 不加载 NoneBot，不持有 bot、
   admin、ingest 或控制面 credential，也不把图片直接发给 QQ。
3. XeLaTeX 和 Poppler 只在独立 worker 容器中运行。worker 没有 Core URL 或任何
   credential，网络模式为 `none`，rootfs 只读，以 uid/gid 1000 运行，删除全部
   capability，启用 `no-new-privileges`，限制为 1 CPU、1 GiB memory、128 PIDs 和单
   并发。只有 `/work` 与 `/tmp` 是有界、`noexec/nosuid/nodev` 的 tmpfs。
4. worker 只读挂载精确 TeX Live 2024、系统字体和 fontconfig cache。Provider 只读
   挂载 `0700` 目录中的 `0600` Unix socket；连接前拒绝软链接、宽权限、非 socket 或
   非当前 uid 所有权。公式数据面不能使用 Provider 到 Core 的 bus 网络。
5. worker 使用 `--no-shell-escape`、`openin_any=p`、`openout_any=p` 和私有
   TEXMF/HOME。编译日志、原始公式和本地路径全部丢弃，不进入 worker/Provider/Core
   日志或安全错误。单次编译、PDF 检查和 PNG 转换分别超时；PDF 必须单页且页尺寸、
   PDF 字节、PNG 字节和 PNG 尺寸都有硬上限。
6. Provider 与 worker 使用有界的 Unix framed 协议：请求是严格 JSON，响应是严格
   JSON 头加精确长度的 PNG 正文。Provider 独立复验 MIME、字节数、SHA-256、IHDR
   尺寸和 descriptor 上限；Core 上传时再独立解析 PNG，不能信任 worker 自报元数据。
7. 成功必须严格依次完成 reserve、一次 upload、finalize 和带精确 artifact reference
   的 invocation complete。上传或完成响应不明确时不自动重试、不伪造成功；孤儿或
   未引用对象由 `0016` reaper 按既有规则收敛。
8. descriptor 的 `network=deny`、`filesystem=subprocess=sandbox_only`、无 secret、
   `artifacts=[image/png]` 和 4 MiB/2048×2048/单产物策略是不可提升参数。wall time、
   memory、input/output/artifact bytes 必须由 Provider inventory 报为 hard，否则工具
   不可执行。
9. 当前规范化 sandbox profile 位于 `deploy/latex-worker-sandbox-v1.json`，SHA-256 为
   `0bdca3208a8b183937ffb4f6f4cda908a731ed709122440a76c781b953c2b492`。当前 worker
   镜像为 `sha256:845faf7b8caecf17540c1933a9a764b5c13865b5f57597a741aa27b3d75b69bc`，
   worker source SHA-256 为
   `fcd58c8983d2ff349afeca938f9b77314e33cc50a8d78ba6455a6a9cff0abc83`，模板
   SHA-256 为 `b83ead47c77beb2f778aa9f366d5d3b006e91afa4d86c66a8ae71a5c35f65780`。
   连同 XeTeX/Poppler 版本生成 worker identity
   `5fec6df87bbfda7666c2e47018763d080e2b99049cea57d24e3ad4bc160e848a`；Provider
   implementation hash 为
   `79c93de77f01b4694dbf4b0a2456148fb4f4082c8092ceb794ccf8f8674c6f0c`。
   这些哈希是运维核验过的部署身份，不是硬件远程证明；任一输入漂移都必须重新审阅。
10. 客户端断开不能中止已经进入 worker thread 的同步 TeX 子进程，因此取消或 Core
    链路不明确时 Provider 只停止续租，不发送虚假的取消 ACK。worker 的纯计算仍受
    超时/cgroup/tmpfs 约束，Core 允许保守收敛为 `unknown_completion`。
11. 现有 `/tex` 继续保持原实现和发送路径，作为即时回退。新 Provider 先以
    `ledger_only` 只报告，再由 reviewer 激活 descriptor，最后只执行一份固定公式、
    最多一次、无平台发送的 Git-bound canary。第三阶段不把旧命令切到新 Provider。

## 后果

这项决定证明真实二进制产物可以进入共同执行账本，但不建立通用 Renderer。夜间黑底
白字、Markdown 转图、Wolfram 图形、长文卡片、平台能力降级和 artifact 取回/发送都
属于 Phase 4；模型决定何时调用 LaTeX 属于 Phase 5。工具产物和平台发送继续是两个
独立、可观察的步骤。

TeX 是复杂解释器，本边界只适合公式渲染，不等于通用不可信代码沙箱。未来允许用户
文件、远程资源、自定义字体、HTML/SVG 或任意 shell 时必须另做威胁模型，不能沿用本
ADR 自动扩大权限。

## 必需证据

- descriptor、Provider、artifact policy 和 hard budget 的合同及双数据库全量回归；
- 私有 socket、严格 framing、MIME/hash/尺寸/字节不匹配、恶意 TeX、超时和错误脱敏；
- worker 的 network/rootfs/capability/cgroup/PID/tmpfs/uid、版本和部署 identity；
- Core artifact 存储启用前备份/恢复，reserve/upload/finalize/reference/reaper 证据；
- 单次固定公式 canary、零平台 response、计划暂停、旧 `/tex` 串行对比和稳定窗口。

