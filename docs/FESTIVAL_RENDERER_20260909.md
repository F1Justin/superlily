# 节日渲染皮肤：2026 中秋至 2027 元宵

已于 2026-09-09 部署，服务 MANIFESTO 第 1 条：群聊中同一份莉莉正文在节日自动采用已确认的皮肤。
这是服务端确定性样式，不增加模型提示词、模型工具参数或装饰文字。

路线图归属：R1 Renderer 的已交付展示增强，独立记录其验证边界，不另设排队阶段。
状态：已部署、已启用、技术验证完成；首个真实节日窗口尚未到来（2026-09-12 复核）。

2026-09-14 后续更新：worker 已发布多文字字体修复，身份由
[多文字部署锁](../deploy/multilingual-renderer.lock.json) 承接；下文 9 月 12 日身份为历史快照。
本次没有更改节日规则或装饰，见 [多文字渲染记录](MULTILINGUAL_RENDERER_20260914.md)。

2026-09-16 已扩展为 16 款，新增万圣夜、圣诞及春季六节气，见
[最新发布记录](FESTIVAL_EXPANSION_20260916.md)与[日历清单](FESTIVAL_CALENDAR_20260916_20270914.md)。

## 历史生产复核（2026-09-12）

- Core 的节日开关仍为 true，worker implementation hash 仍为
  `840a36c24e8a1425595848aabf1f52982f183ed2ad0b156d9adb4d53338c0412`。
- 生产规则模块 SHA-256 为 `e94dfc114a393e40735fd7743487c3e6bf48c00f994b7c658cbb40e71afbbd00`，
  与工作树日期表一致；实际调用生产 `theme_window` 返回 default，截止 9 月 25 日 00:00 CST。
- Renderer 和 worker 镜像仍分别为发布锁中 `d746fc2…` 与 `6f9086b…`；Core 已于 9 月 12 日
  更新为 `sha256:2d57b4c23a8ba6bc5f1f8b87f8c91b401e2d4fb6fddacd4d76b3e2a3f19d1a59`，
  属于后续可逆消融，保留节日能力，见 [ABLATION_20260912.md](ABLATION_20260912.md)。
- 核查时 Core/Renderer healthy、worker running，三者 restart count 均为 0；Nekro bridge
  入口文件哈希仍与节日锁一致。没有为本次状态对齐重新构建或部署 Renderer。

`deploy/festival-renderer.lock.json` 的原 images/base_images 是 9 月 9 日发布快照，新增复核
字段记录后续承接关系，不将旧 Core 镜像误称为当前生产。日期表已生效与当前正在使用某款
节日主题是两回事；隔离模拟成图与生产日常链路通过，不代表 9 月 25 日真实窗口群聊效果
已经验收。后续需观察日期切换、跨窗口缓存/投递和实际图片效果，不需重复实现这八款皮肤。

## 已启用日期

除元旦／跨年皮肤外，区间均为 **UTC+08:00 当日 00:00:00（含）至次日 00:00:00（不含）**。元旦已调整为 **2026-12-31 12:00 至 2027-01-01 12:00**；均恰好 86,400 秒，见 [跨年调整记录](NEW_YEAR_WINDOW_20260916.md)。
没有提前预热、假期延长、每年重复规则或定时任务。日期表结束后返回日常白底黑字。

| 公历日期 | 皮肤 | 背景 |
| --- | --- | --- |
| 2026-09-25 | 中秋：月亮、流云、桂枝 | 藕紫灰 `#E9E0EF` |
| 2026-10-01 | 国庆：双层红绸、金星、烟花 | 象牙白 `#FFFAF3` |
| 2026-10-18 | 重阳：秋阳、远山、菊花 | 淡杏白 `#FBF3E7` |
| 2026-12-22 | 冬至：饺子、梅枝 | 白 `#FFFFFF` |
| 2026-12-31 12:00 → 2027-01-01 12:00 | 元旦／跨年：零点钟面、烟花 | 午夜蓝 `#182D48` |
| 2027-02-05 | 除夕：灯笼、卧羊、梅枝 | 深枣红 `#702923` |
| 2027-02-06 | 初一：爆竹、迈步羊、梅枝 | 朱红 `#B33328` |
| 2027-02-20 | 元宵：灯笼、汤圆 | 绛红 `#842B3F` |

农历日期及冬至日期核对自香港天文台 [2026 公农历对照表](https://www.hko.gov.hk/en/gts/time/calendar/pdf/files/2026e.pdf)
和 [2027 公农历对照表](https://www.hko.gov.hk/en/gts/time/calendar/pdf/files/2027e.pdf)。除夕为正月初一前一天；本表不硬编码每年腊月三十。

## 实现与边界

- `superlily_contracts.festival_themes` 保存固定日期和配色。Core 的 `SUPERLILY_RENDER_FESTIVAL_ENABLED=true`
  启用这份表；不在窗口内时主题为 `default`，模型提交的 RenderDocument 合同不变。
- Core 在渲染快照中记录主题、版本、窗口结束时间；缓存不能跨窗口复用。日常产物也在下一个节日开始时到期。
  如果编译或发布过程中跨界，放弃过期尝试，最多自动重渲染一次。
- Core 通过认证的内部 HTTP header `X-Render-Theme` 传入主题；gateway/worker 仅允许固定主题 ID。
  Worker 按 AST 给标题着色，TeX 负责正文/数学公式和底色，不依据图片坐标识别标题。
- SVG 作者稿及预编译透明 PNG 随 worker 镜像发布，边饰没有文字、外链、顶底分隔线。
  生产合成使用 Pillow，不运行 SVG 脚本、不下载素材；PNG 按 manifest 校验。最终图片仍受 2048 像素和原有字节上限约束。
- 非节日直接返回原来的 TeX 渲染图，不添加边饰或留白。数学专用 `latex.render` 及外部图片透传不套用文档皮肤。
- 新投递意图检查产物到期时间和当前主题；Nekro 在文件准备完成、调用 `send_image` 前检查截止时间。
  若已跨界，先记录该意图为明确未发送，再续办一次。平台发送结果不明时不自动重发。
- 时间保证落在服务器选图/发起发送边界；QQ 网络到达时间不受本服务控制，历史消息图片保持原样。

## 发布与验证

本次没有数据库 migration。使用 `deploy/Dockerfile.festival-overlay` 在保留的生产镜像上只覆盖这次需要的模块，
未带入其他工作区改动。常规 `Dockerfile.latex-worker` 也已增加 renderer extra；正常构建会包含素材包。
确切基础镜像、新镜像、worker implementation hash、素材 manifest 和 bridge 源码哈希见
[`deploy/festival-renderer.lock.json`](../deploy/festival-renderer.lock.json)。

- SQLite 聚焦回归 **108 passed**：settings、worker/gateway、rendering、Markdown、兼容渲染、安全矩阵、bridge、节日规则。
- 临时 PostgreSQL 专项 **40 passed**：节日跨日缓存/投递与渲染契约。生产数据库未用于破坏性测试。
- 与生产相同 TeX Live、Poppler、只读文件系统、无网络、内存及进程限制的隔离 worker 中，日常及八款皮肤全部成图。
- 日常输出 SHA-256 与旧渲染器一致：`3fc83e3b41341d42ad2b6d0cd41a6c3d20650033980c6cf1d01b585eea416e26`。
- 深色底 PDF 的末行白像素舍入问题已覆盖回归测试，避免意外出现底部分隔线。
- 生产 Core → gateway → worker → artifact 下载及再次请求缓存命中通过，生产日常输出仍为上述哈希。
  冒烟使用合成会话 `onebot_v11-group_festival-preview`，没有创建投递意图，也没有发送 QQ 消息。
- Core 和 gateway healthy，worker ready，三个服务重启计数为 0，无新增 ERROR/Traceback。
  Nekro bridge 通过插件 API 热重载，Nekro 容器未重启，bridge 仍启用。
- 采集连续水位从 Lily `783937/783937`、Nekro `295212/295212` 推进至
  Lily `783964/783964`、Nekro `295301/295301`，没有序列缺口。这是短时发布验证，不是长期运行验收。

检查点 `/home/justin/backups/superlily/20260909-festival`（0700，含私密 `.env`，勿提交）保存
旧镜像身份、旧配置、旧 bridge、真实成图、验证结果及部署前后健康快照。

## 回滚

以下完整回滚命令记录的是 9 月 9 日发布时的基线。9 月 12 日 Core 已有后续消融改动，
不得直接用该历史旧镜像覆盖当前生产；优先仅关闭皮肤开关。完整镜像回退须先按最新部署
记录评估并保留后续改动，不能把本节历史命令当作当前一键回滚方案。

只关闭节日样式：将 `.env` 的 `SUPERLILY_RENDER_FESTIVAL_ENABLED` 改为 `false`，然后仅重建 Core：

```bash
docker compose --env-file /home/justin/superlily/.env -f /home/justin/superlily/deploy/compose.yml up -d --no-deps --no-build --timeout 60 lily-core
```

完整回滚：从检查点恢复旧 bridge 并热重载 `superlily_bridge`；从检查点 `.env` 只恢复
`SUPERLILY_RENDER_IMPLEMENTATION_HASH`，关闭节日开关，不覆盖检查点后其他配置。
将保留的旧镜像恢复到 Compose 默认标签，再仅重建这三个服务：

```bash
docker tag superlily/core:pre-festival-20260909 deploy-lily-core
docker tag superlily/renderer:pre-festival-20260909 deploy-document-renderer
docker tag superlily/worker:pre-festival-20260909 deploy-latex-worker
docker compose --env-file /home/justin/superlily/.env -f /home/justin/superlily/deploy/compose.yml up -d --no-deps --no-build --timeout 60 latex-worker document-renderer lily-core
```

旧镜像和停用 Provider 均保留；不要执行整套 Compose `up`。后续新增节日/素材时需更新日期表、素材 manifest
及主题版本绑定，重跑边界与真实成图验证；本版本不会推测未来节日或自动更换生肖。
