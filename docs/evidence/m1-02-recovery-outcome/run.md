# F6 验收投影：IncidentScenario → RecoveryOutcome（#140）

- 日期：2026-10-08；分支 `feature/F6-recovery-outcome`，基于 main `c9546c9`
- 未启动实验环境、未调用模型、未查遥测；全部来自本机测试与临时 PostgreSQL 17.9（55641，数据目录在会话临时目录）。

## 接口

- `opspilot.acceptance.recovery_outcome(scenario: IncidentScenario, records: RecoveryRecords) -> RecoveryOutcome`（实现在 `opspilot/acceptance_recovery.py`，`acceptance.py` 再导出）。
- `RecoveryRecords(incident, sessions, controls, grants)`：`ObservationStore.incident_records(incident_id)` 的三项 + `ObservationStore.table_privileges()`。
- `permissions_from_grants(grants, *, human_control) -> tuple[str, ...]`。

## 字段来源

| 字段 | 来源 |
|---|---|
| incident_lifecycle / incident_mode | `opspilot_incidents` 行 |
| target | 会话行 `target`（不可变绑定） |
| recovery_verdict / recovery_confirmed / latest_sample_verdict / healthy_window_seconds | `replay_history(最新会话)` |
| recovery_reasons | 重放 integrity 码 + 最新已采纳采样的逐信号重算判定（INSUFFICIENT_TRAFFIC / REQUIRED_TELEMETRY_MISSING / MISSING_SIGNAL:x / STALE_TELEMETRY:x / DEGRADED_SIGNAL:x）+ CONTINUED_DEGRADATION / OBSERVATION_UNCONFIRMED / NO_HEALTH_PROFILE |
| used_sample_count | 会话行 `adopted_count`（重放核对） |
| observation_ended / observation_ended_reason | 会话行 state / ended_reason |
| human_interaction / handoff_reasons | ended_reason ∈ {deadline_expired, max_samples_exhausted} → handoff，原因 = 结束码 + recovery_reasons |
| recovery_samples[*] | 采样行 + 读数行（value/status/点数/query/source/窗口/raw_sha256/捆绑内 body_sha256）+ 重放逐信号判定；evidence_id = `<sample_id>:<signal>` |
| recovery_profile_revision / content | `opspilot_health_profiles` 行 |
| recovery_handled_at | 会话行 `authorized_at` |
| observation_sessions / observation_authorization / sample_jobs / handling_audit | 会话行合同字段投影 / 最新会话 / 采样行 job 集合 + 活动任务 / `opspilot_controls` 行 |
| actions | 见 development.md 规则 |
| permissions | `permissions_from_grants(实测权限, human_control=有控制行)` |
| model_requests | 恒空（观察路径无模型客户端，import 边界由 tests/test_m1_observer.py 校验） |

## 测试

- 单元 `tests/test_f6_recovery_outcome.py` 8 passed（确认恢复投影、篡改 → unknown + 完整性码、degraded / 撤流量交接与原因、缺必要信号命名、进行中会话、无会话、权限推导含越权/删除/不可读与空测量拒绝）。
- PG `tests/integration/test_m1_02_recovery_outcome_postgres.py` 5 passed：Observer 登录实测权限（samples/readings/endings SELECT+INSERT，sessions/incidents SELECT+UPDATE，profiles SELECT，runs/controls 无）；真实 Observer 采样确认恢复后的投影（投影期间桩 Prometheus 请求数不变）；次数耗尽交接；`register_remediation` 路径 → `human_control` + `record_handling`；额外 `GRANT UPDATE ON opspilot_runs` 的登录 → `investigation_write:opspilot_runs`。
- 既有 M1-02 PG 套件 + 重放套件 108 passed；`make check` 见 PR。
