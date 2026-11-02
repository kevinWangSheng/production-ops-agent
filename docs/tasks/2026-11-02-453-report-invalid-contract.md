# #453 报告校验退回提示

- 状态：PR #454；独立审查 APPROVE；真实 Run 完成；待机器人分诊与用户 Q6 裁决
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
- 不为提示文本变化提升 `prompt_revision`：`REPORT_RETRY_TEMPLATE` 模板字节未变，变的只是实例细节（C3 §5 规则 2「哈希覆盖模板字节，不覆盖实例字节」）。影响面：升级时停在强制最终轮、上一轮回复是带围栏的报告，且去围栏后解析失败或 schema 不合法的 Run，重建时 #304 哈希比对不一致，按既有路径以 `INCOMPATIBLE_STATE` 失败关闭（C3 §7）。去围栏后合法的报告提示与 main 逐字节相同，不受影响。窗口很窄，结果是设计内的阻断路径。
- 去围栏用线性字符串切片，不用正则：首版正则在未闭合围栏加大量空白时为立方复杂度（审查实测 5000 空格 28 s），而该路径在 live、resume 与 #304 重建都会执行。

## 证据

- 回归测试 `tests/test_453_report_retry_feedback.py`（14 项）：首批 5 项在 main 上 2 项失败；新增围栏变体 5 项与退化空白耗时 4 项（每项 <0.5 s；首版正则 2000 空格实测 1.88 s）。
- 独立审查 r1（全新上下文 Fable 5.1，Codex 满载）：CHANGES_REQUIRED 1 项（正则回溯），已修；其余可选项中窗口描述已更正。
- 定向测试 244 passed（loop、discipline、pairing、alignment C、citations、#310、#304、loop limits）；ruff、format、mypy 通过。
- `make check` 通过：10750 passed、2511 skipped（PG 需显式开启）、2 xfailed，用时 38m48s。
- 有界真实 Run（ADR-0006）：`9d4f368e`，2026-11-02 #432 首个 kind 窗口，健康 lab、不注入故障，问题同 dv2-2b214b；首轮即发布报告（completed），未触发重试路径——本 Run 证明改动后的 loop 端到端正常，退回提示行为由 89459277 离线重放与单测证明。trace（LangSmith `opspilot-lab-f453`，已回读）：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/562b41d4-4320-4896-be8b-506d51a4fbda/r/00000000-0000-0000-8300-98cfd025d78e?poll=true；冻结摘要 `docs/evidence/453-report-retry-feedback/live-run/9d4f368e-1c3c-4f05-9485-572b3bd2f2a5/summary.json`；费用上限 ¥1.04（summary `known_cost_cny_upper`）。
- 独立审查 r2：APPROVE（9c037744；1 MB 退化输入最坏 0.5 ms；parse_report 与 main 一致）。

## 待决

- 第 87 行文案歧义：另开 issue。
