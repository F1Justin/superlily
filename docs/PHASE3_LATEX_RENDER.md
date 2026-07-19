# Phase 3：`latex.render` 实施与上线验收

本文把 ADR 0014 落成可执行检查表。目标是完成第三阶段首个真实 artifact 工具，不是
提前建设通用 Renderer，也不改变 `/tex` 或开放模型工具调用。

## 本包交付物

- `latex.render@1.0.0` 与 `provider-latex-primary` Git-reviewed authority；
- 无网络、无凭据的 XeLaTeX/Poppler worker 和独立 Provider；
- 有界 Unix framed 协议、双重 PNG 检查和安全错误分类；
- reserve/upload/finalize/complete 的真实 Provider 调用；
- worker deployment identity 与 Provider implementation hash；
- 默认不启动、没有 token/identity/artifact store 就 fail closed 的 Compose profile；
- 双数据库、镜像、真实渲染、生产单次 canary 与回滚证据。

发布前全量结果为 SQLite 463 项通过、4 项跳过，隔离 PostgreSQL 17 为 467 项全部
通过。定向 LaTeX/SDK/artifact 合同为 63 项通过；最终 worker/Provider 镜像已构建，
固定公式容器探针输出 34,883 字节、2048×499 的 PNG。

## 已冻结的部署 identity 与 authority

| 项目 | 值 |
|---|---|
| tool | `latex.render@1.0.0` |
| descriptor SHA-256 | `adad493e24444f8a09215180dc90839102646fe069e2d767f9d1cab9ef826b36` |
| provider | `provider-latex-primary` |
| caller | `command`, `admin_api` |
| natural language / 平台发送 | 关闭 / 无 |
| artifact | 1 张 `image/png`，最多 4 MiB、2048×2048 |
| timeout / concurrency | 30 秒 / 1 |
| worker image | `sha256:845faf7b8caecf17540c1933a9a764b5c13865b5f57597a741aa27b3d75b69bc` |
| Provider image | `sha256:cc2ec3b8d73c64f17f12d400dedb903422fb2e7df003757952e3cbddbedb72fc` |
| sandbox profile | `0bdca3208a8b183937ffb4f6f4cda908a731ed709122440a76c781b953c2b492` |
| worker identity | `5fec6df87bbfda7666c2e47018763d080e2b99049cea57d24e3ad4bc160e848a` |
| implementation hash | `26a473b53cb3291c91fa049ed8fc15316d8c44e6d91a9bfa790f6a314d1357c3` |

implementation hash 只要 `main.py`、`runtime.py` 或 worker identity 改变就必须重算。
worker identity 只要镜像、worker 源码、模板、引擎版本或隔离配置改变也必须重算。

## 发布前检查

1. 保持 Core 为 `ledger_only`、无 active plan/attempt，确认 `0016` head/no drift。
2. 保留 `/home/justin/lily/plugins/tex` 原文件和 dirty worktree，不修改旧 `/tex`。
3. 验证 descriptor、Provider authority、Compose config 和 sandbox profile；运行定向、
   SQLite 全量、PostgreSQL 17 全量与镜像 `pip check`。
4. 在宿主和最终容器中分别渲染固定 `x^2+y^2=z^2`，验证 PNG hash/尺寸/字节；恶意
   `\input`、`\write18`、编译失败和原始错误不能读取宿主秘密或泄露公式。
5. 核对 worker `network=none`、只读 rootfs、cap drop、NoNewPrivs、1 GiB、1 CPU、
   128 PIDs、单并发、tmpfs 与 socket 权限；Provider 没有 bot/admin/ingest token。
6. review 取消后残留计算、artifact 半完成、上传响应不明确、lease 续期、输出 schema、
   artifact 精确引用和 reaper；不得把 best-effort 写成 hard。

## 生产上线顺序

1. 在启用 artifact 前制作 PostgreSQL 备份并在独立 PostgreSQL 17 卷实际恢复。
2. 为 artifact store 生成独立 pepper，启用现有私有持久卷；不把 pepper 写入 Git、
   日志或文档。重建 Core 后验证 `artifact_enabled=true`、目录 0700 和旧账本不变。
3. 为 LaTeX Provider 生成独立 token，原子加入 Core Provider token map；不得复用其他
   credential。从完整 Git commit 注册 Provider、导入 descriptor 为 reviewed。
4. 启动 `latex-worker` 与 `latex-provider`，核对精确 identity/inventory/heartbeat 和
   hard budget。reviewed 状态与 `ledger_only` 下应保持零 lease。
5. 通过 M1 reviewer preview/CAS 激活精确 descriptor，不直接修改 SQL。
6. 提交并导入一份最多一次的 Git-bound plan，精确绑定 descriptor hash、Provider、
   `admin_api`、`qq:group:1080353942`、资源版本和短窗口，再由 operator 激活。
7. canary 只提交固定 `x^2+y^2=z^2`。检查一个 invocation、一个 attempt/fence、
   heartbeat、reserve/upload/finalize/reference、精确 usage 和一个 PNG；不得产生平台
   response 或调用旧 `/tex` 发送。
8. 立即暂停 plan，确认第二次提案不能新增 attempt；与旧 `tex2pic` 在不发 QQ 的情况
   下串行比较成功性、MIME、尺寸和可读内容，不要求不同渲染参数产生相同二进制 hash。
9. 跨至少一个完整 inventory 周期观察 Core/Provider/worker、artifact/reaper、C0-D 和
   旧 bot；最后恢复 `ledger_only` 并关闭临时控制面。

## 生产签署（2026-07-19）

上述顺序已全部执行并通过：

- 启用 artifact 前的 PostgreSQL 自定义格式备份为 152,117,402 字节，SHA-256 为
  `881cf9aa7a634768ac42056744fa9b265e675d54edc58431b1ba989b7eeea8b2`；它已在
  独立 PostgreSQL 17 磁盘卷中完整恢复到 `0016_confirm_artifacts`，恢复出的
  source event=388,819、invocation=15、attempt=11、artifact=0、plan=14；
- Core artifact store 已启用，根目录为 0700、对象为 0600，均属 uid 65532。Core
  镜像为
  `sha256:0450f2d9742bcbc69d73e354adf5cd4ebb60e4c4a001a06b225fb0842b89ee86`；
- Provider 为 `active/rv1`，descriptor 经 reviewer 从 `reviewed/rv1` 激活为
  `active/rv2`。最终 inventory hash 为
  `b4ad3081c6f2cd3bb4b4006125eb0088b455d29f7f2866433496ca455f4f2f4b`，五项
  required budget 均为 hard；
- Git-bound 计划 `latex-artifact-success-20260719@1.0.0` 的 SHA-256 为
  `d09f39c5fad1a45953bee32e0e2cbccad113967394e318ff6040460f2ecf4694`，只允许
  `admin_api + qq:group:1080353942 + latex.render@1.0.0 +
  provider-latex-primary`，最多一次；
- 唯一 invocation `a5138434-2b51-4b3a-98bd-810bfb51afc5` 只产生 attempt
  `65a0cd4e-b8f9-4c38-9d7e-dcebd16fc8d1`、attempt number=1、fence=1，并以
  `proposed -> queued -> leased -> running -> succeeded` 完成；wall=1,245 ms、
  input=23 bytes、output=235 bytes、artifact=34,883 bytes；
- artifact `982810cd-ece3-41e0-af04-e9575e5a847f` 的事件严格为
  `reserve -> upload_start -> upload_complete -> finalize -> reference`，最终为
  finalized/referenced、未删除。数据库、私有对象文件和 Provider 三方一致确认它是
  34,883 字节、2048×499、SHA-256
  `4ad21ef65944d745782a87c7970bd56d9ce846ebda45be1f95d457d5bd1fdfce` 的 PNG；
- canary 没有任何关联的 `responses` 行。旧 `tex2pic` 在不经过 QQ 发送的串行对比中
  同样成功生成 PNG（12,004 字节、849×207），`/home/justin/lily` 原文件与既有
  dirty worktree 均未改；
- 计划随即暂停为 `paused/rv3` 且计数 1/1。Core 恢复 `ledger_only`，active
  plan/attempt 均为 0；临时控制面配置清空、登录返回 503，临时明文凭据已销毁；
- 临时控制面关闭后的 03:44:57–03:54:28 UTC 内 20 次 heartbeat 全部 healthy、
  只引用同一 inventory hash，03:44:57 与 03:49:58 两份 inventory 一致；该窗口内
  Provider/worker 零新增日志，Core/Provider/worker/PostgreSQL 均零重启、零 OOM；
- 最终 SQLite 为 463 项通过、4 项跳过，隔离 PostgreSQL 17 为 467 项通过；Core、
  Provider、worker 的 `pip check` 均通过，`0016` 为 head 且无 schema drift。

因此本包退出门全部通过，`latex.render@1.0.0` 完成生产迁移签署。已引用 artifact
按 30 天保留策略处理，不为回滚删除账本或对象。

## 失败与回滚

- 最小回滚是暂停 plan；其次 suspension/quarantine；再其次恢复 `ledger_only` 并停止
  LaTeX Provider。worker 可保留无 authority 空转，也可停止。
- artifact store 一旦产生已引用对象，不为回滚删除账本或字节；按既有保留期处理。
- 上传/终结/完成不明确时不自动重试，不直接 SQL 修成功；让 lease/reaper 收敛并保留
  不确定性证据。
- 旧 `/tex` 始终可用，本包不切命令、不发群消息、不开放自然语言 caller。

## 本包退出门

- 双数据库全量、镜像、真实 worker 与安全探针通过；
- 生产 artifact store、Provider identity 和 hard budget 与审阅值一致；
- 精确一次 canary 得到 finalized、referenced 的 PNG，账本和零平台发送证据完整；
- plan 暂停后零新 lease，旧 `/tex` 无回归，稳定窗口无异常；
- 代码、中文 ADR、部署/验收记录来自可追溯 Git commit。

这些门通过后，只签署 Phase 3 的受控工具协议已具备三个代表性工具。命令统一适配、
通用 Renderer、模型自主选工具和平台发送分别留在 Phase 3d、4、5，不被本包暗中开放。
