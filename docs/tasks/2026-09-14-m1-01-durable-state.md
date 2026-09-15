# M1-01 持久化与恢复

- 目标：完成 PG 业务状态/断点、人工追问纠正取消、重启与不兼容处理。
- 依据：`SPEC.md` 实施门槛/Operating constraints/Verification and delivery；`PRODUCT-CONSTRAINTS.md`；`docs/design/technical-proposal-2026-09-07.md` 第 4、5、6 节；`docs/evidence/m0-real-investigation/m0-exit-matrix.md` 的 M1-01 拆分。
- 范围：`opspilot/persistence.py` 与确定性测试；不修改 `scripts/`、`feature_list.json`、SPEC 门槛或并行分支。
- 当前进展：已建立 feature 分支并实现 PG 业务状态、租约/断点、原子预算、控制代际、迟到结果历史化与版本不兼容阻塞接口。
- 完成条件：四项 M1-01 持久化与恢复条件均由 PostgreSQL 集成测试覆盖；`make check` 通过；独立审查与 CI/code review 待完成。
- 实验/费用：本任务不调用模型或外部服务；仅使用本地 PostgreSQL lab，未知费用为 0。
- 未完成：测试、独立审查、提交/推送/PR、CI 与 code review 处置。

## 验证记录

- `make check`：1036 passed，56 skipped；Ruff 与格式检查通过。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m1_durable_state_postgres.py`：2 passed。
- 本地 PostgreSQL lab 已启动于 `scripts/m0/postgres_lab.py start`；无模型/trace/外部付费调用。
- 独立审查：首轮 P1 已修复；当前 HEAD 的独立复审已完成，无本轮行为缺陷。

## PR #19 review 修复

- 审查接受并修复：过期 lease、Run deadline、完成/取消 incident 的 claim 与 publish fencing；暂停/恢复的 owner、lease 和 control generation 同步；终态人工操作拒绝。
- 审查意见中关于 security review 的额度提示不属于代码 finding；未作为通过条件。
- 修复后 PG 定向测试 5 passed，`make check` 1036 passed / 59 skipped；待新提交 CI 与复审。
- 追加人工追问/纠正回归：PG 定向测试 6 passed，覆盖 `follow_up` / `correct` generation 递增与旧 lease 拒绝。

## 当前交付更新（2026-09-15）

- 当前本地/远端 HEAD：`d24b87d`（代码基线 `0b7d095`，`feature/m1-01-durable-state`），已推送；工作区仅保留既有未跟踪 `.playwright-mcp/`。
- 本轮真实验证：`make check` 为 `1038 passed, 64 skipped, 2 xfailed`；mypy `Success: no issues found in 13 source files`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m1_durable_state_postgres.py` 为 `10 passed`。
- 独立全新上下文审查已完成：自写 PG 探针实际验证 pause 后 `claim=CONTROL_DENIED`、`publish=False`、`follow_up/correct=ILLEGAL_TRANSITION`，resume 后 generation=2 且可重新 claim；探针 PASS。变异副本分别移除 claim paused、cancel paused、pause 写 queued、follow-up/correct paused guard，均被回归测试捕获（对应 1/3/1/1 failures）。
- 独立审查未发现本轮 5 个提交的 paused 行为缺陷，也未发现 mypy strict、Ruff TID251 或检查接入的偷工。审查指出的两项架构 xfail 与 `persistence.py` 的 `Any`/`cast` 是已知范围外债务；已明确拒绝在本修复 PR 扩大范围，保留原始 xfail 和未完成项。
- 机器人审查已手动触发覆盖 `0b7d095`，截至记录更新仍显示 running；此前 security review 返回额度不足，按项目规则不作为门槛。
- PR 描述已更新为本轮真实数字、红线修复、未完成项和风险。

## PR 交付状态

- PR #19：`https://github.com/kevinWangSheng/production-ops-agent/pull/19`
- 代码基线 `0b7d095`（任务记录提交后当前 HEAD 为 `d24b87d`）的 CI：`checks` 与 `m0-postgres` 均 SUCCESS；`m0-postgres` 尚未设置 `M1_DURABLE_POSTGRES=1`，故 CI 未执行 M1 的 10 条 PG 用例。
- 当前代码基线的机器人 code review 已手动触发但尚未返回；此前 security review 因额度不足不可用，按项目规则不作为门槛。
- 独立审查发现：架构两项 xfail 与 `Any`/`cast` 类型债务均为已知范围外项；明确拒绝本 PR 扩大范围，保留并记录为未完成项。
- 未合并，待 CI 缺口决定、机器人状态收敛和用户审核。

## 交付规范更新

项目交付规则已补充：PR 必须实际处于可合并状态；每条 code review 必须明确采纳并修复或拒绝并说明依据；采纳后重新验证并取得覆盖当前 HEAD 的复审，拒绝后回复并 resolve；状态仅在这些条件满足后流转为“PR 已就绪，待用户审核合并”。
