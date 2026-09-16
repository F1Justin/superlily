# 万圣、圣诞与春季六节气发布

路线图归属：[R1.2：节日渲染皮肤](ROADMAP.md#r1-2)。

2026-09-16 经用户批准，已部署并启用万圣夜、圣诞、立春、雨水、惊蛰、春分、清明、谷雨八款皮肤。加上原八款，共 16 款。日历与待设计候选见 [完整清单](FESTIVAL_CALENDAR_20260916_20270914.md)。

元旦／跨年皮肤已调整为北京时间 2026-12-31 12:00 至 2027-01-01 12:00；其他皮肤在配置日期 00:00 至次日 00:00 生效。均左闭右开、恰好 86400 秒。节气按当天整日使用，不以交节时刻开始。保留无装饰文字、无顶底分隔线、实际 TeX 数学与多文字排版的约束。最后配置日期为 2027-04-20；没有自动推算下一年节庆。

节气设计范围：仅春季六节气做系列，现已全部上线；保留已有冬至皮肤。其他季节的节气不列为制作计划，后续从传统节日、中外节日和纪念日中挑选。

## 交付与验证

- 批准的 SVG/PNG 原样打包；更新素材 manifest、主题版本、Core 缓存实现身份。Core、gateway、worker 使用相同主题模块。
- 以更新前正在运行的三项服务镜像为基础，`deploy/Dockerfile.festival-expansion` 仅覆盖主题模块与 worker 装饰资产。已验证新镜像继承各自生产基础镜像的全部层，保留较新的 Core 与多文字字体修复。
- 聚焦回归 97 passed：节日边界、多文字、LaTeX provider、rendering、Markdown、安全矩阵。
- 候选 worker 在隔离沙箱渲染 default + 16 款皮肤，扩展文字、样式和不支持字符的明确失败检查通过；编译未出现缺字警告。
- 生产 Core 内核对 16 个日期的开始前、开始、结束前、结束边界，每个窗口为 86400 秒，开关为 true。
- 生产 gateway → worker 使用群里原报告分别渲染八款新增皮肤，每张 SHA256 与用户批准预览完全一致。圣诞完整长图底部礼物、冬青可见。
- 生产 Core → gateway → worker → artifact 的多文字请求、产物下载与重复请求通过；下载哈希与候选 default 一致。仅使用内部合成预览会话，没有发送平台消息。
- Core 与 gateway healthy，worker 经 gateway readiness 与实际渲染验证；三项服务重启计数均为 0。采集连续水位等于已见水位且持续前进，心跳正常。

元旦跨年窗口调整的最新身份和验证见 [跨年调整记录](NEW_YEAR_WINDOW_20260916.md)。以下为八款扩展发布时的验证与检查点。

具体镜像、沙箱、主题版本、哈希与验证记录在 [发布锁](../deploy/festival-expansion.lock.json)。成图保存在 `/home/justin/.codex/visualizations/2026/09/16/festival-release/`；私有检查点为 `/home/justin/backups/superlily/20260916-festivals/`。验证是技术与模拟日期验证；首个真实自动节日窗口仍为 2026-09-25。

## 发布与回滚

发布仅重建 `latex-worker document-renderer lily-core`，没有数据库迁移。`.env` 仅更新 `SUPERLILY_RENDER_IMPLEMENTATION_HASH`，其他配置保留。

如需撤回本次新增主题，使用本次检查点的镜像与 `previous_worker_identity`，不要使用 9 月 9 日的历史 Core。先确认此后没有新发布，再恢复三个标签与 `.env` 中这一项身份字段，随后执行相同的限定服务 Compose 更新：

```sh
docker tag superlily/core:pre-spring-20260916 deploy-lily-core
docker tag superlily/renderer:pre-spring-20260916 deploy-document-renderer
docker tag superlily/worker:pre-spring-20260916 deploy-latex-worker
docker compose --env-file /home/justin/superlily/.env -f /home/justin/superlily/deploy/compose.yml up -d --no-deps --no-build --timeout 60 latex-worker document-renderer lily-core
```

不整体覆盖备份 `.env`，以免撤销后续其他配置。只停用全部节日展示时，可将 `SUPERLILY_RENDER_FESTIVAL_ENABLED=false` 后仅重建 Core。回滚后重新检查 readiness、渲染、缓存身份和采集水位。
