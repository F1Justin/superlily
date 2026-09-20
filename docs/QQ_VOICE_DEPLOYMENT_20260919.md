# QQ 语音输入试用部署（2026-09-19）

当前状态：`.12-qq-voice.5` 已全量启用，历史音频与文字共用窗口，累计上限16分钟，MiMo 思考关闭。源码已发布为 Runtime 提交 `3254ed24b556108f1030341bd4216e6ce426c918`。下方按时间保留部署验证记录，早期范围与镜像不是当前配置。

已部署普通回复入口，仅启用群 `861651713` 和 `708309706`。转写包含人格名或配置词 `莉莉 / 丽丽 / lily` 时，沿现有 Runtime 回复链路处理；没有称呼的语音不通过随机/内容规则启动回复。没有语音输出或人格提示词变更。

生产 NapCat 4.8.110 的安装包没有 `fetch_ptt_text`，因此选择 `get_record(out_format=wav)` 加 `mimo-v2.5-asr`。API Key 从现有护理虚拟教师服务复用，保存在不入 Git、0600 权限的 `/home/justin/nekro/configs/qq-voice.env`，由 Compose 注入。群内候选语音需转写后才能判断是否叫到莉莉。

镜像：`superlily/nekro-agent:2.3.3-superlily.12-qq-voice.1`，基于线上 `.11-r2.1`，只覆盖部署锁列出的六个 Python 文件，部署前已逐一核对其 HEAD 基线与生产内容相同。源码变更尚未提交，文件哈希见 `deploy/nekro-runtime.lock.yml`。

验证：46 项语音入口及 Runtime 回归测试通过；全局和显式覆盖适配器文件的 `poe lint` 通过；候选镜像配置/导入探针通过；上线容器 healthy，QQ WebSocket 已重连；容器内确认配置仅两个群且 MiMo 密钥存在。数据库确认两个群均启用、非旁观。没有代用户向 QQ 发测试消息；真实 QQ 录音转写和回复仍待用户发语音验收。

回退：恢复 `/home/justin/superlily/run/qq-voice-20260919/` 中备份的 OneBot `config.yaml`、Compose overlay 和部署锁，然后对 `nekro_agent` 执行相同 Compose 文件的 `up -d --no-deps --no-build`。只关闭 `VOICE_TRANSCRIPTION_ENABLED` 可停用入口；为确保进程状态一致可重建 Runtime。旧镜像保留，NapCat 和 Core 未重启。

## QQ 原生转写（.2 部署记录）

NapCat 已升级为 4.18.28，镜像按 digest 固定，配套 QQ 3.2.30-50969。登录数据在停机状态下通过 reflink 备份到 `/home/justin/napcat-backups/20260919-native-asr/napcat_data`；自动登录与反向 WebSocket 恢复。

两个群现选择 `napcat`，不再注入 MiMo 密钥。Runtime 镜像更新为 `.12-qq-voice.2`：原生接口可能在 QQ 转写结果写回前返回“获取语音转文字结果失败”，仅针对该错误和空文本每秒重试，最多 6 次，仍受总计 30 秒期限约束。47 项测试和 lint 通过。

经鉴权 WebUI Debug API 实测：消息 1651640711 返回“你晚上吃什么？”；1743465235 返回“丽丽，晚上吃什么？”，后者匹配配置的额外唤醒词。没有补发旧消息回复。完整新消息回复仍由用户发新语音验收。调试适配器调用完成后关闭。

NapCat 回退必须先停容器，保留升级后的数据并恢复上述旧版备份，再使用旧镜像；不能假设新 QQ 的数据库可直接降级。Runtime 切换前的配置与锁分别在 `run/qq-voice-20260919/config-before-native.yaml` 和 `lock-before-native.yml`，旧 `.12-qq-voice.1` 镜像也保留。
