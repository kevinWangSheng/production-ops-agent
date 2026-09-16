# M1-01 子任务：追问/纠正/取消（人工控制）

## 目标与范围
在 DurableStore 控制代际基础上落实 C3 人工控制、全局/目标级暂停，以及 follow-up/correction/cancel 的持久语义；迟到结果仅登记历史，不得覆盖新代际。范围为领域、持久化和服务接口，不含 UI/SSE。

## 依据与前提
依据 `SPEC.md` M1-01 bounded-open 门槛、`PRODUCT-CONSTRAINTS.md` 人工控制优先级、C3 技术方案及现有 DurableStore 合同。分支基于 `origin/chore/durable-store-hardening`；与重启子任务共享 `persistence.py`，本任务未扩展其结构之外的改动。

## 实现与证据
- 领域控制模型位于 `opspilot/domain/control.py`，实现控制代际条件更新、全局与目标暂停、人工优先级和 scope 栅栏。
- 持久化控制入口为 `DurableStore.control()`；`publish()`/步骤提交共用代际与租约栅栏，迟到结果写入 `late_result` 历史。
- 领域确定性断言见 `tests/test_domain_contracts.py`、`tests/test_domain_state_machines.py`；PG 控制/迟到结果合同见 `tests/integration/test_m0_control_contracts_postgres.py`、`tests/integration/test_m1_durable_state_postgres.py`。

## 验证
- `make setup`：成功，创建并同步 `.venv`（Python 3.12.13）。
- `make check`：通过；ruff、format、mypy 均通过；pytest `1051 passed, 84 skipped, 2 xfailed`。
- PG 集成测试因 `M1_DURABLE_POSTGRES=1` 未在本次环境设置而按项目约定跳过；未将跳过结果宣称为 PG 运行证明。

## 未完成项与下一步
需要在具备本地 PostgreSQL 的环境中运行 `M1_DURABLE_POSTGRES=1` 的并发追问/取消和迟到结果测试，并完成独立新上下文审查、提交/推送和 PR 的 CI/code-review 闭环。当前未修改 feature passes 或扩大产品权限。

## C3 第 4 节逐条映射

| C3 要求 | 现有确定性证据 |
|---|---|
| 人工操作用 `expected_version` 条件更新并递增主体代际 | `tests/test_domain_contracts.py::test_a_control_operation_requires_the_expected_version`、`test_each_accepted_control_increments_the_control_generation`；PG `tests/integration/test_m1_durable_state_postgres.py::test_concurrent_follow_up_and_cancel_have_one_winner_generation` |
| Run 绑定主体且单一当前 Run 可更新结论，跨主体不得修改 | `tests/test_domain_contracts.py::test_an_adoption_rechecks_every_recorded_control_version`；`tests/integration/test_m1_durable_state_postgres.py::test_rebuild_reads_a_consistent_snapshot` |
| 旧 Run 迟到结果仅保存历史 | `tests/integration/test_m1_durable_state_postgres.py::test_correction_rejects_late_publish_and_keeps_history_only`、`test_old_generation_step_and_tool_writes_are_fenced` |
| 暂停继续接收事件但不自动查询 | `tests/test_domain_contracts.py::test_suspension_outranks_resume_mode_and_observation_authorization`（含 `event_intake_allowed` 与查询拒绝）；`tests/integration/test_m0_pause_observer_postgres.py::test_pause_blocks_queries_but_keeps_events` |
| 取消保持终态，新调查使用新 Run；取消不关闭事故 | `tests/test_domain_contracts.py::test_a_cancelled_run_is_terminal`；`tests/integration/test_m1_durable_state_postgres.py::test_paused_run_reaches_terminal_state_on_cancel` |
| 关闭事故新异常默认新关联事故，可显式重开 | `tests/test_domain_contracts.py::test_new_anomaly_after_close_creates_new_incident`、`test_reopen_is_explicit` |
| 重绑定/合并/拆分撤销旧授权并保留历史身份 | `tests/test_domain_contracts.py::test_an_adoption_rechecks_every_recorded_control_version` |
| 每轮固定输入水位，新输入不得显示已分析 | `tests/test_domain_contracts.py::test_the_input_watermark_only_moves_forward_and_only_while_running`；`tests/integration/test_m1_durable_state_postgres.py::test_rebuild_drops_pending_tools_from_a_superseded_generation` |
| 全局/目标暂停是确定性状态，目标只能由登记身份解析 | `tests/test_domain_contracts.py::test_target_scope_resolves_to_registered_identities_not_names` |
| 暂停优先于 resume/automatic/human-owned observation，覆盖查询与 Observer 采样但不阻止事件/历史/人工操作 | `tests/test_domain_contracts.py::test_suspension_outranks_resume_mode_and_observation_authorization` |
| scope generation 递增；领取/预算/请求/采纳复核版本；在途结果失效并保留历史 | `tests/test_domain_contracts.py::test_a_suspension_transaction_increments_its_scope_generation`；PG `test_paused_incident_cannot_be_claimed_or_published`、`test_correction_rejects_late_publish_and_keeps_history_only` |
| 解暂停只移除该层阻挡，不恢复旧任务/授权/采样窗口 | `tests/test_domain_contracts.py::test_resume_does_not_restore_an_observation_authorization`、`test_a_paused_run_resumes_as_a_new_attempt`；PG `test_paused_run_reaches_terminal_state_on_cancel` |
