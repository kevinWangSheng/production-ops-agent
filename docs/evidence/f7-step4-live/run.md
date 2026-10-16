# F7 第 4 步：真实 Run 中途的全局与按目标暂停（S4c）

- 时间：2026-10-16T16:42:14Z 注入 `paymentFailure`（实验 id `f7-step4-20261016`）→ 16:57:42Z 恢复；flagd 11 个 flag 恢复后全部 `off`。协调者批准的 lab 时段，持共享重负载锁；注入前 swap 11.9/13.3 GB，checkout 5m rate 0.0125/s、p95 193 ms。
- 环境：新建 PG 17（55597，空库迁移到 `0017_web_suspension_grants`，web/worker 用 `postgres_lab runtime-roles` 的非 owner 角色）；web 在 8080（Alertmanager webhook 固定指向）；worker 真实模型、trace `lab`；不启 Observer（暂停期间 Observer 停止 S4d 已由确定性测试覆盖，合同 S4d「已覆盖」）。经本 PR 的只读代理读 Prometheus。
- 运行代码：本分支 `b272bdb` 加未提交的探测期望修正（不涉及产品代码）。
- 操作：[ctl.py](ctl.py) 以工作台操作员（Basic Auth `e2e-lab`，带 Origin）调用 `POST /controls/suspension` 与 `POST /incidents/{id}/control`，每次记录前后状态到 [run/controls/](run/controls/)；判定由 [collect.py](collect.py) 从产品行只读计算（入库版本只做了 ruff 格式化与 noqa 注释，行为与运行时相同），冻结 [run/summary.json](run/summary.json)（sha256 `cbb60eb8…07d073`）。
- 费用：DeepSeek 余额 196.41 → 195.93 CNY（0.48），见 [run/balance.jsonl](run/balance.jsonl)。
- trace（project `opspilot-lab-f7-step4-20261016`，longlived）：3 个根 run（暂停前、第一次 resume 后、第二次 resume 后）：[16:47:05](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a05b57ef-bfa7-4093-ac48-05b5f80e090b/r/00000000-0000-0000-5433-33c67c002893)、[16:50:40](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a05b57ef-bfa7-4093-ac48-05b5f80e090b/r/00000000-0000-0000-3806-c15718b89df5)、[16:53:15](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/a05b57ef-bfa7-4093-ac48-05b5f80e090b/r/00000000-0000-0000-cbc5-d3183b030ddd)。

## 时间线（事故 `ddbe2e9c`，Run `180c39be`）

| UTC | 动作 → 结果 |
|---|---|
| 16:47:05 | 告警建事故，Run 开始；16:47:12 第 1 步提交（3 次工具查询） |
| 16:47:14 | 全局暂停 → 200，全局代际 1；Run `paused` |
| 16:47:32 | 暂停前派出的模型轮写回 → 步骤 `late_result`（仅历史），worker 记 `CONTROL_DENIED`；工具查询数保持 3 |
| 16:49:16 | 解除全局暂停 → 200，代际 2；之后 75 秒内 Run 仍 `paused`、无新步骤、无新查询（不自动恢复） |
| 16:50:38 | 操作员 `resume` → Run 继续；16:50:51 第 2 步提交（查询数 6） |
| 16:50:54 | 目标 `m0-otel-20260909` 暂停 → 200，目标代际 1；Run `paused`；16:50:54.9 在途轮写回 `late_result`，worker `CONTROL_DENIED`；查询数保持 6 |
| 16:52:16 | 解除目标暂停 → 200，代际 2；之后 50 秒内 Run 仍 `paused`、无新步骤 |
| 16:53:14 | 操作员 `resume` → Run 跑完，16:57:27 `completed`，共 23 次查询 |

## 判据结论

主证据是工具计费行计数快照：计费行在派发前预留写入（`opspilot/persistence/budgets.py:253-262`），所以计数不变即窗口内没有新派发。[run/controls/](run/controls/) 中全局窗口内各快照（16:47:14 启用、16:48:38、16:49:16 解除）均为 3，目标窗口内（16:50:54 启用、16:52:16、16:52:16 解除）均为 6，解除后到下一次 resume 前也不变。

`summary.json` 的 `ok: true` 是辅助判定（PR 机器人指出：其查询时间取工具提交事件或计划步骤时间，不是派发时刻，单独不足以证明窗口内无派发；以上计数快照补足）：两个暂停窗口（全局、按目标）内新增查询 0；窗口内写入的步骤全部是 `late_result`；解除到下一次操作员 `resume` 之间没有任何步骤（不自动恢复）；暂停审计行 4 条，actor 为操作员。符合 C3 §4 l.114 与 §8 l.312（在途结果只作历史、阻止后续调用、解除不自动恢复）。没有观察已发出读请求的撤销——按合同不撤销。
