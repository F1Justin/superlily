# ADR 0017：Agent 产品协调与平台发送边界

状态：accepted，2026-07-30。

## 背景

ADR 0015/0016 和 `0020`–`0023` 已证明 planner-only AgentRun、独立模型 Provider
身份和一次 Wolfram continuation，但验收刻意不创建平台发送。把它接入 Nekro 时有
三种容易越界的做法：

1. 让 Nekro 自己再实现 planner/tool loop，与 Core 形成两套 authority；
2. 把 QQ token 或工具执行 token 交给模型 Provider；
3. 在用户消息回调内等待整个模型/工具流程，进程中断后无法恢复。

项目需要第一个真实群聊产品切片，同时保持模型输出只是请求、工具不发送消息、其他群
默认关闭。

## 决定

1. 新增 Core-owned `AgentInteraction`，以 `instance_id + source_event_id` 幂等接受
   一条明确 mention/reply，并只在 Git/配置共同限定的 exact conversation 中创建。
2. Core 后台协调器创建 system-owned AgentRun、检查已记录 proposal、精确提升最多
   一次 `wolfram.run@1.1.0`，并在 direct answer 或 continuation 后创建 delivery。
3. 常驻模型 Provider 的 trigger 接口只接收 run/loop ID。Provider 使用独立身份从
   Core 拉取冻结 planner input；Core 不持有 DeepSeek key，Provider 不持有 admin、
   tool-provider 或平台 token。
4. 原生短文本使用独立 `AgentTextDeliveryIntent`。Nekro adapter 只能为自身 lease；
   内容 hash、一次 fence、短 deadline 和 completion receipt 均由 Core 验证。
5. 平台成功而 completion 未确认时收敛为 `ambiguous`，不得自动重发。普通消息回调
   只有在 Core 已持久接受 interaction 后才 `BLOCK_TRIGGER`；Core 不可用则
   `CONTINUE`，保留 Nekro 原路径。
6. 首批 authority 固定为 `qq:group:708309706`、`nekro-agent`、明确 mention/reply、
   `deepseek-v4-pro@1.0.0`、最多一次 Wolfram、最多一条 8 KiB 原生文本。

## 后果

- Nekro 配置的通用聊天模型不再是该 exact slice 的第二个 planner；它只作为 Core
  拒绝/不可用时的既有 fail-open 路径。
- AgentRun 和模型 Provider 继续没有 delivery authority；平台发送只能由有账本的
  Core intent 经 adapter 完成。
- `status.inspect`、`latex.render`、history、文件/shell 和写工具不会因为产品入口
  出现而自动进入目录。
- 其他群、私聊、写操作和更长任务需要新的 exact authority 增量，不得复用本 ADR
  推断授权。

## 回滚

按顺序关闭 `SUPERLILY_AGENT_PRODUCT_MODE`、Nekro `AGENT_ENABLED` 和
`SUPERLILY_AGENT_MODE`；工具执行回落 `ledger_only` 并 pause exact rollout。
`AgentInteraction`、delivery intent 和事件保留为证据。回滚不要求删除 Provider
profile、descriptor 或历史账本。
