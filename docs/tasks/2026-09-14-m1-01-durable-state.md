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

- 当前本地/远端分支 `feature/m1-01-durable-state` 已推送；代码基线为 `0b7d095`，CI 配置提交为 `d5d7c4c`，后续仅为任务记录同步提交。工作区仅保留既有未跟踪 `.playwright-mcp/`。
- 本轮真实验证：`make check` 为 `1038 passed, 64 skipped, 2 xfailed`；mypy `Success: no issues found in 13 source files`；`M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m1_durable_state_postgres.py` 为 `10 passed`。
- 独立全新上下文审查已完成：自写 PG 探针实际验证 pause 后 `claim=CONTROL_DENIED`、`publish=False`、`follow_up/correct=ILLEGAL_TRANSITION`，resume 后 generation=2 且可重新 claim；探针 PASS。变异副本分别移除 claim paused、cancel paused、pause 写 queued、follow-up/correct paused guard，均被回归测试捕获（对应 1/3/1/1 failures）。
- 独立审查未发现本轮 5 个提交的 paused 行为缺陷，也未发现 mypy strict、Ruff TID251 或检查接入的偷工。审查指出的两项架构 xfail 与 `persistence.py` 的 `Any`/`cast` 是已知范围外债务；已明确拒绝在本修复 PR 扩大范围，保留原始 xfail 和未完成项。
- 机器人审查已手动触发覆盖 `0b7d095`，截至记录更新仍显示 running；此前 security review 返回额度不足，按项目规则不作为门槛。
- PR 描述已更新为本轮真实数字、红线修复、未完成项和风险。

## PR 交付状态

- PR #19：`https://github.com/kevinWangSheng/production-ops-agent/pull/19`
- 代码基线 `0b7d095`（任务记录提交后当前 HEAD 为 `d24b87d`）的 CI：`checks` 与 `m0-postgres` 均 SUCCESS；`m0-postgres` 尚未设置 `M1_DURABLE_POSTGRES=1`，故 CI 未执行 M1 的 10 条 PG 用例。
- 当前 HEAD 的机器人 code review 已手动触发但尚未返回；此前 security review 因额度不足不可用，按项目规则不作为门槛。
- 独立审查发现：架构两项 xfail 与 `Any`/`cast` 类型债务均为已知范围外项；明确拒绝本 PR 扩大范围，保留并记录为未完成项。
- 未合并，待 CI 缺口决定、机器人状态收敛和用户审核。

## 交付规范更新

项目交付规则已补充：PR 必须实际处于可合并状态；每条 code review 必须明确采纳并修复或拒绝并说明依据；采纳后重新验证并取得覆盖当前 HEAD 的复审，拒绝后回复并 resolve；状态仅在这些条件满足后流转为“PR 已就绪，待用户审核合并”。

## P1 thread 处置与最终复验（2026-09-15）

- 机器人审查曾提出 5 条 P1：`waiting_human`/`blocked` 控制闭合、queued follow-up/correct generation 重绑定、claim incident generation 同名列覆盖、publish final step generation fence、commit_tool step generation fence。
- 5 条均已采纳并修复，新增 PG 回归测试；每条均已在 PR 回复“采纳并修复”并 resolve。
- 独立复审随后发现第 6 条 P1：`blocked` Run 上除 cancel 外的控制会先改 incident 状态造成不一致。已在 `d1bc5dc` 修复为 `ILLEGAL_TRANSITION` fail-closed，并新增回归断言；该 finding 已由独立复验确认关闭。
- 当前代码复验：M1 PG `13 passed`；blocked 的 pause/resume/follow_up/correct 均拒绝、cancel 正确完成；waiting_human 流程和 step-generation 变异均通过。
- 最新 CI（修复提交前一轮代码变更）checks 与 m0-postgres 均 SUCCESS；m0-postgres 已启用 M1 PG。`d1bc5dc` 推送后等待对应新一轮 CI。
- 最新机器人普通 code review 已覆盖此前文档同步 HEAD 且无新 finding；`d1bc5dc` 为后续代码修复，已重新手动触发审查。security review 仍因额度不足，不作为门槛。

## 独立审查（2026-09-15，全新上下文 agent）与合并前收尾

审查对象为 `1993f6f`、`d1bc5dc` 两个代码提交，结论为可合并、无阻塞项。逐条核对：

- 变异矩阵 9 项中 4 项单独回退不被任何测试捕获，全部来自 `1993f6f`：claim 列别名、
  commit_tool step 代际栅栏、resume 状态集、follow_up/correct 状态集。
- 其中 commit_tool 的 step 代际栅栏（`opspilot/persistence.py:300`）经复现确认承重：
  删除后新租约可把工具结果写入追问前的旧代际步骤，使过期证据被标记为
  `tool_result_committed`；该路径经 `rebuild()['pending_tools']` 公开可达。
  原有测试用旧租约，被守卫中更靠前的 owner/epoch 检查短路，属假覆盖。
  已在 `bde7017` 补回归测试，变异验证：删除该行后仅新测试失败，还原后 14 passed。
- claim 列别名修复的可达性经复现确认：`1993f6f` 之前，对 queued Run 追问会使
  incident 与 run 代际分叉（1 对 0），claim 因同名列取到 run 的 0，发出的租约
  代际陈旧且后续写入必被拒。属真实可达缺陷，非仅合同正确性。
- 测试未被弱化：`git diff 1993f6f~1 d1bc5dc -- tests/` 无删除行，65 insertions / 0 deletions。

## 未完成项（本 PR 范围外，另开修复任务）

以下为既存生产代码缺陷，均非本 PR 引入，已复现并记录，不在本次修复范围：

1. `rebuild()` 撕裂快照：三条 SELECT 无 `FOR UPDATE`，隔离级别 read committed，
   逐语句取快照。实测并发下 800 次读取中 44 次 incident 与 run 的
   `control_generation` 不一致（5.5%）。与 C3 第 7 节「PostgreSQL 业务记录是唯一
   跨进程恢复依据」冲突。
2. ABBA 死锁：`claim`/`control`/`publish` 按 incidents→runs 加锁，
   `reserve_budget`/`commit_step`/`commit_tool` 按 runs→incidents 加锁。
   实测强制交错可触发 `DeadlockDetected`。非 `d1bc5dc` 引入，改动前后一致。
3. 幂等接口在并发下报存储故障：同一身份并发 `accept()` 实测返回
   `STORAGE_UNAVAILABLE`，实际操作已成功、存储正常。
4. `CONTROL_CONFLICT` 语义收窄：`d1bc5dc` 的 JOIN 使「incident 存在但 run 行缺失」
   也落入该码，调用方按「重读重试」处理会无限循环。当前不可达。
5. `control()` 只守 `blocked`，未守 `failed`/`budget_exhausted`；两者在
   `RUN_EXECUTION` 中同为终态。当前不可达，但与 `waiting_human` 的前瞻处理不对称。
6. 租约守卫复制 4 份且语义已分叉：`publish` 一份将 `lease_until IS NULL` 视为拒绝，
   另外三份视为通过。
7. 错误码压平：`_require_row` 与 `transaction()` 同用 `STORAGE_UNAVAILABLE`，
   前者表示数据不一致（重试必然再失败），后者表示瞬时故障（可重试）。
   第 2、3 条的表象由此造成。

第 1、2、3、7 条同根（读路径无锁 + 错误码压平），建议合并修复。
第 4、5、6 条可单独排期。
