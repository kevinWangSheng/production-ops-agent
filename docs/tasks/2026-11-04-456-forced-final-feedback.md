# #456 强制收尾退回提示：散文包裹的围栏与引用错误

- 状态：进行中（PR 待独立复验）
- 更新日期：2026-11-04
- 依据：GitHub issue #456（#453/#454 后续）；用户裁决 F1 收尾 U5(a)(d)（2026-11-04）；C3 §5 revision 规则 2；C2（[对齐 C](2026-09-28-m1-01-alignment-c.md)）；前序 [#453 记录](2026-11-02-453-report-invalid-contract.md)
- 工作区：`feature/F3-456-forced-final-feedback` / `/Users/shenghuikevin/dev/AI/poa-456-impl`
- 合同预审：`~/.agent/handoffs/opspilot-prereview-456-2026-11-04.md`（需用户裁决 0 条，D1–D7 可逆默认）

## 目标与范围

模型把报告写成「一句散文 + ```json 围栏」，或围栏里的 claims 引用了 operation_id 而不是 evidence_id 时，唯一一次强制重试只看到外层「JSON parse error」，不知道真正的问题。#432 探针 `a26613d9` 正是这样失败的。本次只让退回提示多说两件事：

1. 回复不是整条围栏时，找到其中第一段围栏，报告它内部的 JSON 或 schema 错误；
2. 围栏内容通过 schema 时，再按真正引用门的同一组入参跑一遍引用检查，逐条列出失败的 claim 与 evidence_id（C2 封闭词表，最多 50 条加截断说明）。

不变：`parse_report` 的接受结果（带围栏的回复仍是 `REPORT_INVALID`）、`REPORT_RETRY_TEMPLATE` 模板字节、`prompt_revision`、只有一次强制重试。围栏内容全部合法时提示与 main 逐字节相同。

## 合同（r0，预审决定表，均为可逆默认）

| 编号 | 要点 | 出处 |
|---|---|---|
| D1 | 散文里取第一个 ``` 到下一个 ``` 之间的内容，两次 `str.find`，不用正则 | 预审 D1 |
| D2 | 引用明细加层名前缀：`After removing the Markdown json fence, citation check:` / `In the Markdown json fence found inside the reply, citation check:`；只在有失败时出现 | 预审 D2 推荐 b |
| D3 | 不提升 `prompt_revision`（模板字节未变）。代价：停在强制收尾、上一轮回复是带围栏报告且围栏内 schema 不合法/JSON 坏/引用失败的旧 Run，重建时按 #304 哈希比对以 `INCOMPATIBLE_STATE` 失败关闭（C3 §7 设计内路径） | 预审 D3，同 #453 先例 |
| D4 | F3 等价证明由非实现者在最终代码上复跑，结果写 `docs/evidence/f3-step5-competing-cause/456-feedback-equivalence.json` | 预审 D4 |
| D5 | 不加「你可能想引用 X」提示，不扩 C2 词表 | 预审 D5 |
| D6 | 实现者写回归单测；独立验收测试由未参与实现的 Agent 在 `chore/456-acceptance-tests` 写 | 预审 D6 |
| D7 | `_json_parse_position_detail` 加关键字参数 `views`/`citation_kwargs`，只在内部调用 | 预审 D7 |

## 证据

- 回归单测 `tests/test_456_forced_final_feedback.py`（15 项）：未改代码时 6 项失败、9 项（不变量）通过；实现后全部通过。覆盖散文围栏 + 引用错、整条围栏 + 引用错、全合法时与裸解析错误逐字节相同、散文围栏内 JSON 坏 / schema 错、未闭合或无围栏、未加围栏的引用错不变、50 条封顶、多段围栏取第一段、1 MB 级退化输入 < 0.5 s。
- 定向测试 316 passed、2 xfailed（#456、#453、对齐 C、#304 验收与实现、F3 第 5 步两份证据型验收、#310、架构、引用合同、loop、loop limits、pairing、instruction discipline）；ruff、format、mypy 通过。按 lead 指示本机 swap 紧张未跑全量 `make check`，由 CI 覆盖。
- F3 第 5 步重建（实现者自跑，非独立复验）：用预审脚本在最终代码上对 `f3-3b-run1`、`f3-3b-run2` 做 `rebuild_transcript`，两者都成功；负对照（反馈末尾多 1 字节）run-2 变为 `INCOMPATIBLE_STATE`、run-1 仍成功。独立复验与 D4 证据文件由 lead 另派。
- 有界真实 Run（ADR-0006）：`50458170`，2026-11-04 07:34–07:38 PT，本机 kind lab 健康、不注入故障，DeepSeek Flash，问题同 `dv2-2b214b`；12 次模型请求，第一次尝试即发布报告（completed），`offline_rebuild` 成功，trace 已回读。本 Run 未触发重试路径，证明的是改动后 loop 端到端正常；退回提示行为由单测、预审的 `a26613d9` 回放与 F3 run-2 重建证明（同 #453 做法）。trace（LangSmith `opspilot-lab-f456`）：https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/77baee16-4f8f-4c41-bdce-9497e5c4590c/r/00000000-0000-0000-1b0e-ac2e1e4fa9df?poll=true；冻结摘要 [`summary.json`](../evidence/456-forced-final-feedback/live-run/50458170-e092-498c-9e30-8d673948e9cf/summary.json)；费用上限 ¥1.30（summary `known_cost_cny_upper`）。首次启动因 lab PG 未迁移在 `install()` 失败（未发模型请求），`make migrate` 后重跑。

## 待决

- 独立复验（F3 重建 + 代码审查）与独立验收测试 cherry-pick 后的结果。
