# F3 验收测试与证据映射（六步）

独立测试作者（未参与 PR-3b 实现、未读实现分支）按 PR-3b 合同预审（仓库外 `~/.agent/handoffs/opspilot-prereview-f3-3b-2026-10-24.md` 第 6 节，见 [F3 任务记录](../tasks/2026-10-17-f3.md)；U-A 按推荐，可逆默认照做）、[F3 合同 r1](../tasks/2026-10-17-f3-contract.md) 与 [feature_list.json](../../feature_list.json) F3 六步编写。外部入口是 `IncidentScenario -> IncidentOutcome` 加只读投影 `investigation_evidence` 与 `audit_timeline`；每个场景 `feature_id="F3"`、`acceptance_step` 为对应步号。

## 判定器（一套判定，两种来源）

[`tests/acceptance/test_f3_step5_rubric_gates.py`](../../tests/acceptance/test_f3_step5_rubric_gates.py) 定义对一条 **Run 记录** 的确定性判定：第 1 步观测字段、第 2 步五类分组、第 5 步 rubric 确定性门 1–4 与场景形状（一事故一 Run、代际 1、无 `attached`）、第 6 步只读动词与绑定目标、R10 探测输出。同一判定用在：

- PG 产品路径（[`tests/integration/test_f3_acceptance_postgres.py`](../../tests/integration/test_f3_acceptance_postgres.py)）：Run 记录由该文件的 `run_record` 从实时投影构建；
- 冻结证据（[`tests/acceptance/test_f3_step5_real_runs.py`](../../tests/acceptance/test_f3_step5_real_runs.py)）：Run 记录读自 PR-3b 两次 kind Run 的 `summary.json` 的 `f3_acceptance` 块（字段见判定器文件顶部 ASSUMED，PR-3b 收集脚本须按此输出）。

判定器不替代审查者：rubric 第 5–8 条与第 2 步「summary 与结构化分组一致」由 U3-a 两名审查者在每次真实 Run 上判定，结论写在各 Run 的 `run.md`。门 2 只验证被引证据满足评测签名，不验证 fact 措辞点名 payment Charge；门 3 只验证否定引用了本 Run 的 ok 证据，不验证证据真能否定该部署。

## 步骤 → 测试 → 证据

| 步 | 确定性测试 | 已有证据 | 新证据 |
|---|---|---|---|
| 1 观测带来源、查询、窗、新鲜度、目标、可解析引用 | PG：`test_step1_every_observation_carries_its_attribution`、`test_step1_legacy_view_lists_missing_fields_in_a_read_only_transaction`；冻结：`test_step1_every_observation_of_the_real_run_is_attributed_and_resolvable[run-1/2]` | PR-1 验收 `tests/integration/test_f3_pr1_projection_acceptance_postgres.py`（合成数据） | 两次 kind Run 的 summary：[run-1](../evidence/f3-step5-competing-cause/run-1/summary.json)、[run-2](../evidence/f3-step5-competing-cause/run-2/summary.json)；R4 旧库检查：无可用归档库，按 R-7 引用 PR-1 合成旧行测试（见 [run.md](../evidence/f3-acceptance-live/run.md)） |
| 2 五类可区分 | PG：`test_step2_five_claim_kinds_stay_apart`；冻结：`test_step2_real_run_keeps_the_five_claim_kinds_apart[run-1/2]` | PR-1 验收（同上） | summary：同上；审查者 summary 一致性判定（第 2 步门槛）：[审查者结论](../evidence/f3-step5-competing-cause/run.md)，两次 Run 均两名审查者判一致 |
| 3 缺源被点名、不当作已支持结论 | PG：`test_step3_a_failed_source_is_named_as_a_gap`；冻结：`test_step3_d9_rerun_of_scenario_a_on_the_current_prompt_revision` | [PR-2 证据](../evidence/f3-step3-coverage/README.md) A、B；`tests/acceptance/test_f3_step3_coverage*.py`、`tests/integration/test_f3_step3_coverage_postgres.py` | D9 重跑场景 A：`docs/evidence/f3-step3-coverage/missing-source-rerun-<revision>/`（替身，不占 kind）：[已完成](../evidence/f3-step3-coverage/README.md) |
| 4 输入错误、连通失败、超时、无数据可区分 | PG：`test_step4_input_connectivity_timeout_and_no_data_are_distinguished` | PR-1 验收；`tests/acceptance/test_311_source_errors.py` | 无（复用） |
| 5 无关部署 + 依赖故障，按 rubric 检验竞争原因 | 判定器单元测试（门 1–4 各自失败、签名、探测）；冻结文件 `test_step5_frozen_rubric_answer_stimulus_are_unchanged`；R11 `test_scenario_index_lists_the_competing_cause_scenario`；PG：`test_step5_alert_follow_up_and_one_run_published`、`test_step5_follow_up_is_visible_in_the_first_model_round`、`test_step5_rubric_deterministic_gates_hold_on_the_product_path`、`test_step5_signature_is_on_the_cited_trace_evidence`；冻结：`test_step5_real_run_used_the_frozen_files_and_current_model_surface`、`test_step5_real_run_meets_rubric_gates_1_to_4`、`test_step5_two_independent_real_runs` | PR-3a 驱动与冻结 rubric/answer（`tests/acceptance/test_f3_step5_competing_scenario.py`） | `docs/evidence/f3-step5-competing-cause/run-{1,2}/summary.json`、[审查者结论](../evidence/f3-step5-competing-cause/run.md)（rubric 2/2）、[运行记录](../evidence/f3-acceptance-live/run.md)（LangSmith、余额差） |
| 6 调查不能改动目标环境 | 静态：`test_step6_registered_tools_are_read_only_verbs`；PG：`test_step6_tool_requests_are_read_only_gets_on_the_bound_target`、`test_step6_unregistered_write_tool_is_refused_without_an_outbound_request`；冻结：`test_step6_real_run_tool_requests_are_read_only_on_the_bound_target[run-1/2]`、`test_step6_authorization_probe_in_the_f3_window_denies_writes` | F7：[代理探测 2026-10-16](../evidence/f7-authz-probe/run.md)、`tests/acceptance/test_f7_step1_entry_points.py`、`tests/integration/test_runtime_roles_postgres.py` | R10 探测 [probe.json](../evidence/f3-step6-authz-probe/probe.json)（136/136） |

`IncidentOutcome.permissions` 是常量，任何测试都不以它为证据（U9-a）。

## main 上的状态（3c18bdb）

- 绿：判定器与静态测试 14 个；PG 产品路径 11 个（需 `M1_DURABLE_POSTGRES=1`，否则跳过）。
- （编写时在 main `3c18bdb` 上）红 14 个，均为预期缺口：场景索引缺 `F3:competing-cause`（R11）1 个；冻结证据未入库 13 个（缺文件即失败，不跳过）。PR-3b 入库证据、加索引后应全绿；任何一项仍红即该步未通过。

## 运行

```bash
OPSPILOT_LAB_DSN="host=127.0.0.1 port=<临时实例> dbname=m0_budget user=m0_lab" \
OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump M1_DURABLE_POSTGRES=1 \
.venv/bin/python -m pytest tests/acceptance/test_f3_step5_rubric_gates.py \
  tests/acceptance/test_f3_step5_real_runs.py tests/integration/test_f3_acceptance_postgres.py -q
```

PG 测试自建并删除临时库，不写共享 lab 库。这些是确定性合同与集成测试，不是真实模型或生产证明。
- PR-3b 证据入库并加 R11 索引后：本文件所列测试全绿（2026-10-24）。
