# ADR 0012：确认等待期与内容寻址 artifact 的具体协议

- 状态：accepted
- 日期：2026-07-19
- 细化：ADR 0003、0004、0007、0011

## 背景

`status.inspect@1.0.2` 已完成故障矩阵和稳定窗口，第三阶段下一包是冻结编号的
`0016_confirm_artifacts`。既有 ADR 已规定确认必须绑定精确请求、artifact
必须经过 reserve/upload/finalize，但尚未解决两个会直接影响正确性的问题：

1. descriptor 的 `timeout_ms` 是执行预算，不能从等待人类确认时开始倒计时；
2. M3 当前在 invocation 创建时消费 rollout 次数，但需要确认的调用只有在批准后才
   真正获得排队 authority。

同时，Provider 本地路径、任意 URL、可重用上传口令或“先成功后补文件”都会破坏
attempt/fence 与结果的一致性。

## 决定

### 确认

1. `0016_confirm_artifacts` 线性接在已经部署的
   `0015d_rollout_plans` 后，不改写任何 `0015*` 历史。新增 confirmation challenge
   与只追加 event；challenge 绑定 invocation、request/input/principal/policy hash、
   caller、过期时间和需要的批准数。
2. `confirmation=always` 或需要写确认的 descriptor，在其他 policy gate 均通过后进入
   `awaiting_confirmation`。随机 confirmation ID 不是 bearer credential；批准还必须
   使用原 caller credential，并逐字提交 confirmation ID、request/input/principal
   hash。状态与资源版本使用数据库 CAS，成功后即单次消费，重放只能返回原结果。
3. 等待期使用独立、数据库时间签发的 confirmation expiry。批准时才把 invocation
   execution deadline 设置为 `DB now + descriptor.timeout_ms`，并受 rollout plan
   expiry 上限约束；Provider 自报时间没有 authority。
4. 对需要确认的调用，创建 challenge 时不增加 rollout counter。批准事务重新锁定并
   验证 active plan、plan/item/hash/resource version、descriptor/Provider/runtime、
   global stop、时间窗口、调用上限和原始 policy snapshot；只有成功进入 queued 才
   原子消费一次。暂停、漂移、过期或额度耗尽只会拒绝/过期，不会排队。
5. 第一包只开放单人 `always/on_write` challenge。`two_person` 的表结构和事件语义保留，
   但在跨平台强身份与两个独立确认渠道完成前 fail closed 为
   `confirmation_unavailable`，不把两个 bot credential 或两个昵称冒充两个人。
6. 拒绝、过期、批准、消费和幂等冲突均留下只追加证据。确认 ID、hash 和状态可审计，
   bearer、cookie、平台原始消息与密码不进入 confirmation 表。

### Artifact

7. descriptor 新增可选 `artifact_policy`。没有 artifact MIME 权限时它必须为空；有
   artifact 权限时必须同时声明总字节预算、单件/数量/尺寸上限、reservation TTL，且
   `artifact_bytes` 必须是 hard enforcement。旧 descriptor 缺少该字段仍按无 artifact
   authority 解析，不改变原 authority hash。
8. Artifact 状态是 `reserved -> uploading -> finalized`，或 `rejected/expired`；
   状态、引用和清理都需要匹配的只追加 event。记录绑定 invocation、attempt、Provider、
   当前 fence、允许 MIME、总量/单件/尺寸、classification、scope、expiry 和 producer
   descriptor。只有 running 的当前 attempt 能 reserve/finalize。
9. reserve 使用 Provider bearer、attempt secret/fence 与 Idempotency-Key。一次性上传
   secret 由 Core 使用独立、至少 32 字节的 artifact pepper 对 reservation identity
   派生，数据库只保存 hash；未消费的相同 reserve 重放可重新得到同一 secret，
   不需要存明文。secret 不能授权 finalize、读取 artifact 或执行其他工具。
10. upload 使用 Provider bearer 加 upload secret，原始 body 直接流入 Core 管理的
    quarantine 临时文件。Core 独立限制 Content-Length/实际字节、计算 SHA-256、验证
    MIME 和尺寸；不接受 multipart 路径、Provider 本地路径、symlink 或远程 URL。
    第一包只把有独立解析器的 `image/png` 标为受支持 MIME，未知 MIME 使工具不 eligible。
11. finalize 再次验证当前 attempt/fence、上传观察值和 descriptor 上限，然后先以
    `sha256/<prefix>/<digest>` 原子放入内容寻址存储，再提交数据库 finalized 状态。
    数据库提交失败最多留下不可见 orphan，不能留下“数据库已成功但文件不存在”。
12. complete 必须显式提交 artifact reference 列表。Core 在同一事务中验证全部记录
    finalized、绑定当前 attempt/fence、hash/MIME/字节/尺寸完全一致，并使
    `usage.artifact_bytes` 等于引用总量；随后才允许 invocation 成功并标记 referenced。
13. Core 默认 `artifact_root` 和 pepper 为空，因此迁移上线不会启用 artifact。
    需要 artifact 的 descriptor 在存储、pepper 或 MIME inspector 缺失时显示明确的
    reduced-authority reason。存储目录为 0700、对象为 0600，不放入 PostgreSQL。
14. reaper 对 reserved/uploading 的过期、失败 attempt、临时文件和不可见 orphan 做
    幂等清理；finalized 且被引用的对象在 retention 到期前不能删除。artifact 元数据与
    审计保留独立，清理字节不会删除 provenance 行。

## 后果

ADR 0011 的“创建时消费 rollout 次数”继续适用于无需确认、直接排队的调用；本 ADR
只把确认型调用改为批准时消费。`0016` 默认禁用上线不开放写工具、模型 caller、图像
Wolfram 或 LaTeX；它只建立共同账本和存储边界。文本模式 `wolfram.run` 可以在
artifact canary 前迁移，但任何图像输出和 `latex.render` 必须等本协议签署。

## 必需证据

- SQLite/PostgreSQL 的迁移往返、drift、confirmation CAS/幂等/过期/重放、plan
  pause/expiry/counter 并发和数据库时间测试；
- artifact reserve 重放、上传 secret 错误/复用、旧 fence、超限、伪 MIME、错误
  hash/尺寸、部分上传、finalize 并发、complete 前引用和总量核对；
- Core/Provider/数据库在 upload/finalize 各断一次，证明 partial 不可见、orphan 可清理；
- 路径穿越、symlink、权限、日志/错误/导出 secret 扫描；
- 默认空 root/pepper 的生产迁移、备份恢复、零 confirmation/artifact 行和
  `status.inspect`/旧命令不变。
