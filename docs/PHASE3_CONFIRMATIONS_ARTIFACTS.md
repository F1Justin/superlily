# Phase 3：`0016` 确认与 Artifact 实施包

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

## 第一包状态门

- `two_person` 仍 fail closed；
- artifact 存储默认关闭，只支持经过独立 inspector 的 `image/png`；
- 不导入 artifact descriptor，不激活新 plan；
- 不迁移 `/wf` 或 `/tex`；
- 不把 Core 本地对象路径暴露给 Provider、模型或平台 adapter。

## 完成后顺序

`0016` 默认关闭生产签署后，先迁移文本模式 `wolfram.run`，验证现有持久 worker 的
恢复、超时、内存、输出和错误边界。Wolfram 图片继续关闭。随后用专门的一次性
artifact plan 证明 PNG reserve/upload/finalize/cleanup，最后才开始 `latex.render`。
