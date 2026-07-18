# 第三阶段 rollout plan

本目录保存经过 Git 审阅的短时执行 authority。plan 只决定生产中哪一个精确调用
可以执行，不规定模型何时选择工具，也不替代 descriptor、Provider 或调用策略。

2026-07-19 的四份 `status.inspect` plan 只用于首次生产演练，统一满足：

- caller 固定为 `admin_api`，canonical conversation 固定为
  `qq:group:1080353942`，不触发任何 QQ 发送；
- 精确绑定 `status.inspect@1.0.2`、descriptor hash 和
  `provider-status-primary`；
- 每份最多创建 1 条 executable invocation，最长窗口 4 小时，回退固定为
  `ledger_only`；
- 按 global stop、descriptor suspension、Provider quarantine、成功 canary 的顺序
  使用，后续 plan 的 expected resource version 显式包含前一步反向 mutation 的增量；
- 导入只能得到 `reviewed`，激活/暂停仍需独立 operator/break-glass 会话。窗口过期或
  计划暂停后保留为不可变历史，不复制到长期策略中。
