# 真实 deepseek-flash Run 记录说明

## 2026-09-17 UTC 前两次 Run（`real-run-ledger.json`、`real-run-ledger-2.json`）

两次都以 `REPORT_INVALID` 结束。根因不在模型也不在 loop 校验器：

- 当时 `scripts/m1_live_flash_loop.py` 传给 loop 的 `evidence_context.time_policies`
  只有 `{"id": "policy-window-1"}`，没有 `mode`/`window`。
- `opspilot/investigation/reports.py` 的 `eligible_time_policies` 对没有 `mode` 的策略直接跳过，
  所以工具返回的 view 不携带任何 `time_scope_refs`；模型按提示词把 fact 类 claim 的
  `time_scope_ref` 填成上下文里唯一的 `policy-window-1`，在 `unsupported_citations`
  （`reports.py:344`）处被 fail-closed 拒绝，loop 记为 `REPORT_INVALID`。
- 第二次 Run 的报告 `real-run-report-2.json` 本身可解析、schema 为 `m0-report-v2`、
  引用正确；离线重放（`tests/acceptance/test_m1_live_flash_replay.py`）证明：
  在修复后的上下文下它是 `completed`、无 handoff、`report_available`。
- 第一次 Run 的报告 `real-run-report.json` 另有一处模型侧问题：正文 JSON 之后多了一行
  `{"type":"json_object"}`，`json.loads` 报 Extra data，按合同（只返回一个 json 对象）应拒绝。
  去掉尾巴后它是一份合法的 `incomplete/inconclusive` 报告。

两份原始记录保留不改；账本中的 `handoff_reasons` 仍是当时的真实结果。

## 修复后 Run（`live-runs/<run_id>/`）

| run_id | execution | handoff_reasons | report | known_cost_cny_upper |
|---|---|---|---|---|
| `40b9705a-0ef7-4fe9-a50c-1a9f04e103c3` | completed | `INCOMPLETE_INVESTIGATION` | v2，模型自判 incomplete | 0.025918 |
| `bbf10e0e-0b0e-489c-9efd-98b80ef4aa3b` | completed | 无 | v2，`completed/partial`，`decision=report_available` | 0.02658 |

均为 2 HTTP、`prompt_revision=prompt-replay-candidate-017c81744c26`、fixture Prometheus 数据、
只读权限；这证明「真实调查产出可通过 v4 绑定的报告」这条纵向链路在集成分支上可用，
不等于 F3 全部验收步骤通过，也不改变 `feature_list.json` 的 `passes`。
