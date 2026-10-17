# 缺陷合同点裁决（2026-10-17）

- 状态：已完成（裁决已记录，修复各自按缺陷 PR 处理）
- 出处：用户 2026-10-17 会话裁决，按分诊推荐；分诊基于 main `51264b5`
- #255 r1 补充：lead 按用户 2026-10-17 常设指示「决策先按推荐走」作出；依据为 #255 测试作者报告与 `opspilot/domain/runs.py` 的 `RUN_EXECUTION`

| Issue | 裁决 |
|---|---|
| #255 结论后追问开新 Run 反复触发复盘生成 | 事故仍有未终态 Run（含 `waiting_human` 与交接停放）时一律不触发复盘生成；Run 进入终态后立即恢复候选；关闭后再生成保持现状。分诊推荐的修复面是 `opspilot/knowledge/jobs.py` 的 `_CANDIDATE_SQL`，F2 不改变「未终态 Run」集合的定义。 |
| #255 r1 补充 (a) | 未终态 = {queued, running, paused, waiting_human, blocked}，终态 = {completed, failed, cancelled, budget_exhausted}，按 `opspilot/domain/runs.py:37-76` `RUN_EXECUTION` 状态机有无出边定；paused 计为未终态，交接停放不论落在 blocked 还是 waiting_human 都算未终态。该集合比 `ACTIVE_RUN_EXECUTIONS`（`runs.py:78`，只含 queued/running/waiting_human）宽，不能直接复用后者。 |
| #255 r1 补充 (b) | 判定条件为「该事故的任一 Run」未终态，不只看当前 Run。 |
| #255 r1 补充 (c) | 作为本裁决的直接后果，F13 验收 fixture `tests/f13_acceptance_support.py` 的 `seed_upstream`（目前种入 queued Run）及依赖它的 PG 测试，允许按合同变更改为种入终态 Run 并做相应调整（follow_up 路径须避开 `REOPEN_REQUIRED`）。验收断言本身不放宽；由独立测试作者改动，实现者不得改。 |
| #250 eval `judge_once` 未对当前请求执行 r9 规范标识校验 | r9 规范标识校验覆盖当前请求；未认证调用同样拒绝。 |
