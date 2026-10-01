# M1-01 后续项：被拒工具调用列入 Run 覆盖摘要

- 状态：已完成，提交 `0f84c4a`
- 独立测试：`tests/test_m1_coverage_refusals_contract.py`；提交号 `0d6648b`；红 3（新摘要拒绝类、无拒绝 `none` 类、恢复摘要哈希路径），绿 0。
- 更新日期：2026-10-01
- 依据：[整视图计量记录](2026-09-29-m1-01-view-bytes-timeout.md)「决策点（留给用户）」；用户 2026-10-01 决定做
- 工作区：`feature/m1-01-coverage-refusals`，`../production-ops-agent-coverage-refusals`

## 状态

- 已实现：覆盖摘要按调用顺序加入无交付视图的网关拒绝/错误 `<tool> (<reason>)`，空类为 `none`；拒绝记录只取结构化字段，live/rebuild 共用筛选并纳入最终输入哈希重建。
- 验证：`.venv/bin/python -m pytest tests/test_m1_coverage_refusals_contract.py -q` → 3 passed；`.venv/bin/python -m pytest tests/test_m1_investigation_loop.py -q` → 82 passed。
- 真实 Run：OTel Demo `otel-demo` 宽查询一次，Run `7cb82d94-d7af-5bab-8959-4925d6068f04`，8 次模型请求、19 次工具调用、3 次 `traces_search (RESULT_TOO_LARGE)`；完成报告为 `completed/partial`，gaps 点名三次拒绝。证据：[run](../evidence/m1-01-coverage-refusals/run.md)、[ledger](../evidence/m1-01-coverage-refusals/ledger.json)。
- gitleaks：本地未安装 `tmp/gitleaks/gitleaks`，未扫描；证据记录已注明。
- 既有测试后果：`tests/test_m1_view_explicit_contract.py` 的 5 个 `none` 计数各增加一类；这是合同新增拒绝类的直接文字后果，不削弱原有 incomplete/truncated/non-ok 断言。
- 最终检查：`make check` → 2504 passed, 269 skipped, 2 xfailed；ruff、format、mypy 均通过。

## 问题


（合同 1–5 与验收口径原文见本分支历史版本；实现与独立测试在未推送分支 `feature/m1-01-coverage-refusals` 的 `adf29fd`、`0d6648b`，不合入。）

## 发现：覆盖摘要在模型自行停止的 Run 中不生效

- 摘要只随「强制最终报告轮」发送（`opspilot/investigation/loop.py` 的 final 轮，恢复侧 `context.py` 同一条件）。#59 去掉每 Run 预算上限后，模型通常自行停止（`finish_reason: stop`），没有最终报告轮；只有报告校验失败触发的强制重试轮才会附摘要。
- 证据（各 ledger 中 `context.final` 为 true 的请求数）：`m1-01-alignment-c-effect` 6 个 Run 中 2 个；`m1-01-view-tokens-effect` 6 个 Run 中 2 个（normal-1、fault-2 为 0，正是本项立项所依据的两份报告）；#59 之前的 `m1-01-window-points-rerun` 4/4（预算强制）。
- 本项实现的真实 Run（8 次请求，均 `final=false`）报告 gaps 点名了 3 次 `traces_search (RESULT_TOO_LARGE)`，来源是工具结果里的拒绝说明，而非摘要。
- 独立审查（Opus）另指出：工具名解析前就被网关拒绝的调用（`TOOL_NOT_REGISTERED`、`TOOL_NOT_IN_SCOPE`、注册表变更）视图里 `tool` 为 null，会被漏列。

## 用户决定（2026-10-01）

放弃本项，对齐上游：上游 HolmesGPT 没有覆盖摘要，拒绝原因随工具结果返回模型。原有覆盖摘要（incomplete/truncated/非 ok）在自行停止的 Run 中同样不生效，记为后续改进，不在本项处理。
