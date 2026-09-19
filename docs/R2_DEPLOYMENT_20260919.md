# R2 前两阶段上线记录

2026-09-19 12:55（Asia/Shanghai）：前两阶段已完成、已上线。不将未出现的低频场景
另挂“待验收”；未实施的后续功能仍单独列为未完成。

## 交付范围

- 每群一个长期共享目录，沿用原位置和文件；每任务独立临时区，同群执行轮次串行。
- JSON RPC 与短期执行凭证；跨会话目标拒绝，不再反序列化 pickle。
- 文件导出用安全打开和可信快照，拒绝穿越、符号链接、硬链接、特殊文件。
- 容量选项已生效：单群 256 MiB、单任务 128 MiB、全部沙盒目录 2 GiB、磁盘余量 2 GiB。
  超额停止执行，不删旧成果；单文件硬限制 64 MiB，总量是监测式软限制，存在轮询突发窗口。
- 生产保持 bridge 网络，非全面网络隔离。已启用插件的深层资源权限、收费预算及后续
  Core 投递扩展没有在本次全部实现。

## 版本与发布

- Runtime 远端源码：`db0c6d0595e8fb550a0a4cc2cf51e6771f06a6a5`。
  镜像构建标签为本地提交 `396e82a62d4ef10a9f580ff7ab799e34032c9e35`；GitHub API 发布后
  提交元数据不同，文件树均为 `5826121912490872d511c23a451a10d7ba29a086`，内容完全相同。
- 镜像：`superlily/nekro-agent:2.3.3-superlily.11-r2.1`。
- 镜像 ID：`sha256:df3a01581b7071939cea84e56f69fcc789314c58b964c7e08ed7ec16d98d3062`。
- 在现网 `.10-ablation.2` 镜像上仅覆盖 23 个相关文件，各文件 SHA256 已与源码核对，
  清单见 [Runtime 锁](../deploy/nekro-runtime.lock.yml)。保留已部署的可选插件延迟初始化。
- 仅对 Compose 服务 `nekro_agent` 执行 `up -d --no-deps --no-build --pull never`。
  Core、NapCat、document-renderer、latex-worker、PostgreSQL 的启动时间均未改变。

## 验证结果

1. Runtime `poe lint`、`poe typecheck` 通过；`R2_DOCKER_SMOKE=1 poe test` 为 90 passed。
2. 候选镜像自身 Python 3.11/依赖环境执行 `scripts/verify_r2_release.py`：离线 Unix RPC、
   生产兼容 bridge HTTP RPC 各 5 轮真实容器，验证依赖导入、重试、同群共享、跨群隔离、
   上传只读、稳定导出和跨会话拒绝。测试无模型请求、无平台发送、无生产数据库连接。
3. 最终镜像仅补充源码 revision 标签，23 个交付文件均与上述测试源码完全一致。
4. Runtime 启动于 `2026-09-19T04:55:46.44426993Z`，12:55:51 完成应用启动；
   12:56:15 QQ WebSocket 重连，健康检查 healthy，restart_count=0，无启动 ERROR/CRITICAL。
5. 12:56:45 生产采集水位：Nekro `404605/404605`，Lily `968591/968591`，均追平。
   两端事件时间持续推进；这证明重连与采集恢复，不宣称覆盖所有断线场景。
6. 配置前后语义比较：原有键值无变化，仅新增 5 个 R2 配置项。已启用插件仍为
   `KroMiose.basic`、`Superlily.core_bridge`。bridge 配置 SHA256 前后相同：
   `06e107be78980b563877064af6cad4b471fde1067cccd6ccb48cb7e27299db44`。
7. 没有主动发送测试 QQ 消息，没有开启 R3.1、媒体下载、Luna 或其他模型。

## 回滚

旧镜像保留：`superlily/nekro-agent:2.3.3-superlily.10-ablation.2`，
ID `sha256:cf6f66a458d7754cdaa50c74493c2e28cfd54529f17e878207a5b861f142920a`。
发布前配置与旧锁/override 备份在本机私有目录
`/home/justin/nekro/.r2-release-20260919-kobbDM/`；包含配置，禁止提交或公开。

若出现本次引入的启动失败、RPC/沙盒持续失败或采集不能恢复，只回滚 Runtime：

```sh
docker compose --project-name nekro --env-file /home/justin/nekro/.env \
  -f /home/justin/nekro/docker-compose.yml \
  -f /home/justin/superlily/deploy/nekro-compose.override.yml \
  -f /home/justin/superlily/deploy/nekro-r2.rollback.yml \
  up -d --no-deps --no-build --pull never nekro_agent
```

回滚时保留群文件、上传、数据库和 `.r2-state`，不自动清空，不恢复整份旧配置覆盖后续修改。
回滚后重新检查健康、QQ 连接、采集水位，并把主 override 和锁文件同步为真实在线版本。
未执行过上述回滚，不把可用命令冒称实际回滚演练。
