# M1-01 人工控制补全

状态：实现中，尚未完成测试与审查。

## 目标、范围与依据

按用户 2026-09-16 授权接续 PR #28 待决中的三项：全局/登记目标暂停持久化及领取、预算、请求、采纳栅栏；追问/纠正内容持久回读；每轮固定输入边界及已处理水位。依据 SPEC 有界 M1-01、PRODUCT-CONSTRAINTS、C3 第 4/5 节及 PRD/feature_list 的 F2/F12。close/reopen、目标重绑定、合并/拆分仍不实施；不改 passes 或验收步骤。

## 工作区与前提

- `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-control-completion`，`feature/m1-01-control-completion`，基于 `integration/m1-01`。
- 依赖 PR #20/#21/#26/#28/#29/#30（#27 经 #29 集成）。PR base 使用 integration/m1-01。
- 接手时仅 persistence.py 有前会话未提交修改（79 insertions / 11 deletions），已保留并接续；未移动用户 WIP。
- 用户授权 make setup、本地独占 PG、必要的有账本真实调用；本任务当前无需付费调用。
- 与并行 UI/acceptance 工作隔离，主要编辑 persistence.py；若 loop/adapter 需要接入，只增加输入快照边界。

## 完成条件

领域/持久化确定性检查、M1_DURABLE_POSTGRES=1 的暂停竞争集成、关键断言变异验证、make check、全新上下文独立审查并修复；提交推送 stacked PR，workflow_dispatch CI success，当前 HEAD code review 与 inline thread 闭环；不自动合并。

## 当前进展与证据

- 已核查 pwd/git status/git diff；make setup 成功，锁定 43 packages，39 packages audited。
- 半成品包含 scope/输入表与 Lease 代际，尚缺完整事务/输入快照与测试。
- 首次 make check：ruff check 通过，format check 因 persistence.py 格式失败；尚未运行后续检查，不计通过。
- 独立设计审查已启动；重点为锁序、暂停解除后显式恢复、输入快照与处理状态的分离。

## 下一步

完成 scope 锁与输入轮次合同，补领域/PG/loop 回归，运行变异与全套检查，审查后交付 PR。当前无需要用户裁决的新范围事项。

## 验证证据（2026-09-16）

- `.venv/bin/python -m scripts.m0.postgres_lab start` 成功启动本 worktree 专属 PG 17.9；完成后已 stop，数据保留。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_control_completion_postgres.py -q`：3 passed；覆盖目标暂停 claim、解除不自动恢复、follow-up payload 回读、全局暂停预算栅栏。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`：41 passed。
- `make check`：1444 passed, 98 skipped, 2 xfailed；ruff check/format、mypy 均通过。
- 设计独立审查（全新上下文）完成：指出统一 scope 锁序、冻结 round 输入边界、每次模型/工具请求发送前复核 lease、输入原文与模型投影边界；已采纳 scope lock、begin_round、assert_current 与 loop 接入。审查未运行测试，PG/确定性命令证据以上述实际输出为准。

## 未完成与交接

- 当前代码尚未提交、推送或创建 PR；需完成最终 diff 审核后提交 `feat: complete durable human control [M1-01]`，推送并创建 base=`integration/m1-01` 的 stacked PR。
- 需要 workflow_dispatch CI 覆盖当前 HEAD，等待 code review 并处理线程；不自动合并。
- close/reopen、目标重绑定、事故合并/拆分仍是用户待决，未实现；11 个 feature passes 未改。

### 最终 PG 复验（2026-09-17）

- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_control_completion_postgres.py -q`：3 passed。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q`：41 passed。
- 前述专属 PostgreSQL 已由 `postgres_lab start/stop` 启停，输出为 server started / server stopped；实例数据保留。

## 最终交付（2026-09-17）

- HEAD：`445020f`，后续提交未改写历史；提交标题中的 `[M1-01]` 保留为用户要求的已推送历史信息，新增提交已去掉该伪 feature id。
- PR：[ #31 ](https://github.com/kevinWangSheng/production-ops-agent/pull/31)，base=`integration/m1-01`。
- 最终 workflow_dispatch：[35175606617](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35175606617)：`checks` 与 `m0-postgres` 均 success。
- 最新机器人审查覆盖最终 HEAD；6 条及后续 4/3 条 inline thread 均已逐条回复并 resolve，当前未解决 thread 数为 0。采纳项包含 mandatory expected generation、actor 审计、输入 allowlist、暂停优先级与 new_run 重放。
- 用户审核/合并仍是最后一道门；本任务不自动合并。close/reopen、目标重绑定、合并/拆分仍待决。

## 后续复核（2026-09-17）：`claim()` suspension 恒假子句

- 来源：`integration/m1-01-full-r2` 集成报告（integ-final.md §7 第 1 条）标出 `persistence.py::claim()`
  suspension 判断第三个子句 `row.get("target_generation", 0) == 0 and row.get("target_suspended", False)`
  在其前一子句 `row["target_suspended"]` 同处一个 `or` 时恒为假，怀疑笔误。
- 判定依据：
  1. `DurableStore.transaction()` 用 `psycopg.connect(..., row_factory=dict_row)`（persistence.py:14,123），`row` 是普通 `dict`；
     `row["target_suspended"]` 与 `row.get("target_suspended", False)` 读的是同一个已存在的键，值必然相同。
     `or` 短路意味着只有前一子句为 `False` 时才会求值第三子句，而此时它的第二个合取项已经等于 `False`——
     无论 `target_generation` 取何值，第三子句都不可能为真，这是纯逻辑上不可达的死代码，不依赖任何具体状态。
  2. 即便忽略短路论证，`set_target_suspension`（persistence.py:372-428）在应用层也从不产生
     `target_generation == 0 且 target_suspended == True` 同时成立的状态：首次挂起总是把 generation 从
     0 跳到 1，与 `suspended=True` 在同一条 UPDATE 里一起提交（persistence.py:406-410）。
  3. 对照 C3 第 4 节「全局与目标级暂停」与 `SuspensionState.blocks()`（domain/control.py:66-71）：暂停判定
     的权威定义只有 `global_suspended or target in suspended_uids`，没有代际项；`claim()` 也没有为「本次
     claim 之前的某次尝试」保存过 target_generation 基线可供比较（`opspilot_runs` 建表 persistence.py:174-188
     未存这类列），因此第三子句不可能是「代际不匹配」检查的正确实现——没有可比较的第二个值。
  4. C3 要求的「解除暂停不自动恢复旧任务，需显式恢复」语义，已经由另一条独立、已生效的路径落实：
     `set_global_suspension`/`set_target_suspension` 挂起时把受影响的 `opspilot_incidents.state`/
     `opspilot_runs.state` 直接批量置为 `'paused'`（persistence.py:353-362, 411-423），`claim()`
     既有的 `incident_state in {"completed","cancelled","paused"}` 分支（persistence.py 原 593 行）据此拒绝
     领取，解除暂停不会把这个 `'paused'` 状态改回来。`tests/integration/test_m1_control_completion_postgres.py::
     test_scope_suspension_fences_claim_and_release_does_not_resume_old_run` 的第二个断言（release 后 claim 仍
     CONTROL_DENIED）验证的正是这条路径，与被删的第三子句无关（已用调试脚本逐行核对 row 状态确认）。
  5. 该子句由 `2c67910 fix: tighten control boundaries` 引入，提交信息无正文说明意图；同一提交里另一处改动
     （`new_run` 从「暂停即拒绝」改为「暂停即创建 paused 状态的新 Run」）是合理的语义演进，但 `claim()` 这处
     的第三子句与该改动无关联，也没有配套测试覆盖它的独有分支。
  - 结论：**冗余，非缺陷**——不是笔误漏写了别的字段，是死代码。已删除
    （`opspilot/persistence.py::claim()`，恢复为 `if row["global_suspended"] or row["target_suspended"]:`）。
- 验证：删除后 `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_control_completion_postgres.py -v`
  4 passed（含新增 `test_claim_allows_a_fresh_never_suspended_target_at_generation_zero`，证明
  `target_generation==0` 不单独触发拒绝）；`test_m1_durable_state_postgres.py` 41 passed（与修复前一致）；
  `make check`：1444 passed, 99 skipped, 2 xfailed，ruff check/format、mypy 通过（skipped 从 98→99 是新增
  PG 测试在非 PG 模式下多跳过 1 条，预期内）。PG lab 已 stop。
- 独立审查（全新上下文子代理，未参与本次改动讨论，自行重跑上述 PG/`make check` 并逐行核对代码与文档）：
  确认删除安全、C3「解除暂停不自动恢复旧任务」语义由独立路径落实、测试数字与文档一致；指出文档一处笔误
  （误写方法名 `DurableStore._connect`，实际是 `transaction()`），已按审查意见修正为
  `DurableStore.transaction()`。审查未发现需要变更代码的问题。

## 合并顺序提醒：`_call_model` 需要 #29 的 `reservation` 结算变量（rebase 待办）

- 来源：integ-final.md §8 第 4 条——**#31 应晚于 #29 合并**；#31 自己的 PR 分支（当前 HEAD，未 rebase）在
  `opspilot/investigation/loop.py::_call_model` 里没有 #29 引入的「按结算结果 settle 预算」机制：本分支只有
  具名内联的 `self._reserve(request.run_id, f"{logical_key}#a{attempt}")`，既没有 `reservation` 具名变量，
  也没有 `_settle` 方法（本仓库确认：`grep -n '_settle' opspilot/investigation/loop.py` 无命中）。
- 具体位置：`opspilot/investigation/loop.py` 的 `_call_model`（本分支约第 495-541 行，紧跟在
  `self._reserve(...)` 之后新增了本 PR 自己的 `self.store.assert_current()` 复核）。#29 分支
  （`origin/feature/m1-01-investigation-loop`）在同一函数里是：
  ```python
  reservation = f"{logical_key}#a{attempt}"
  self._reserve(request.run_id, reservation)
  ...
  except ModelError as exc:
      self._settle(request.run_id, reservation, "unknown")
      ...
  ...
  self._settle(request.run_id, reservation, "spent")
  if reply.response_model != self.accepted_response_model:
  ```
  正确的合并结果（已在 `integration/m1-01-full-r2` 的 `_call_model` 中手工核实，
  `git show origin/integration/m1-01-full-r2:opspilot/investigation/loop.py`）是**两者都要**：保留 #29 的具名
  `reservation` 变量与两处 `self._settle(...)` 调用，在 `self._reserve(...)` 之后追加本 PR 的
  `try: self.store.assert_current() except StepStoreError as exc: raise _halt_from_store(exc) from exc`。
- 待办：**#29 合并到 main 之后、#31 合并之前**，#31 owner 需要 `git rebase` 到已含 #29 的 main，并在
  `_call_model` 手工核对/合并出上述形态（若自动合并/rebase 丢弃了 `reservation` 变量或两处 `_settle` 调用，
  需要手工补回）；rebase 后重跑 `tests/test_m1_investigation_loop.py` 及本任务的 PG 定向测试确认预算结算未回退。
  本任务（#31）当前分支未做这处改动，因为它依赖 #29 的 `_settle`/`settle_budget`，不在本 PR 范围内。
