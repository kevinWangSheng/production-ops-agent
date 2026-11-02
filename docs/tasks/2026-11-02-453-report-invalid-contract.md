# #453 报告校验退回提示

- 状态：实现完成，待 PR、独立审查与一次有界真实 Run
- 更新日期：2026-11-02
- 工作区：`fix/453-report-invalid-feedback` / `/Users/shenghuikevin/dev/AI/production-ops-agent-453`
- 交接：`~/.agent/handoffs/opspilot-453-report-invalid-handoff-2026-11-02.md`

## 诊断（离线，已复核）

Run `89459277` 第 10 轮把报告包在 ```json 围栏里，`parse_report` 以 JSON 解析失败判 `REPORT_INVALID`，退回提示只写「JSON parse error: Expecting value: line 1 column 1」。第 11 轮（强制最终重试）去掉了围栏，内容几乎不变，但违反 `reports.py:140` 的规则：`assessment_status=incomplete` 必须配 `conclusion=inconclusive` 且 `gaps` 非空，该 Run 写的是 `partial`。第 10 轮去围栏后同样违反这一条。校验器符合自身合同，没有误判；根因是解析层与 schema 层逐层报错，唯一一次重试只看到第一层。

复核方法：归档 PG（`~/.agent/archive/poa-f14-trace/tmp/f14tr/pg` 的副本，端口 56411，用后已停）取出该 Run 第 9、10 步的 `assistant.content`，用 main 与本分支的 `parse_report` / `report_retry_feedback` 重放。本分支对第 10 轮的提示为「JSON parse error … report: schema_error (Value error, INCOMPLETE_REPORT_CONTRACT)」，接受结果不变。

## 合同

| 编号 | 要点 | 出处 |
|---|---|---|
| U1 | `incomplete + partial` 保持非法，`reports.py:140` 不改 | 用户 2026-11-02（交接 R1） |
| U2 | 报告失败仍只有一次最终重试，不改预算与状态转移 | 现行合同 #95 / `loop.py` `force_final`；本 PR 不动。是否另有用户裁决未确认 |
| U3 | 去围栏后的内容只用于生成提示，`parse_report` 的接受结果不变 | Agent 可逆默认 |

## 决定（Agent 自定，可逆）

- 不改 `REPORT_CONTRACT` 文案。原 WIP 改了第 87 行的歧义句，但 `tests/acceptance/test_310_investigation_method.py` 钉了该文案的 SHA，改文案就要改验收钉值；规则在 `reports.py:70-71` 已写明。歧义句另开 issue。完整 WIP 补丁存 `~/.agent/archive/453-wip-full-2026-11-02.patch`。
- 去围栏后 schema 合法时仍返回解析错误（原 WIP 在此处返回空串，提示比 main 还少；新测试发现后修正）。
- 不为提示文本变化提升 `prompt_revision`。影响面：升级时恰好停在强制最终轮、且上一轮是带围栏且 JSON 合法的报告的 Run，重建时 #304 哈希比对不一致，按既有路径以 `INCOMPATIBLE_STATE` 失败关闭。窗口很窄，结果是设计内的阻断路径。

## 证据

- 回归测试 `tests/test_453_report_retry_feedback.py`（5 项）：main 代码上 2 项失败，本分支全过。
- 定向测试 244 passed（loop、discipline、pairing、alignment C、citations、#310、#304、loop limits）；ruff、format、mypy 通过。
- `make check` 通过：10750 passed、2511 skipped（PG 需显式开启）、2 xfailed，用时 38m48s。
- 真实 Run：待 #432 第一个 kind 窗口。

## 待决

- 第 87 行文案歧义：另开 issue。
