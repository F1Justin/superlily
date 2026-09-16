# 元旦／跨年皮肤中午切换

2026-09-16 已按用户要求上线：`new_year` 在北京时间 **2026-12-31 12:00（含）至 2027-01-01 12:00（不含）**使用，共 86400 秒，覆盖跨年倒计时和元旦早晨。1 月 1 日中午恢复默认皮肤。其他 15 款时间窗口保持原配置。

日期表支持本地开始时刻，原有窗口选择逻辑直接解析该时刻；主题版本随日期表变化，缓存与过期投递沿用既有窗口边界检查。装饰、配色、文字字体与数学渲染没有变化。

## 验证

- 72 项聚焦测试通过，包括元旦零点持续显示、两个中午的精确边界，以及切换期间渲染重试、旧产物到期和缓存更新。
- 生产 Core 验证全部 16 个窗口，跨年开始前的默认窗口截止于 12 月 31 日中午，跨年窗口截止于 1 月 1 日中午。
- 生产 default 与 new_year 多文字成图哈希与调整前一致；Core 渲染、产物下载、重复请求缓存通过。
- 三项服务主题模块一致；镜像继承更新前生产基础，worker 代码、引擎版本与沙箱未变；模板身份绑定更新后的主题版本，并已同步至 Core 缓存实现身份。Core 和 gateway 健康，worker 实际渲染成功，重启计数为 0。
- 实际跨年日期尚未到来，上述日期验证使用显式测试时刻。未发送平台消息。

最新身份与证据：[发布锁](../deploy/new-year-window.lock.json)。日历口径同步至 [节日清单](FESTIVAL_CALENDAR_20260916_20270914.md)。此前扩展发布锁保留为历史快照。

## 发布与回滚

使用 `deploy/Dockerfile.festival-expansion`，三个 base 参数分别设为 `superlily/core:pre-noon-20260916`、`superlily/renderer:pre-noon-20260916`、`superlily/worker:pre-noon-20260916`，构建各 target 为 `superlily/<role>:noon-20260916`。仅更新主题模块；worker 资产与原版本一致。

检查点：`/home/justin/backups/superlily/20260916-new-year-noon/`。恢复前先确认此后没有其他发布，再将上述三个 pre-noon 镜像标签恢复到对应 `deploy-*` 标签，并将 `.env` 的 `SUPERLILY_RENDER_IMPLEMENTATION_HASH` 恢复为锁内 `previous_worker_identity`。只重建 `latex-worker document-renderer lily-core`，不整体覆盖 `.env`。

```sh
docker compose --env-file /home/justin/superlily/.env -f /home/justin/superlily/deploy/compose.yml up -d --no-deps --no-build --timeout 60 latex-worker document-renderer lily-core
```

回滚后检查健康、实现身份与渲染链路。
