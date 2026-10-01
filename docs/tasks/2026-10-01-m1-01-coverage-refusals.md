# M1-01 后续项：被拒工具调用列入 Run 覆盖摘要

- 状态：已取消（用户 2026-10-01 决定放弃，记录发现；实现 `adf29fd` 与独立测试 `0d6648b` 留在未推送分支 `feature/m1-01-coverage-refusals`，不合入）
- 更新日期：2026-10-01
- 依据：[整视图计量记录](2026-09-29-m1-01-view-bytes-timeout.md)「决策点（留给用户）」
- 证据：本项实现分支上的一次真实 Run，[run.md](../evidence/m1-01-coverage-refusals/run.md)、[ledger.json](../evidence/m1-01-coverage-refusals/ledger.json)、[report.json](../evidence/m1-01-coverage-refusals/report.json)

## 问题


（合同 1–5 与验收口径原文见本分支历史版本；实现与独立测试在未推送分支 `feature/m1-01-coverage-refusals` 的 `adf29fd`、`0d6648b`，不合入。）

## 发现：覆盖摘要在模型自行停止的 Run 中不生效

- 摘要只随「强制最终报告轮」发送（`opspilot/investigation/loop.py` 的 final 轮，恢复侧 `context.py` 同一条件）。#59 去掉每 Run 预算上限后，模型通常自行停止（`finish_reason: stop`），没有最终报告轮；只有报告校验失败触发的强制重试轮才会附摘要。
- 证据（各 ledger 中 `context.final` 为 true 的请求数）：`m1-01-alignment-c-effect` 6 个 Run 中 2 个；`m1-01-view-tokens-effect` 6 个 Run 中 2 个（normal-1、fault-2 为 0，正是本项立项所依据的两份报告）；#59 之前的 `m1-01-window-points-rerun` 4/4（预算强制）。
- 本项实现的真实 Run（8 次请求，均 `final=false`）报告 gaps 点名了 3 次 `traces_search (RESULT_TOO_LARGE)`，来源是工具结果里的拒绝说明，而非摘要。
- 独立审查（Opus）另指出：工具名解析前就被网关拒绝的调用（`TOOL_NOT_REGISTERED`、`TOOL_NOT_IN_SCOPE`、注册表变更）视图里 `tool` 为 null，会被漏列。

## 用户决定（2026-10-01）

放弃本项，对齐上游：上游 HolmesGPT 没有覆盖摘要，拒绝原因随工具结果返回模型。原有覆盖摘要（incomplete/truncated/非 ok）在自行停止的 Run 中同样不生效，记为后续改进，不在本项处理。
