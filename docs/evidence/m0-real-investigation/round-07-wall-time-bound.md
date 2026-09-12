# 工作包 7：工具 wall-time 与清理上界（round-07）

日期：2026-09-12。只使用已提交账本回算，0 模型调用、0 环境操作。分位数按线性插值计算，样本量极小（n≤8），只能作候选校准依据，不是 SLA。

## 数据来源

| 来源 | 记录 | 可用维度 |
|---|---|---|
| [`round-02-final-usage.json`](round-02-final-usage.json) + `m002-*-result.json` | M002 8 条 per-run（4 调查、2 报告、2 PG 探针） | 每 Run `tool_wall_seconds`（累计）、`tool_queries`、`sum_request_wall_seconds`、首发到末响应 |
| [`round-02-activity-timing.json`](round-02-activity-timing.json) | M002 阶段汇总 | `real_holmes_tool_wall_seconds=10.378`（62 次查询）、各阶段模型 HTTP wall |
| [`holmes-per-run-usage.json`](../m0-real-environment/holmes-per-run-usage.json) | M0-01 Holmes 6 条 | 首请求到末响应（含中间工具，不含进程启动） |
| [`m0-06-b6-ledger.json`](m0-06-b6-ledger.json) | B6 候选 2 请求 | `ended_at-started_at` 单请求 wall |
| [`round-05-b4-usage-ledger.json`](round-05-b4-usage-ledger.json)、[`round-06-protocol-ledger.json`](round-06-protocol-ledger.json)、[`round-06-upstream-comparison-ledger.json`](round-06-upstream-comparison-ledger.json) | B4/协议/上游 | 只有 HTTP 次数与 usage，无时序字段 |
| M003/M004 | — | 逐请求 token 与 wall-time sidecar 未随安全摘要提交（见 v4 校准段），**证据不足** |

## 观测分布

| 指标 | n | min | 中位 | p95 | max | 候选上限 | max/上限 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 单工具 elapsed | 0 | — | — | — | — | 30 s | 未逐次持久化；只能给出 Run 均值 0.142–0.182 s/次（M002 4 条调查 Run），M002 总体 10.378 s/62 次 = 0.167 s |
| 每 Run 累计工具 wall（M002 6 条 wrapper Run） | 6 | 0.000 | 2.147 | 3.105 | 3.168 | 240 s | 1.3% |
| 每 Run 模型请求 wall 合计（M002 8 条） | 8 | 0.655 | 29.076 | 74.162 | 77.276 | — | — |
| 单请求模型 wall（可分离样本：M002 单请求 Run 3 条 + B6 2 条） | 5 | 0.655 | 2.288 | 75.5 | 77.276 | 360 s | 21.5% |
| M0-01 Holmes 首请求到末响应（模型+工具） | 6 | 40.478 | 61.098 | 80.753 | 83.508 | 1800 s（Run） | 4.6% |
| M002 首发到末响应（每 Run） | 8 | 0.655 | 34.482 | 101.725 | 114.889 | 1800 s（Run） | 6.4%（114.9 s 为 PG 探针含人工间隔） |

最长模型单请求（68.4 s、77.3 s）来自 M002 报告阶段的大输出响应（completion 15,104 tokens 一例、另一例 usage 缺失），不是工具等待。M0-01 `fault-handoff-01` 单请求 66.1 s 对应 8,191 completion tokens 的 `length` 截断。

## 清理上界的实现与证据

- `tool_remaining`（`scripts/m0_environment/holmes_baseline.py`）在派发前取 `min(单工具 30 s, 240 s − 已累计, Run 剩余, scope 剩余)`；剩余 ≤4 s 时直接拒绝派发并按最先耗尽的边界报 `tool total deadline` / `deadline reached` / `query authorization deadline reached`。
- `run_child`（`scripts/m0_environment/round02.py`）把子进程 `communicate` 超时设为 `wall − 2×grace`，超时后 `terminate`→grace→`kill`→grace，未回收则报 `child not reaped after kill`；默认 grace 2 s，即每次派发预留 4 s 清理。
- 新增确定性测试 `tests/test_m0_tool_wall_time.py`（`9ebc7be`）：固定时钟下累计达 236 s 时拒绝派发且不 spawn；累计耗尽时报工具边界而非 Run/scope 边界；剩余 5 s 时忽略 SIGTERM 的子进程在 6 s 内被 kill 并回收、耗时计入累计、下一次派发被拒。既有 `test_child_that_ignores_terminate_is_killed_and_reaped` 覆盖单次 kill 路径。
- 局限：测试用真实函数复现 `guarded_send` 的“先算上界、后 spawn、finally 记账”顺序，未直接驱动该闭包；`tool_io_lock` 使工具串行，没有并行工具清理样本；M003/M004 无时序 sidecar。

## 推荐冻结值（候选，待用户批准）

| 项 | 候选值 | 依据 |
|---|---|---|
| 单工具 wall | 30 s | 已观测 Run 均值 <0.2 s；30 s 留出 >100× 余量并覆盖 4 s 清理预留。 |
| 每 Run 工具累计 | 240 s | 已观测最大 3.17 s（20 工具上限下 ≈ 4 s 均值）；240 s 为 >75× 余量，确定性测试已证明超限拒绝派发。 |
| 单次清理上界 | 4 s（terminate 2 s + kill 2 s） | `run_child` 现值；派发在剩余 ≤4 s 时拒绝，保证清理时间不被工具预算吞掉。 |
| 单模型请求 | 360 s | 已观测最大 77.3 s（15k 输出 tokens）；候选输出上限 16,384 tokens 下预计同量级，360 s ≈ 4.7× 余量。 |
| Run 总 wall | 1800 s | 已观测调查 Run ≤ 84 s（首发到末响应）；未含进程启动、PG 重建与人工步骤，保留 1800 s。 |

不建议现在收紧：样本 n≤8，且 M003/M004 缺 sidecar。冻结须由用户在账单、B3/B4 证据后批准；本文不修改 [v4 校准段](../../testing/first-investigation-v4-2026-09-10.md) 的候选值，也不打开 SPEC gate。工作包 7 状态保持**部分**：工具/清理上界已有确定性证据，供应商账单（B8）仍未核对。
