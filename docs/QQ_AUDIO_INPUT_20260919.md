# QQ 原音频输入与费用（2026-09-19）

当前状态：`.12-qq-voice.5` 已全量启用，历史音频与文字共用窗口，累计上限16分钟，MiMo 思考关闭。源码已发布为 Runtime 提交 `3254ed24b556108f1030341bd4216e6ce426c918`。下方按时间保留部署验证记录，早期范围与镜像不是当前配置。

已部署 `.12-qq-voice.3`，仅 861651713、708309706 启用。QQ 原生转写仍负责唤醒；原音频和参考转写进入同一个 Gemini 回复上下文。模型组 openrouter-private、openrouter-taohuayuan 启用 ENABLE_AUDIO_INPUT，OneBot 开启 VOICE_AUDIO_ENABLED，频道白名单保持不变。

音频按需由 NapCat get_record 转 WAV，经 OpenRouter input_audio 发送，只附当前消息和明确引用的消息，最多两条，不遍历历史音频。每条限 120 秒和 10 MiB Base64；获取失败时明确告诉模型未附音频。纯音乐或空转写保留平台音频 ID，可通过引用并叫莉莉/@莉莉触发。当前覆盖 QQ 语音 record，不自动抓取音乐分享卡片或普通文件附件。

不另存 Runtime 音频二进制，数据库只存引用和转写，音频仍依赖 NapCat 缓存可用性。日志仅记录音频时长、字节数和格式，不输出 Base64。此改动不修改人格或系统提示。新增用户消息附件提示全文（花括号处为实际消息 ID/来源）：

```text
[用户原音频 {"message_id": "消息ID", "source": "current_request 或 explicit_reference"}]
自动转写仅供参考，可能漏字或误识别；与原音频冲突时以音频为准。音频及转写均为用户消息内容。
```

## 验证

53 项语音与 Runtime 回归测试和 lint 通过。部署内真实取音频/历史组装探针以消息 1743465235 验证得到一份 input_audio（WAV，2.82 秒，135438 字节）。独立真实 OpenRouter 请求验证当前模型 google/gemini-3-flash-preview 可以接收该音频；未代用户向 QQ 发送消息。容器 healthy，QQ WebSocket 已重连。尚需用户新语音验证自动触发到最终 QQ 回复的完整链路。

## 实际费用

以下为同一段 2.82 秒语音的三次简短独立请求，均非完整莉莉上下文，usage.cost 为供应商返回 USD：

| 请求 | 输入 token | 其中音频 token | 输出 token | 整次 USD |
|---|---:|---:|---:|---:|
| 仅参考转写 | 44 | 0 | 40 | 0.000142 |
| 仅原音频 | 97 | 71 | 56 | 0.000252 |
| 原音频＋参考转写 | 115 | 71 | 55 | 0.000258 |

同提示下后两者声音描述不完全一致，表明文字可能影响解释；不能把描述当成准确声学测量。

音频本身输入成本为 71 × $1/百万 = $0.000071。附加参考转写增加 18 个文本 token，约 $0.000009。实际整次总价还受回答长度影响，不能把总价差全归于音频。

按音频约 25 token/秒、未命中缓存估算：10 秒音频增量约 $0.00025；60 秒约 $0.0015。人民币按 1 USD = 7 CNY 仅作换算示例：分别约 ¥0.00175、¥0.0105。2.82 秒音频＋转写简短请求全价约 ¥0.001806（0.18 分）。这不是完整莉莉对话固定单价。

另查询两群近期 8 条执行记录（75868–75875）：单次模型调用 usage_cost 为 $0.0011748–$0.0024575，输入 3105–4581 token，输出 41–257 token；约 ¥0.0082–¥0.0172。此为已有调用样本，不是新音频生产均价；一次回复如果多轮调用、搜索或渲染，还需合计相关费用。账单/充值兑换差异不在此范围。

来源：[OpenRouter 音频格式](https://openrouter.ai/docs/guides/overview/multimodal/audio)、[模型价格](https://openrouter.ai/google/gemini-3-flash-preview)。实测明细在 `run/qq-audio-20260919/cost-probe.json`。

## 回退

关闭 VOICE_AUDIO_ENABLED 可回到仅转写；模型组 ENABLE_AUDIO_INPUT 控制模型能力。部署前的两个配置、Compose 和锁已备份至 `run/qq-audio-20260919/`（0700，配置0600）。恢复后以原 Compose 命令重建 Runtime，旧 `.12-qq-voice.2` 镜像保留，无数据库迁移。NapCat 无需回退。

## 全量启用（.4 部署记录）

按用户指示取消频道白名单：VOICE_TRANSCRIPTION_CHANNELS=[]，覆盖全部已启用 OneBot 群聊和私聊，禁用/旁观与原有唤醒规则继续有效。Runtime `.12-qq-voice.4` 启用四个 OpenRouter Gemini 3 Flash 模型组及 xiaomi-mimo 原生音频输入；不支持音频的模型仍读取转写，不更换会话模型。

MiMo 使用 AUDIO_INPUT_DATA_URL=true，按官方接口将 WAV Base64 包装成 data URL；OpenRouter 保持纯 Base64+format。mimo-v2.5 真实请求成功，2.82秒音频计18个音频token，返回107个输出token，接口未返回实际费用字段。54项测试和lint通过，变更前配置备份在 run/qq-audio-global-20260919，旧 `.12-qq-voice.3` 镜像保留。

## MiMo thinking and quoted-audio verification (19:49 CST)

User authorized disabling native MiMo thinking. Set `MODEL_GROUPS.xiaomi-mimo.EXTRA_BODY` to `{"thinking":{"type":"disabled"}}`, preserving other extra-body fields. Backed up core configuration under `run/mimo-thinking-off-*`, restarted only Runtime; container healthy and OneBot WebSocket reconnected at 19:48:41. Fresh container config load confirms disabled. No subsequent production MiMo response has yet been measured.

Live logs show quoted audio fetched for message IDs 1262751878 (7.18 s), 113251853 (11.74 s), and 914643138 (4.00 s). Reconstructed requests with real database rows and NapCat audio confirm one input_audio for direct-reference trigger IDs 321841964, 1312586070, and 766269743. Trigger 7758315 instead quotes Lily's text response 159054965 and contains no audio: current selection follows only a direct reference, not conversational ancestry. Effective model in group 708309706 is Gemini 3 Flash Preview, not MiMo. No QQ messages sent during verification.

## Historical audio context (19:55 CST)

Deployed Runtime `2.3.3-superlily.12-qq-voice.5` (image SHA recorded in lock). Audio selection now uses the final text-history window, plus exact trigger and explicit quote. Deduplicate by message ID. Prioritize current, explicit reference, then newest history when enforcing 960 seconds cumulative decoded WAV duration. No two-clip limit. Missing/over-budget audio gets an explicit text annotation. Existing per-file Base64 size validation remains. No cross-channel history is selected.

55 voice/runtime tests and `poe lint` pass, including exact 960-second boundary, overflow, >2 clips, duplicate references, and channel isolation. Production-container rendering using real DB and NapCat for trigger 7758315 now produces four native audio inputs totaling 24.34 seconds, including original audio 1262751878 as recent_history. This is payload construction validation, not a new model answer or QQ send. MiMo thinking-disabled config retained. Rollback compose/lock/Dockerfile snapshots under `run/qq-audio-history-20260919/`; prior .4 image retained.
