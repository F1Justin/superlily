# DeepSeek 搜索：第二桃花源试点

2026-10-20（UTC+8），用户要求实现 DS 搜索与整理、由莉莉回复，并在桃花源试点。
22:04 只更新 Nekro Runtime；NapCat、PostgreSQL 和 Lily Core 保持运行。

## 最终行为

- 唯一允许会话：第二桃花源 `onebot_v11-group_861651713`。
- 莉莉通过既有原生 Agent 循环自行决定调用 `deepseek_search(query)`；没有 Runtime 内容分类器。
- 后端明确调用一次 `POST https://api.deepseek.com/v1/tools/search`，最多五条结果，
  `need_content=false`，不调用 Fetch，不自动重试或升级搜索。
- 只把独立查询、当前日期与每条最多 1200 字符的资料交给 `deepseek-flash`。
  整理关闭 thinking、最多 800 output tokens；不发送人格或群聊历史给 DS。
  按用户指定使用固定提示词：`根据搜索结果回答search query的内容，100字以内回复`。
  整理结果以纯文本直接交给莉莉；来源 URL 留在管理观测记录中。
- 验证结束状态与非空内容；截断或服务失败时返回未核实。
  搜索和整理总超时 40 秒。
- 每任务最多搜索一次，每日最多 100 次尝试（UTC+8 日期）；启动即预留额度，失败不自动重试。
  任务去重为进程内状态，重启会清空；每日额度来自持久使用记录，重启不重置。
- 搜索摘要只留在当前循环的临时上下文，不生成系统聊天记录或 Core 世界事实。
  执行记录、诊断文件只保留费用、token、耗时、来源 URL 等观测信息。最终发送的回复
  仍遵循普通聊天档案语义。已有管理员开启的 prompt/debug 日志属于另外的诊断存储；
  当前生产 `SAVE_PROMPTS_LOG=false`。
- 调用时再次检查会话范围；其他群与私聊既无方法签名，也无法通过 RPC 绕过范围。
  旧 `ENABLE_OPENROUTER_WEB_SEARCH` 开关保持关闭。

## 网页观测

Nekro WebUI → **沙盒日志** → 搜索 `deepseek_search` 或桃花源的 chat key → 点击记录展开。
“执行代码”显示查询；“执行结果”显示 **DeepSeek 搜索与整理**，含状态、结果数、搜索与
总耗时、DS token、公开价费用估算、来源 URL、任务关联标识和失败类型。
该页既有模型性能信息仍表示莉莉模型的调用，不把 DS 估算冒充供应商账单或合入既有成本字段。

隔离测试记录 84887、84888、84890、84891、84892（搜索），84893（续答预览）
均标记为 `DS搜索隔离测试`。这些记录不表示已发 QQ；试点的正常消息仍照常发送。
浏览器实际展开 84888 验证展示成功，未修改前端。

## 验证与费用

首次部署 Runtime `poe lint`、插件显式 lint、`poe typecheck` 通过；完整测试 **162 passed, 1 skipped**。
测试覆盖会话权限、任务及日额度、零结果不调用整理、材料截断、引用越界、整理截断、
费用计算、临时结果不生成系统历史及不泄漏到执行/诊断正文。
当前短提示词版本已移除 JSON 格式及引用编号校验，lint、typecheck 与调整后的 10 项搜索测试通过。

真实搜索与整理对 API 价格、Python 发布、东方锦上京发售及不存在的型号查询进行了小样本检查。
无法核实的型号查询没有被当成已发布的事实。小样本不替代全量准确率评测。

以下是首版结构化整理格式的费用与回放记录，不代表当前短提示词的费用。
使用当时人格、模型组 `openrouter-taohuayuan` / `google/gemini-3-flash-preview`，
追加合成请求到生产当前最近 16 条历史（含一张图片）的隔离回放中，两条明确搜索请求
调用了研究工具，道谢请求没有搜索。随后另用原生 `run_agent` 循环验证，
仅在最终投递处截留预览，真实沙盒 RPC 和 DS 调用均执行：

| 原生循环环节 | 费用 / 计量 |
| --- | --- |
| 莉莉发起搜索 | OpenRouter usage `$0.00181375`，4742 input / 16 output，1345 cached |
| DS 搜索 | 355 ms；按公开价估算 `$0.00075` |
| DS 整理 | 1883 input / 256 output；输入缓存命中已纳入估算 |
| 搜索与整理合计 | 2106 ms；估算 `$0.000941442–$0.001132884` |
| 莉莉续答 | OpenRouter usage `$0.00295625`，6829 input / 49 output，1345 cached |
| 整条合计 | `$0.005711442–$0.005902884`，两轮莉莉生成约 27 秒，另有执行开销 |

DS 价格取自 2026-10-20 [官方美元价格表](https://api-docs.deepseek.com/quick_start/pricing/)，模型费用列出非高峰至高峰范围；搜索独立 API
按每千次成功请求 `$0.75` 估算，不假定开发工具的限时免费也适用于本插件。
DS 没有返回实际美元扣费，所以其数字是公开价估算。失败时已观测的 usage 仍记录，
未收到 usage 的可能费用保持未知。两次莉莉 usage 为供应商返回值。

当前原生 AGENT 续答仍会重新带入历史，并在迭代反馈中重复包含结果；因此完整费用明显高于
单独 DS 环节。不同日期/上下文的小样本不能证明相对旧搜索路径的稳定降幅或生产均价。
目前是可用性与费用观测试点，不是全量质量验收通过。

私人证据位于 `run/deepseek-search-20261020/`（不提交群聊或密钥）：
`native-results.json`、`native-0-messages.json`、`native-1-messages.json`、
`full-system-prompt.txt`、`manifest.json`、使用记录与浏览器截图。
其中 native messages 保存完整 system/persona、动态历史与本次请求；人设未修改。
新增 DS 固定提示词为 Runtime `nekro_agent/services/agent/deepseek_search.py` 的 `DIGEST_PROMPT`。

## 部署、恢复与回滚

- Runtime commit：`c40b75835c53da32e6c3626b5ea42befbfd85476`。
- 镜像：`superlily/nekro-agent:2.3.3-superlily.16-deepseek-search.2`。
- Image ID：`sha256:fd30d9278183ceccacc2355a4781a86a773cf8d01fc39fcd6dc615c8f865735a`。
- Compose 与来源锁见 `deploy/nekro-compose.override.yml`、`deploy/nekro-runtime.lock.yml`。
- 生产 `/api/health` 返回 `{"ok":true}`，容器 healthy；22:04:37 OneBot 重连，后续普通
  消息持续入库。84894 是部署后的普通群回复执行记录。部署后未主动发送测试群消息。
- 插件 `Superlily.deepseek_search` 已成功加载；生产文件与代码读取验证桃花源可用，
  其他群及私聊不可用，密钥文件可读。未宣称已经观察到生产群内真实搜索调用。
- 密钥：`/home/justin/nekro/run/deepseek-search.key`，`0600`；不进入镜像、沙盒或送模输入。
- 插件配置及只含观测信息的账本：
  `/home/justin/nekro/plugin_data/Superlily.deepseek_search/`。

可在网页插件管理中关闭 **DeepSeek 搜索**，停止提供新调用；RPC 同样会复查插件状态。
完整回滚备份是 `/home/justin/nekro/backups/deepseek-search-20261020/`，保留原配置、
Compose override 和运行锁。恢复这三份文件到原路径后，只对 `nekro_agent` 运行既有
Compose 的 `up -d --no-deps --no-build --pull never`。旧镜像
`superlily/nekro-agent:2.3.3-superlily.15-attachment-identity.1` 仍在本地，无数据库迁移。
回滚不需要删除真实聊天或这次隔离测试的执行记录。
