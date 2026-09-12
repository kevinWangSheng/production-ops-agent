# B3 持久恢复、人控与 HealthProfile 合同补证

日期：2026-09-11。此项只补不需要模型的确定性合同；不会把替身或局部 PG 证据写成完整 M0 通过。测试使用固定输入/可控时间语义，真实 PostgreSQL 集成仍需显式 `M0_STEP_POSTGRES=1` 才运行。

## 已补的离线合同

- `tests/test_m0_outcomes.py::test_health_requires_independent_current_complete_observation` 现在覆盖 `no_data`、`stale`、缺 profile、缺 signal、样本不足、错误状态、未来采样和超 deadline；所有路径都只能得到 `unknown`，不能提前 healthy。
- `tests/test_m0_outcomes.py::test_health_profile_is_minimal_and_rejects_duplicate_signals` 固定最小 profile schema（revision、required_signals、min_samples、freshness、required_window）并拒绝重复 signal。
- `tests/test_m0_outcomes_v4.py` 保留 paused 无审计转移拒绝、cancel/correct 与 new_run 顺序、迟到 prepared request 拒绝等失败样例。
- `tests/integration/test_m0_step_store_postgres.py` 已有真实 PG 的进程中断、部分工具结果重建、控制代次、预算不重置和迟到提交拒绝合同；本轮专属 55431 实例显式运行结果为 **27 passed**（`b3-postgres-step-store-check.txt`）。
- `tests/integration/test_m0_budget_postgres.py` 本轮专属 55431 实例运行 **9 passed**，并以 `M0_B_RESTART=1` 单独执行 DB stop/start 合同 **1 passed**（`b3-postgres-budget-check.txt`、`b3-postgres-restart-check.txt`）；测试 finally 恢复后已显式 stop，数据目录保留。

## 八包缺口与失败样例索引

| 机制 | 当前结果 | 失败/证据不足样例 | 处置 |
|---|---|---|---|
| DB 短暂故障 | 部分 | `test_database_restart_retains_unknown_and_denies_when_down` 实例 stop/start 及 `STORAGE_UNAVAILABLE` 断言通过 | 仍缺 ModelStep/ToolOperation 全链路在短故障中的组合证据；保留为后续真实合同。 |
| 发布异常→既有事故竞争 | 证据不足 | 当前合同没有两条并发发布/事故控制事件的真实时序样例 | 先用可控时钟建最小事件序列，再由独立审查确认 owner。 |
| no-data/stale 交接 | 离线通过 | `no_data`/stale 健康样例若被标 healthy 会命中 `UNPROVEN_HEALTHY_STATE` | 已加入回归；仍缺持久 Run 到期交接的 PG 组合。 |
| 观察缺口不得提前 healthy | 离线通过 | 缺 profile、缺 signal、窗口不足、未来采样 | `independent_health()` 固定返回 unknown；不建 HealthProfile 平台。 |
| HealthProfile 变更/乱序采样 | 证据不足 | profile_revision/窗口错配由离线检查拒绝；没有持久观察流的乱序重放 | M1 前定义版本迁移合同，保留旧观察，不重标新鲜度。 |
| 全局/目标暂停与 resume | 证据不足 | v4 paused 无审计转移固定拒绝；尚无目标级暂停存储与 resume 并发 | 不扩建平台；将控制事件加入真实 PG 合同后再审查。 |
| 单独观察授权并发 | 证据不足 | 现有 Action 只断言 query/mutate/release_gate 权限，未覆盖观察授权并发 | M1 前补 owner/lease/目标 scope 竞争样例，未授权查询必须 denied。 |

### 结论

B3 当前完成的是 schema/unknown 语义和已有 PG 子集的索引，**不是八项机制全部通过**。原始失败、默认 skip 和未测项均保留；任何新增真实 PG 环境操作都必须使用专属实例、记录输出并在完成后停止服务但保留数据。
