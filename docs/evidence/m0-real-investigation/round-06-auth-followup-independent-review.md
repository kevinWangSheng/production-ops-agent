# B6 认证线路复验独立复核

日期：2026-09-12。由未参与实现的全新上下文只读复核当前 B6 认证复验合同、原始输出、账本、结果、退出矩阵、ROADMAP 与任务记录；未运行模型、trace 或付费调用。

## 初次发现与处置

1. 合同原先将实验描述为“无工具挂载”，但 stdout 显示上游默认 toolset 定义仍被初始化，模型生成了两个 `run_bash_command` 请求。已将合同和结果改为：工具定义暴露但未执行，严格“无工具挂载”未验证；`tool_execution=0` 仅证明本次没有执行。
2. B6 候选 ledger 原先只在环境 worktree，当前提交树不可回读。已补入 [`m0-06-b6-ledger.json`](m0-06-b6-ledger.json)，并在结果中固定 SHA-256 `150c9d34d775e0b8950fd2aacd255301f166cf56e939a1e2674e7eafcdba46c9`；403/453 token 与 0.004086 CNY 上界可由账本重算。

## 复核结论

- 认证/线路只记为“部分通过”：观察到模型内容，但 wire HTTP、usage 和退出码未捕获；没有升级为完整 HTTP 成功。
- shell tool-call 被当作不可信模型输出，未执行，不构成宿主写操作证据。
- B6 正式同条件比较、judge 人工校准和保留集盲测仍为证据不足；费用授权与实际账单/unknown 预留明确分开。
- 退出矩阵、ROADMAP、任务记录和 PR 描述均保持 SPEC gate `not cleared`，未修改 feature passes。

结论：初次两项 P1/P2 已修正，无剩余文档边界发现。
