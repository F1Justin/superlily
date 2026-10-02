# Nekro API 自动切换出口

Nekro 模型组通过 Docker 网关 172.20.0.1:7898 进入专用节点组。每次请求先向 7900 节点管理服务获取固定节点入口，18000 起的端口按订阅生成，避免并发切换改变当前请求的出口。

节点来源为当前订阅的日本、新加坡节点，AnyTLS 优先。每 30 秒检查 OpenRouter /models，要求 HTTP 200，超时 5 秒。失败节点冷却 10 分钟，状态保存在 /home/justin/nekro-egress-state.json。订阅重载后服务从 API 重新读取节点。

连接错误、首包超时和明确地区限制触发轮换，原模型与消息保持一致，遍历全部候选节点后结束；备用模型已停用。凭证和配额等错误停止轮换。收到部分流式内容后保留中断处理。控制端鉴权令牌在 control.token 和 Nekro .env 中，令牌文件排除于 Git。

## 运维

systemctl status nekro-egress.service 查看节点管理服务。journalctl -u nekro-egress.service 查看切换记录。

Clash 配置源为 profiles/Script.js，重激活后验证 /rules 和 /connections。Docker 构建源为此目录 Dockerfile，正式 Compose 使用 /home/justin/nekro/docker-compose.yml 与 deploy/nekro-compose.override.yml。

## 回滚

恢复 /home/justin/nekro/backups/egress-20261002-212047 内的 nekro-agent.yaml、env 和 nekro-compose.override.yml 至原路径，恢复 Clash 的 nekro-egress-backup-20261002-211840/Script.js 并重启 Clash Verge。停止并禁用 nekro-egress.service，再以原 Compose 重建 nekro_agent。

## 验证

四项离线测试覆盖第 5 节点成功、全节点耗尽、部分流式响应和凭证错误。运行中的管理服务已验证 5 次节点尝试以及并发切换合并。容器内 Gemini 实际请求 HTTP 200，Clash 连接链为固定节点入口。
