# M1-03 计划第 4 步真实验收（D32、D24；合同 r6）

- 日期：2026-10-10 04:05–04:11Z；分支 `feature/F13-acceptance`（合入 main 74bed61，含 D35 修复 #178）；驱动 [`scripts/m1_03_review_live.py`](../../../scripts/m1_03_review_live.py)。
- 冻结摘要：[live-runs/e9822e88-9c45-4095-8523-bb54312d4bfe/summary.json](live-runs/e9822e88-9c45-4095-8523-bb54312d4bfe/summary.json)；原始 ledger 不入库（sha256 与字节数在摘要内，ADR-0006）。
- **测试驱动模拟审核**：审核动作由驱动以专用实验 Basic Auth 账号经工作台 HTTP 表单提交（D33），不是真人审阅；不是生产证明。
- **真实链路批准不可达，见 [#181](https://github.com/kevinWangSheng/production-ops-agent/issues/181)**：本次 8 个版本（加干跑 4 个）全部带模型自提争议，按 D16 不能批准；因此批准、发布、替换、撤销没有真实证据（用户 2026-10-09 裁决：本 PR 照实记录，后续改生成提示词的 disputes 定义另开 PR）。替换不可达的另一原因见 [#179](https://github.com/kevinWangSheng/production-ops-agent/issues/179)，本次未走到该判断。

## 环境

- 数据：F6 归档库 `f6live` 的副本（原件 `tmp/f6-passes-worktree/pg55681-archive` 不动），复制到本 worktree `tmp/pg55762-f6copy`（端口 55762），无运行时连接时 `python -m opspilot.schema migrate`：`0007_ending_job_identity` → `0009_postmortem_generation`。事故 `0834af7e-0951-5850-a409-b1550255ee0a`（已确认恢复，调查 Run 仍 queued）。
- 进程：真实 `python -m opspilot.web serve`（同一副本，实验账号）；生成由驱动调用产品 `PostmortemWorker.generate` 与真实 `deepseek-flash`，未启动调查 worker（R12），归档中的 queued Run 未被领取。
- trace：`OPSPILOT_TRACE=lab`，round `m1-03-review-20261009`（project `opspilot-lab-m1-03-review-20261009`）。运行前在同一入口核实 fail-closed：非白名单 `LANGSMITH_ENDPOINT` → `lab target check failed: LANGSMITH_ENDPOINT_NOT_ALLOWED`；缺 `--lab-round` → `OPSPILOT_TRACE=lab requires --lab-round`；两次都在写库前退出，之后生成尝试行数 0。

## 结果

| 判据 | 结果 |
|---|---|
| F13-1 生成 | 8 次生成全部 `succeeded`，版本进入 `under_review`，引用全部有效；8 条 LangSmith trace 均回读 `found`，如 [v1](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/30cc2335-3662-45df-b1c8-a641d9cfa147/r/00000000-0000-0000-fbb5-1d58f7ee23eb?poll=true)（其余链接见摘要 `generations[*].trace.url`） |
| R9 计数 | 模型请求驱动计数 9 = 持久尝试记录 9；每次生成 ≤ 2 次请求 |
| F13-2 争议不成为知识 | 5 次批准有争议版本经 HTTP 被拒 422 `DISPUTED`，复盘代际不变；整个运行没有任何知识 revision |
| F13-3 退回/驳回 | 退回 → worker 带理由生成 `revises_version` 新版本（5 次）；驳回 → `rejected`；经 HTTP `follow_up` 合法改变水位，旧版本 `stale` |
| F13-3 批准/发布/替换/撤销 | **未达成**：每个版本都有争议（#181） |
| D34/R14 页面一致 | 23 个步骤均在稳定代际边界内用独立测试作者的 `assert_pages` 逐字段比较，全部一致 |
| D35 证据链接 | 版本引用的 61 个证据 ID 全部经 `/incidents/{id}/evidence/{evidence_id}` 解析为存储的恢复读数，信号名、时间窗与目录一致，哈希未失败（本事故无调查证据） |

驱动判定的失败（不为通过而改判定）：`approve_v1:HTTP_422`、`approve_v1:STATE_under_review`、`APPROVAL_PUBLISHED_NOTHING_RETRIEVABLE`、`V4_STILL_DISPUTED`、`NOTHING_ACTIVE_TO_REVOKE`，均源于 #181。

## 费用

余额 5.23 → 5.05 CNY（实耗 0.18 CNY）；按 token 计价上界 1.35 CNY（272834 输入 / 86105 输出）。此前干跑（另一副本 `f6dry`，trace 关闭，不入库）上界 0.74 CNY，余额两位小数内无变化。
