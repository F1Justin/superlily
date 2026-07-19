# 第三阶段 rollout plan

本目录保存经过 Git 审阅的短时执行 authority。plan 只决定生产中哪一个精确调用
可以执行，不规定模型何时选择工具，也不替代 descriptor、Provider 或调用策略。

2026-07-19 的五份 `status.inspect` plan 只用于首次生产演练，统一满足：

- caller 固定为 `admin_api`，canonical conversation 固定为
  `qq:group:1080353942`，不触发任何 QQ 发送；
- 精确绑定 `status.inspect@1.0.2`、descriptor hash 和
  `provider-status-primary`；
- 每份最多创建 1 条 executable invocation，最长窗口 4 小时，回退固定为
  `ledger_only`；
- 前四份按 global stop、descriptor suspension、Provider quarantine、成功
  canary 的顺序使用，后续 plan 的 expected resource version 显式包含
  前一步反向 mutation 的增量；第五份只用于在 deadline 内直接证明
  rollout plan pause 独立阻止 lease；
- 导入只能得到 `reviewed`，激活/暂停仍需独立 operator/break-glass 会话。窗口过期或
  计划暂停后保留为不可变历史，不复制到长期策略中。

2026-07-19 的第二组八份计划用于 Phase 3b 故障矩阵，窗口统一为
07:00–13:00 CST，每份仍只允许 1 条 executable invocation：

- `retry-fence` 同时证明 Provider 在已开始 attempt 中断后，短 lease 能安全重排队、
  新 attempt 获得单调 fence、旧 worker 与重复完成均被拒绝，第二 attempt 成功；
- `invalid-output` 与 `clock-skew` 分别证明非法结果 fail closed，以及 Provider 自报
  时间只作诊断、不能延长数据库 deadline；
- `cancel-ack`、`cancel-race`、`cancel-unack` 分别证明明确取消、完成竞态和未确认
  取消的三种不同终态；后两项预期产生受解释的 `unknown_completion`，不是成功；
- `core-outage` 与 `database-outage` 只证明持久账本在真实服务中断后由 reaper 收敛，
  不在中断期间重放完成请求；
- 八份计划均精确绑定当前 `active/rv4` descriptor 与 `active/rv3` Provider。一次只激活
  一份；每项完成或失败后都先暂停当前 plan，再处理下一项。

`wolfram-text-success-20260719.json` 是文本 Wolfram Provider 的首次生产计划：

- 只允许 `admin_api + qq:group:1080353942 + wolfram.run@1.0.0 +
  provider-wolfram-primary`，精确绑定 descriptor `active/rv2` 与 Provider `active/rv1`；
- 最多 1 次调用，只返回有界文本，不生成 artifact、不触发 QQ 发送；
- 固定 `2+2` canary 完成后立即暂停，旧 `/wf` 继续作为回滚入口。

`latex-artifact-success-20260719.json` 是 LaTeX Artifact Provider 的首次生产计划：

- 只允许 `admin_api + qq:group:1080353942 + latex.render@1.0.0 +
  provider-latex-primary`，精确绑定 descriptor `active/rv2` 与 Provider `active/rv1`；
- 最多 1 次调用，只接受固定 JSON 字段中的 LaTeX，生成 1 张有界 PNG，不触发 QQ
  发送；
- 固定 `x^2+y^2=z^2` canary 完成后立即暂停，旧 `/tex` 继续作为回滚入口。
