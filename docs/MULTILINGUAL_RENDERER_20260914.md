# 多文字衬线排版与单色 emoji

2026-09-14 已部署，服务 MANIFESTO 第 1 条：群聊图片中的西里尔字母、希腊文、阿拉伯文及 emoji 不再因默认 Latin Modern 缺字而静默消失。

## 字体和排版

- 中文等 CJK 文字继续使用 Noto Serif CJK；正文拉丁、希腊、西里尔字母使用 Noto Serif，代码中的对应文字使用 Noto Sans Mono。
- 阿拉伯文字使用 Noto Naskh Arabic，连续文字段保留连写，使用 bidi 设置 RTL；段内数字保持 LTR，标点和数字按字库覆盖选字体。
- 希伯来、亚美尼亚、格鲁吉亚、印度及东南亚等文字优先使用对应的 Noto Serif 字体；仅提供 Sans 的文字使用对应 Noto 补字。此处不声称覆盖全部 Unicode。
- 文字符号按固定字体 cmap 回退至 Noto Symbols / Music / Math；默认文本形式的箭头等不会强制变成 emoji。
- 单色 Noto Emoji 使用固定上游版本的可变字体生成 400 字重静态字体，适配 XeTeX；按 Unicode grapheme cluster 分段，保留肤色、旗帜、ZWJ 家庭和键帽组合。emoji 继承正文或节日标题颜色。
- 仅对非 CJK 字体段关闭 xeCJK 字符拦截，避免 emoji 的变体选择符被送回中文字库。普通文本仍先做 TeX 转义，字体名和命令来自固定集合。
- 文档设置 `tracinglostchars=3`：剩余未覆盖字符导致明确的编译失败，不返回漏字图片。诊断仍遵守原有有限错误合同，不暴露正文或完整编译日志。
- 原始数学公式输入保持 TeX 语义；本次自动选字库作用于文档正文、标题、表格、列表、引用及代码块，不将任意数学宏中的字符串改写为自然语言。
- 节日日期、配色、无文字装饰和 24 小时窗口保持原有规则。

## 构建与验证

标准 worker Dockerfile 安装 `fonts-noto-core=20201225-2`，renderer extra 固定 `regex==2026.9.10`。本次使用 `Dockerfile.multilingual-overlay` 在保留的当前 worker 镜像上发布，只更新 worker 模块与字体依赖。

字体源码、OFL 许可证、静态字体、coverage 和 manifest 位于 `superlily_latex_provider/fonts`。`scripts/build_renderer_fonts.py` 使用 `fonttools==4.61.1` 和指定 Debian Noto 包离线重建。镜像身份、模板 hash、Unicode 处理模块及字体 manifest 共同绑定缓存版本。

- 86 项聚焦回归通过：字体转换、转义安全、worker、文档/Markdown、节日、投递与安全边界。
- `scripts/verify_multilingual_renderer.py` 在同生产限制的只读、无网络 worker 中执行：日常与全部 8 款皮肤通过，示例单张约 2 秒。
- 实际 XeTeX 验证拉丁扩展、西里尔、希腊、阿拉伯、希伯来、日、韩、印地、泰及更多印度/东南亚文字；标题、加粗、引用、代码和表格通过。未知字符拒绝测试通过。
- Core → gateway → worker → artifact 下载通过；生产图与隔离测试 PNG SHA-256 相同，重复请求命中缓存。合成会话 `onebot_v11-group_font-preview` 未创建投递意图，未向 QQ 发消息。
- Core 与 gateway healthy，worker running，重启计数 0，检查窗口无新增 ERROR/Traceback。采集连续水位继续推进，无序列缺口。

具体身份和验证值见 [部署锁](../deploy/multilingual-renderer.lock.json)。Core 保留当前 `0f55e7d…` 镜像，仅因缓存身份环境变量更新而重建容器；gateway 与 Nekro 没有重建，没有数据库迁移。

## 回滚

检查点 `/home/justin/backups/superlily/20260914-fonts` 保存部署前镜像、私密环境配置、部署和验证记录。不要提交其中的 `.env.before`，不要整份覆盖后续环境修改。

1. 从本次部署锁的 `previous_worker_identity` 仅恢复 `.env` 中 `SUPERLILY_RENDER_IMPLEMENTATION_HASH`。
2. `docker tag superlily/worker:pre-fonts-20260914 deploy-latex-worker`。
3. 确认 `deploy-lily-core` 标签仍对应当前 Core（本次不需要回滚 Core 镜像），再执行：

```sh
docker compose --env-file /home/justin/superlily/.env -f /home/justin/superlily/deploy/compose.yml up -d --no-deps --no-build --timeout 60 latex-worker lily-core
```

回滚将恢复原来的缺字行为。不要以 9 月 9 日节日发布的历史 Core 镜像覆盖当前 Core。
