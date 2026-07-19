# Phase 3：`0016` 确认与 Artifact 实施包

状态：实现、双数据库回归和默认关闭的生产迁移签署均已完成。

## 目标与非目标

本包实现两条共同底座：精确请求确认，以及 Provider 产物进入 Core 内容寻址存储。
它不开放自然语言 caller、`enforce`、平台发送、写工具或 Renderer，也不把图片字节
存入 PostgreSQL。具体 authority 决定见 ADR 0012。

## 实施顺序

1. 扩展共享合同：confirmation request/receipt、artifact policy、reserve/finalize、
   finalized reference；旧 `status.inspect` 合同与 hash 必须保持兼容。
2. 增加 `0016_confirm_artifacts`：confirmation/artifact 当前行与只追加
   event、资源版本、唯一约束和 SQLite/PostgreSQL guard。
3. 先接 confirmation：创建 challenge 不消费 rollout；批准事务重新验证 authority、
   原子消费并排队；拒绝/过期终止且无 lease。
4. 再接本地 artifact store：默认关闭；reserve、流式 upload、PNG inspect、原子
   finalize、complete 引用和幂等 cleanup 分层实现。
5. 为 Provider SDK 增加单次 reserve/upload/finalize 调用，不做盲重试，不在异常中
   输出 bearer、attempt secret、upload secret、路径或原始 body。
6. 完成双数据库、真实磁盘、并发和故障注入；默认关闭部署到生产并做备份恢复，保持
   `status.inspect`、C0-D 和旧命令不变。

## 已实现的协议

确认线已经落成 `pending -> consumed/rejected/expired` 的资源版本状态机。需要确认的
调用先进入 `awaiting_confirmation`，不提前消费 rollout 次数；原 caller 提交与原
请求、输入、principal、policy 完全相同的 hash 后，Core 才在同一事务中重验
descriptor、Provider、inventory、plan、计数、时间、限速和停止开关，原子消费一次并
重置执行 deadline。拒绝、取消和 reaper 都会同时关闭 challenge 与 invocation。
`two_person` 只有账本形状，没有可冒充的弱身份实现，因此继续明确拒绝。

Artifact 线已经提供三条 Provider API：

- `POST /v1/tool-executions/{invocation_id}/artifacts/reserve`；
- `PUT /v1/tool-artifacts/{artifact_id}/content`；
- `POST /v1/tool-executions/{invocation_id}/artifacts/finalize`。

reserve 绑定当前 running attempt、Provider、fence、attempt secret、幂等键、精确
descriptor 与会话分类；一次性 upload secret 由 Core pepper 派生，数据库只存 hash。
upload 直接流入 0700 quarantine，文件为 0600，逐块限制实际字节并检查 PNG framing、
CRC、IHDR、尺寸和声明 hash；调用方路径、symlink、URL、multipart 和未知 MIME 都不
构成 authority。finalize 在 digest 级跨进程文件锁下先发布
`objects/sha256/<prefix>/<digest>`，再提交数据库；complete 只有在全部 reference 与
当前 attempt/fence、Core 观察到的 MIME/hash/字节/尺寸完全一致，且
`usage.artifact_bytes` 等于引用总量时才会成功并标记 referenced。

reaper 独立于执行模式运行：它回收过期 reserve/upload、部分 quarantine、失败 attempt
留下的未引用对象、保留期届满的已引用对象和无数据库可见行的 orphan。内容寻址对象的
finalize 与删除共用 digest lock，数据库失败最多留下不可见 orphan，不会留下
“数据库已成功但文件缺失”的发布结果。元数据与只追加 provenance 不随字节删除。

## 审查与测试结果

- SQLite 全量：439 项通过、4 项 PostgreSQL 专用场景跳过；
- PostgreSQL 17 全量：443 项通过；
- 覆盖确认幂等/CAS/过期/plan 重验、artifact reserve 重放、错误与复用 secret、旧
  fence、错误 MIME/CRC/hash/尺寸/字节、上传中断、finalize 断点、引用与 usage 核对、
  retention/orphan 清理、路径与 symlink、防泄漏和字段级数据库篡改；
- review 期间修正了四个不会留到生产的问题：Registry 不可用但 reason 为空、reserve
  evidence 曾包含 lease secret、reference/cleanup 误刷新 finalized 时间，以及事件行
  存在时仍可能夹带修改其他状态字段。

## 生产签署

2026-07-19 09:36–09:45 CST，部署提交 `cd41026520b4ab88ab7c21bd13b0abd7cae2defd`
前先创建 0600 PostgreSQL custom-format 备份。备份大小为 151,402,854 字节，
SHA-256 为 `0ceaa7f4f9b7ca2e4538b9ec9e4d981d2f4a8223e6bcbc9faa8d8ca53bec0962`；
它在无端口暴露、独立磁盘卷的 PostgreSQL 17 容器中零错误恢复出 `0015d`、
387,909 条 source event、419,795 条 observation、8,186 条 receipt，以及原有
3 个 descriptor、1 个 Provider、14 条 invocation、10 个 attempt 和 13 份暂停计划。
恢复环境随后删除，主备份保留。

Core 线性迁移到 `0016_confirm_artifacts (head)`，`alembic check` 无新操作。两张
confirmation 当前/事件表和两张 artifact 当前/事件表均为零；4 个数据库保护触发器、
2 个保护函数齐全。旧的 invocation、attempt、plan 和消耗计数保持 14、10、13、13，
没有 active plan。Core 仍为 `ledger_only/global_stop=false`，lease 关闭，root/pepper
均未配置，所以 `artifact_enabled=false`。Compose 只创建了 0700、属主 65532:65532
的预备命名卷。PostgreSQL 未重启。

新 Core/Provider 镜像分别为
`sha256:4a3f9143887f27ed0afd9219cca10f649a1423efa689f67a549733ce0c6760e7` 和
`sha256:7cfd227d244d3e0ef6b59918aacadddb7fbcaa2d2bddf2128766f1be7d860994`；
两者 `pip check` 均通过。滚动后 Registry 仍只有 `status.inspect@1.0.2` active，
Provider inventory/heartbeat 新鲜健康，Lily/Nekro spool 均 healthy、pending=0，
collector watermark 差为 0。完整上线记录见 `docs/DEPLOYMENT.md` 第 16 节。

默认配置仍让 `SUPERLILY_ARTIFACT_ROOT` 与
`SUPERLILY_ARTIFACT_SECRET_PEPPER` 同时为空。Compose 只准备私有持久卷，不会启用
artifact authority；没有 artifact descriptor，也没有新增 rollout plan。

## 第一包状态门

- `two_person` 仍 fail closed；
- artifact 存储默认关闭，只支持经过独立 inspector 的 `image/png`；
- 不导入 artifact descriptor，不激活新 plan；
- 不迁移 `/wf` 或 `/tex`；
- 不把 Core 本地对象路径暴露给 Provider、模型或平台 adapter。

## 完成后顺序

`0016` 默认关闭生产签署完成后，下一步迁移文本模式 `wolfram.run`，验证现有持久 worker 的
恢复、超时、内存、输出和错误边界。Wolfram 图片继续关闭。随后用专门的一次性
artifact plan 证明 PNG reserve/upload/finalize/cleanup，最后才开始 `latex.render`。
