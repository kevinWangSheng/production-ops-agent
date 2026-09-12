# 工作包 3：控制与观察确定性合同

日期：2026-09-12。测试不调用模型、不上传 trace；PG 使用专属 55431 实例，完成后已停止并保留数据目录。所有测试显式以 `M0_CONTROL_POSTGRES=1` opt-in。

## 合同与结果

| 项目 | 测试 | 结果 | 证据/边界 |
|---|---|---|---|
| 发布异常转事故 vs 人工接管竞争 | `test_release_failure_then_takeover_generation_race_keeps_first_control` | **通过（机制子集）** | 先到 generation 0 的 `cancel` 获得 generation 1；旧 generation 的 `correct` 返回 `CONTROL_CONFLICT`；安全 `control_snapshot` 只导出 generation/action，不导出 payload。 |
| HealthProfile 变更与乱序采样 | `test_health_profile_revision_and_capture_order_are_unknown_with_fixed_clock` | **部分通过（schema/存储子集）** | 最小 schema 为 `revision/required_signals/min_samples/freshness_seconds/required_window`；固定 captured_at 的 PG 回读保持 `profile-v1` 与旧 observation `profile-v0` 不匹配。未实现持久观察流的乱序拒绝/unknown 重放。 |
| 全局/目标暂停 + resume | `test_global_pause_and_resume_are_fail_closed_until_control_contract_exists` | **证据不足** | 当前 `StepStore.control` 对 `pause`/`resume` 均 fail-closed 为 `INVALID_INPUT`，generation/state 不变；尚无暂停期间拒绝查询/模型、resume 不隐式恢复的产品状态机。 |
| 单独观察授权并发 | `test_observer_authorization_cannot_borrow_investigation_identity` | **通过（身份隔离子集）** | 新 Run 切换后旧 investigation Run 的 `claim` 返回 `IDENTITY_CONFLICT`；尚无独立 observer 授权 API 的并发竞争测试。 |

## 执行输出

- 成功重跑：[`round-06-control-contracts-postgres.txt`](round-06-control-contracts-postgres.txt)，`4 passed`，包含 `-rA` 测试名。
- 初次实现失败样例：[`round-06-control-contracts-postgres-initial-failures.txt`](round-06-control-contracts-postgres-initial-failures.txt)；修复为适配安全 snapshot 形状、PG JSONB 映射和时区回读后才重跑通过。
- 测试文件：[`test_m0_control_contracts_postgres.py`](../../../tests/integration/test_m0_control_contracts_postgres.py)。

## 结论

本轮补齐了 generation 冲突、HealthProfile 最小 schema/旧版本保真和 investigation identity 隔离的真实 PG 子集；pause/resume 与独立 observer 授权仍明确为产品缺口，不能汇总为工作包 3 通过。失败样例与 skip/opt-in 边界均保留，SPEC gate 不变。
