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

## 追加修复（2026-09-17 第二轮）：输入白名单投影 + `new_run` 状态一致性

来源：`digest3.md`「PR #31」§5 第 1 条 + PR #31 上一条未 resolve 的机器人 review thread。

### 1. `begin_round` 送入模型的追问/事件内容改为白名单投影

- 问题：`opspilot/investigation/loop.py::_round` 把 `opspilot_inputs.content`（追问/纠正原文、事件，调用方
  自由传入、无 schema 约束）注入模型 prompt 前，原过滤逻辑是黑名单——只按 4 个固定顶层键
  （`password`/`secret`/`token`/`authorization`）做大小写不敏感精确匹配，既不过滤嵌套字段，也不认识
  `api_key`/`Api-Key` 这类常见凭据键名变体，构造 `{"nested": {"api_key": "sk-..."}}` 或
  `{"Api-Key": "..."}` 即可绕过，原样进入 `canonical(...)` 序列化后的 prompt。
- 修复：新增 `_INPUT_CONTENT_FIELDS = frozenset({"text", "channel"})` 与 `_project_input_content()`
  （`opspilot/investigation/loop.py`，紧邻 `_halt_from_store` 之后），只放行这两个顶层字段，且若其值本身是
  `Mapping`/`list`/`tuple` 也整体丢弃（不做递归剥离，直接丢该键）。`read_inputs()`（人工回读追问/纠正原文）
  与 `begin_round()` 本身均保持不过滤——这条投影只影响送进模型的路径。
- 已知边界（非本次遗漏）：`text` 是允许自由文本的字段，若调用方把凭据字符串直接写进 `text` 的文本内容本身
  （而非塞进结构化子字段/其它顶层键），这段文本仍会原样送入模型——白名单堵的是「结构化字段名绕过」，不做
  文本内容级别的凭据检测，已在 `loop.py` 该常量上方注释与 PR 描述中注明。
- 测试基础设施：`opspilot/investigation/store.py::MemoryStepStore` 新增可选 `inputs=` 构造参数（默认
  `()`，不影响既有调用），使 `begin_round()` 能在不起 PG 的情况下返回预置的 `opspilot_inputs` 行，从而对
  loop 的投影逻辑做纯单测覆盖。
- 新增测试 `tests/test_m1_investigation_loop.py::test_investigation_inputs_only_send_the_allowlisted_text_and_channel_fields`：
  follow_up content 同时携带 `text`/`channel`（应放行）与 `api_key`/`Api-Key`/`AUTHORIZATION`/嵌套
  `{"nested":{"api_key":...}}`（均应丢弃），断言 outbound 消息精确等于只含 `text`/`channel` 的投影结果。
  修复前（`git diff`补丁临时回退验证）该测试确认为红：旧黑名单逻辑下 `api_key`/`Api-Key`/嵌套值原样泄漏进
  outbound（只有 `AUTHORIZATION` 因命中旧黑名单被挡）；修复后绿。

### 2. `new_run()` 挂起期间创建的后继 Run，Run 行本身也要落 `paused`

- 问题（机器人 review thread，`chatgpt-codex-connector`，P2，PR #31）：`new_run()` 在全局/目标暂停生效时把
  `opspilot_incidents.state` 正确置为 `'paused'`，但插入 `opspilot_runs` 的 INSERT 硬编码 `state='queued'`
  （`opspilot/persistence.py`，`new_run()`），Run 行状态与其所属 Incident 不一致。`opspilot/recovery.py::
  rebuild_plan()` 的 `RecoveryPlan.candidate` 只读 `run["state"]`，因此会把这个应保持暂停的后继 Run 误判为
  可恢复候选，`Worker.resume()` 会据此反复发起一次必然被 scope fence 拒绝的 `claim()`。
- 判定：这是业务记录一致性缺陷，不是控制绕过——`claim()` 本身通过 Incident 级 `state in
  {"completed","cancelled","paused"}` 分支独立、正确地拒绝了这个 Run（scope 暂停语义未被绕过），但 Run 行
  本身记录的状态是错的，且会导致恢复模块做无意义的重试尝试。
- 修复：把 `next_state`（`paused`/`queued`）的计算挪到 INSERT 之前，INSERT 语句里也用 `next_state` 替代
  硬编码 `'queued'`，与紧接着对 `opspilot_incidents.state` 的 UPDATE 使用同一个值，两行状态不再可能分叉。
  未触及 `existing_run`/replay 幂等分支（`row["state"] != "cancelled"` 时提前返回/抛错的路径），无副作用。
- 新增 PG 测试 `tests/integration/test_m1_control_completion_postgres.py::
  test_new_run_created_while_suspended_persists_paused_on_the_run_row_too`：取消 Incident、挂起目标、
  `new_run()`，断言 `rebuild()["run"]["state"] == "paused"` 且 `opspilot.recovery.rebuild_plan(...).candidate
  is False`，并确认此时 `claim()` 仍独立因 `CONTROL_DENIED` 拒绝（纵深防御第二层不受影响）。修复前该测试在
  `assert rebuilt["run"]["state"] == "paused"` 处红（`AssertionError: assert 'queued' == 'paused'`）；修复后绿。
- 已在 PR #31 上对应的机器人 review thread 回复采纳并 resolve。

### 验证与独立审查

- `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_control_completion_postgres.py -v`：**5 passed**
  （含两个新测试）。
- `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py -q`：**41 passed**（无回归）。
- `pytest tests/test_m1_investigation_loop.py tests/test_m1_investigation_pairing.py -q`：**47 passed**
  （原 46 + 新增 1）。
- `make check`：**1445 passed, 100 skipped, 2 xfailed**，ruff check/format、mypy 均通过。
- 独立审查（全新上下文子代理，未参与改动讨论）：自行构造 16 组边界用例直接调用 `_project_input_content`
  验证白名单无绕过；自行用 diff+apply -R（未用 stash，协调者中途提醒后切换）把两处改动分别临时回退，确认两个
  新测试在旧代码上确实转红；核对 `read_inputs()`/`begin_round()` 未受影响；核对 `new_run()` 幂等分支与
  `rebuild_plan()`/`claim()` 因果链；全量回归复核一致。结论：两处修复均安全、依据充分、测试有效，可以合并；
  建议在 PR 描述注明「白名单不检测 `text` 自由文本内容本身携带的凭据」为已知设计边界。
- 审查过程中子代理曾用 `git stash push -u` 隔离 `persistence.py` 做红绿对照，`git stash apply` 复原后内容与
  当前改动逐字一致（已本地 diff 核对），但 `git stash drop` 被权限规则拒绝，遗留 stash 条目
  `review-verify-persistence-only-1789683798` 在共享 stash 栈里，不影响本仓库工作区内容，需要有权限的会话
  手动清理。

## 追加修复（2026-09-18 第三轮）：第二轮改动引出的 2 条新 bot thread

第二轮修复推送后，机器人对新 HEAD 又留了 2 条未 resolve review thread，均已处置为「成立修复」。

### Thread A（P1）：白名单误删 `question` 字段

- 问题：`_INPUT_CONTENT_FIELDS = frozenset({"text","channel"})` 会把 `{"question":"why"}` 整个丢弃——这
  正是本 PR 自己的 `test_follow_up_payload_is_durable_and_readable` 用作例子的 payload 形状，也是
  `opspilot/intake.py::IntakeRequest.question` 这个生产入口真实使用的字段名（独立审查指出的额外证据，
  未仅凭本仓库一个测试例子下结论）。修复前该场景下 `_round()` 发给模型的 `content` 会是空字典，追问文本
  实际上永远到不了模型——这是白名单选错字段集合导致的功能性回退，不是安全问题被绕过。
- 修复：`_INPUT_CONTENT_FIELDS` 加入 `"question"`，值仍必须是标量（非 `Mapping`/`list`/`tuple`），机制
  不变——不是退化回黑名单，`api_key`/`Api-Key`/`AUTHORIZATION`/嵌套结构在 `question`/`text`/`channel`
  任一键下依旧被丢弃（独立审查另做了一轮不依赖本仓库测试的对抗验证，含大小写变体、嵌套 dict/list/tuple，
  均确认被拦）。
- 新增测试 `tests/test_m1_investigation_loop.py::
  test_investigation_inputs_preserve_the_question_field_follow_up_payloads_use`：修复前（`git diff`+
  `git apply -R` 临时回退验证）红——`content` 变空字典；修复后绿。
- 提交 `18bd8f3`；已在该 review thread 回复采纳理由并 resolve。

### Thread B（P2）：同值 suspension 写入不该撤销活跃租约

- 问题：`set_global_suspension`/`set_target_suspension` 在校验完 `expected_generation` 后，无条件
  `nxt = current + 1`，即使提交的 `suspended` 值和当前值完全相同（例如：确认丢失后用刷新过的 generation
  重新提交一次「释放」，而释放前其实已经是释放状态）。`_lease_revoked()` 对每条活跃租约都比较
  `global_generation`/`target_generation` 是否等于租约捕获时的值，任何 generation 变化都会让所有当前
  持有的活租约被判定撤销——即使 suspension 实际值根本没变，被撤销的 worker 要等到租约自然过期才能重新
  claim。
- 修复：在 `expected_generation` 校验之后（不影响乐观并发校验本身，generation 不匹配仍然
  `CONTROL_CONFLICT`）新增判断——`suspended == row["global_suspended"]`（或目标版的
  `row["suspended"]`）时不递增 generation，仍写一条 `opspilot_suspension_audit` 记录（用当前 generation，
  该表 `audit_id` 是独立自增主键，`generation` 只是普通列，多行共享同一 `generation` 不违反约束），
  直接 `return current`。跳过 `if suspended: ...`（批量置 paused）副作用是安全的：独立审查核实
  `claim()`/`control()` 都直接读当前 `suspended` 布尔值（不只靠 generation）拒绝写入，且全仓库唯一把
  run 置为 `running` 的路径是 `claim()`；只要 suspended 保持 True，就不会有新的 run 进入需要「顺带置
  paused」的状态，因此 True→True 的 no-op 分支跳过该副作用不会漏处理任何 run。
- 新增 PG 测试 `test_same_value_global_suspension_write_does_not_revoke_active_leases`/
  `test_same_value_target_suspension_write_does_not_revoke_active_leases`：claim 一个 lease 后对同一
  scope/target 提交一次值不变的 suspension 写，断言返回 generation 不变、`lease_current(lease)` 仍
  `True`、`reserve_budget` 仍成功。全局测试读 `lease.global_suspension_generation`（claim 时刻捕获值）
  而非硬编码 0——`global_suspended` 是单例行，其 generation 会跨本 worktree 专属 PG 实例历史上所有测试
  运行累积，不会每次从 0 开始（这是我们第一版测试踩的坑，写成硬编码 0 后在真实 PG 上被推翻，已改正）；
  目标测试因为每次都注册全新 UUID 的 target，`target_generation` 确实总是从 0 开始，两种写法在仓库里都有
  先例。修复前（`git diff`+`git apply -R` 回退验证）两个测试均红（数值因历史累积不同，形如
  `assert 19 == 18`/`assert 26 == 25`、`assert 1 == 0`）；修复后绿。
- **未在本次处理、需要用户确认的文档冲突**：独立审查指出，C3 第 4 节「暂停事务增加对应 scope generation」
  （`docs/design/technical-proposal-2026-09-07.md` 第 112 行）字面上没有为「同值/幂等重复提交」留例外，
  与本次修复字面冲突。是否需要同步澄清/更新这句措辞（明确「generation 递增对应的是暂停生效状态的实际
  改变，不是每次调用」），留给用户/架构决策者判断，本任务未修改 C3。
- **已知、非本次引入的既有不一致**：`opspilot/domain/control.py::suspend_globally`/`suspend_targets`
  纯函数仍是「每次调用无条件 bump」，与本次修复后的 `persistence.py` 行为不一致；但这两个域层函数目前
  只被 `opspilot/domain/__init__.py`（重导出）和 `tests/test_domain_contracts.py` 使用，运行时代码不调用
  它们（`persistence.py` 是独立用原生 SQL 实现同一套语义，不经过域层），且
  `tests/test_architecture.py::test_persistence_builds_on_domain`（`xfail(strict=True)`）已经把「持久化层
  是否应建立在 domain 状态机之上」记录为待决架构债务——这处不一致不是本次 PR 新增或加深的遗漏，不额外处理。
- 提交 `0d1437b`；已在该 review thread 回复采纳理由（含上述文档冲突提示）并 resolve。

### 验证（第三轮）

- `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_control_completion_postgres.py -v`：**7 passed**。
- `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py -q`：**41 passed**（无回归）。
- `pytest tests/test_m1_investigation_loop.py tests/test_m1_investigation_pairing.py -q`：**48 passed**
  （原 47 + 新增 1）。
- `make check`：**1446 passed, 102 skipped, 2 xfailed**，ruff check/format、mypy 均通过。
- 独立审查（全新上下文子代理，未参与改动讨论）：独立发现 `IntakeRequest.question` 作为 Thread A 修复的
  额外佐证；独立走完 no-op 分支跳过批量 paused 副作用的安全性推理链；确认审计表无唯一性约束依赖
  `generation`；确认域层不一致属于既有已记录债务；用 diff+apply -R 独立复现两组红绿；全量回归复核一致；
  指出上述 C3 措辞冲突。结论：两处修复均安全、依据充分，可以合并。

## 追加修复（2026-09-18 第四轮）：第三轮改动引出的 1 条新 bot thread（P1）

### 白名单字段未做大小限制，单条超大输入会让事故永久卡死

- **机器人原文**：调用方可以通过 `control()`/`append_input()` 持久化一个任意大的 `text`/`question` 值，
  `_project_input_content()` 原样转发。一旦序列化后的 `investigation_inputs` 让整个请求超过
  `MAX_HTTP_REQUEST_BYTES`，`_reject_oversized()` 会让每一轮都失败；因为该输入持续被持久化、
  `begin_round()` 每次都会重新选中它，事故会永久卡住直到人工修数据库。
- **核实**（含独立审查的额外交叉验证）：
  1. `opspilot/persistence.py::begin_round()` 的水位是累积的（`sequence<=watermark`，非增量选取新行）。
  2. `control()` 支持的动作集合 `{"cancel","pause","resume","follow_up","correct"}` 没有一个能删除/替换
     已持久化的 `opspilot_inputs` 行；`append_input()` 撞同一 `input_id` 时返回既有 sequence，不覆盖内容。
  3. 独立审查额外核查了 `opspilot/domain/control.py` 里定义的更大动作集合（`reopen`/`close`/`cancel_run`/
     `takeover` 等），确认这些在 `persistence.py` 里零实现、零调用点——不是被漏掉的恢复路径，是尚未接线的
     领域类型；`accept()` 能开一个全新 `incident_id`，但那是新事故，不能恢复原事故。
  4. 结论：在当前 M1 代码下，一条超大输入一旦落库，原 incident 确实无法在不改库的情况下恢复到可继续调查
     的状态——推理链成立。
- **修复**：`_project_input_content()` 加入 `_INPUT_CONTENT_FIELD_MAX_CHARS = 8192`，对 `text`/`channel`/
  `question` 三个白名单字段里「值是字符串且超长」的情况做截断（保留前 8192 字符，追加
  `" …[truncated]"` 后缀），非字符串标量（bool/int/float/None）不受影响。**这个 8192 不是
  `opspilot/investigation/limits.py` 那张「2026-09-13 冻结」资源上限表的一部分**（那张表管的是整个序列化
  请求，不是单个字段）——是本次自选的防御性默认值，代码注释与本节均明确标注，不伪装成走了同样审批流程的
  正式冻结数字，留给用户确认或替换成正式数值。
- **新增测试** `tests/test_m1_investigation_loop.py::test_investigation_inputs_truncate_an_oversized_free_text_field`：
  构造 `content={"text": "x" * (MAX_HTTP_REQUEST_BYTES + 1)}`，断言 `loop.run(request)` 的
  `outcome.handoff_reasons != ("REQUEST_TOO_LARGE",)`（模型请求真的发出去了，不是在 `_reject_oversized()`
  那一步就被拦下）。修复前（`git diff`+`git apply -R` 临时回退验证）该测试红——`handoff_reasons` 确实等于
  `("REQUEST_TOO_LARGE",)`；修复后绿。
- **独立审查额外发现、写入本节供后续参考（非本次需要处理的阻塞项）**：
  1. **JSON 转义膨胀的字节界**：8192 字符截断按字符数而非字节数计算；若字段值恰好是大量控制字符，
     `json.dumps` 会把每个字符转义成 `\u00XX`（膨胀 6 倍），单字段最坏情况可达约 49KB（512KB 的
     ~9.4%），三个白名单字段同时拉满最坏情况约 28%——仍远不足以单独撑爆 512KB 预算，结论不变，但这是
     「不是理论上的紧界」的已知余量，若未来要精确控制应改成按 UTF-8 字节数截断。
  2. **未覆盖的更大范围问题**：本次修复只堵住「单条输入本身超大」这一种卡死方式；`begin_round()` 累积
     选取的设计意味着「很多条正常大小的 follow_up/correct 长期累积」同样可能让总投影超过 512KB，且数量
     本身无上限——这是同一类问题的更大范围版本，本次修复的措辞（代码注释与本节）都明确限定在
     "single oversized value"，未声称已解决累积增长这个更广的开放风险，留待后续按需处理。
  3. **UTF-8 多字节字符切割**：已核实 Python 字符串切片按 code point 操作，非 BMP 字符（如表情符号）
     不会被切出残缺代理对，独立审查用脚本验证过编码/解码往返无损。
  4. **截断读出 vs 写入时拒绝/截断的取舍**：独立审查认为「读出投影时截断」优于「写入时拒绝/截断」——
     `opspilot_inputs` 是业务记录（PRODUCT-CONSTRAINTS 的「业务记录恢复权威」），写入时截断会不可逆丢失
     原始值，未来放宽上限也拿不回全文；写入时拒绝会让合法但偏长的 `correct` 人工纠正被硬性拒绝，制造新的
     可用性问题。当前「不动持久层、只影响投影给模型的内容」的方案权衡合理，未改动。
- **处置**：GitHub reply（回复原评论），`resolveReviewThread` 标记对应 thread 为 resolved。提交
  `786e720`。

### 验证（第四轮）

- `M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_control_completion_postgres.py
  tests/integration/test_m1_durable_state_postgres.py -q`：**48 passed**（无回归，本次改动不涉及持久层，
  未新增 PG 测试）。
- `pytest tests/test_m1_investigation_loop.py tests/test_m1_investigation_pairing.py -q`：**49 passed**
  （原 48 + 新增 1）。
- `make check`：**1447 passed, 102 skipped, 2 xfailed**，ruff check/format、mypy 均通过。
- 独立审查（全新上下文子代理，未参与改动讨论）：独立核实「永久卡死」推理链（含额外核查
  `opspilot/domain/control.py` 更大动作集合未接线这一潜在恢复路径，确认不存在）；独立验证截断不影响非
  字符串标量、不切割多字节字符；独立发现 JSON 转义膨胀的字节界与「累积多条正常输入」的开放风险两点补充；
  用 diff+apply -R 独立复现红绿；全量回归复核一致。结论：修复安全、依据充分，可以合并。

## 与 main 同步（2026-09-23，PR #31 改为 target main 前置）

- 起点 `bf11401`（=origin）。`git merge origin/feature/m1-01-intake-auth`（`610ccb1`，已含 main `d9afdf1`），
  合并提交 `e376747`；`origin/main` 已被该分支包含，无需再合并。未 rebase、未改写历史。
- 冲突与处置：
  - `ROADMAP.md`、`tests/test_architecture.py`：取 main 版本（本分支 23 个提交未改这两处；
    `test_persistence_sql_never_selects_a_qualified_star` 两侧相同）。
  - `opspilot/persistence.py`：`accept()` 同时保留 main 的 `input` 与本分支的 `target_id`；`new_run()`
    INSERT 带 main 的 `input` 列并用本分支的 `next_state`（paused/queued）；`claim()` 保留 main 的
    incident/run 状态优先判定，再加本分支的 scope 暂停拒绝；`commit_tool()` SELECT 同时带 main 的
    `s.response` 与暂停列；`rebuild()` 保留 main 的 `pending_tools`，追加 `inputs`/`input_rounds`/`pending_inputs`。
  - `opspilot/investigation/store.py`：以 main 的 `StepCommitter`（usage/settle/renew/control_generation）为底，
    加回 `begin_round()`/`assert_current()`；`MemoryStepStore` 保留 main 的 `control_generation`/`input`，加 `inputs=`。
  - `opspilot/investigation/loop.py`：在 main 的 `_State` 版 `_round()` 内重新接入 `begin_round()` 与
    `investigation_inputs` 投影消息（位于 `state.messages` 之后、最终报告指令之前）；`assert_current()` 分别
    加在每次工具派发前（`renew()` 之后）与每次模型请求前（预留之后），保留 main 的 `reservation`/`_settle` 记账
    ——即本记录「合并顺序提醒」一节预告的形态。
- 为适配 main 而做的改动（非本分支原有语义变化的部分已注明）：
  1. `DurableStepStore.begin_round()` 不再给步骤键加 `g<gen>:e<epoch>:` 前缀：main 的 `step_key()`/`parse_step_key()`
     禁止 segment 含 `:`，transcript 重建会把前缀读成外来 segment。改为在 `DurableStore.begin_round()` 内处理
     「同键、旧代际、未提交」的输入边界：按当前水位重新冻结（`ON CONFLICT DO UPDATE`），已提交的旧代际边界仍拒绝。
     main 的重建让 `next_round` 跨代际单调递增，因此正常路径不会复用已提交轮次键；复用只发生在 `_RoundAborted`
     未提交轮次。
  2. main 新增的 `charge_tool()`/`block()` 走 `_lease_revoked()`，其 SELECT 补上 scope 暂停列并先取 `_lock_scope()`；
     否则 claim 时捕获的 suspension generation 非零的租约会在每次工具计费时被误判撤销。`renew_lease()`/
     `settle_budget()` 为 main 的内联栅栏，未改动（暂停时 `owner=NULL` 已使其拒绝）。
  3. `tests/integration/test_m1_control_completion_postgres.py` 的 `new_run()` 传入 main 必填的 `expected_generation=1`。
- 已知局限：`_manage_context()` 的上下文预算估算不含随后追加的 `investigation_inputs` 消息（与合并前一致）。
- 验证：`make check`：**1833 passed, 177 skipped, 2 xfailed**，ruff check/format、mypy 通过。PostgreSQL 集成套件
  （DSN 固定 55431，属另一 worktree）本地未运行，由 push 后 CI `m0-postgres` 覆盖。

## 独立审查处置（2026-09-23，PR #31 retarget main 后）

先合并 `origin/main`（`6649a69`，仅新增 M1-01 集成验证证据文件，无冲突）。未 rebase、未 force-push、未改 PR/机器人/合并状态；未移动任何 `prompt_revision`/`context_policy_revision`/tool 版本值（`context_policy_revision` 只哈希 compaction 策略与指令文本，本次未触及）。

- **P1（必须修复）收到过 follow_up/correct/event 输入的 Run 无法恢复** — 提交 `e61216b`。
  - 根因：`_round()` 把 `investigation_inputs` 投影消息追加进出站请求，步骤行的 `input_snapshot_hash` 因此包含它；
    `rebuild_transcript()` 只从 `initial_messages` + 步骤行重建，`_check_snapshot_hash()` 失配 → `INCOMPATIBLE_STATE`，
    runner 把 Run 置为 blocked。
  - 修复：投影（字段白名单、单字段上限）与消息形状迁入 `opspilot/investigation/context.py`
    （`project_input_content`/`inputs_message`/`input_watermark`），loop 与 rebuild 共用一个函数；步骤行 `context`
    新增 `input_watermark`（`begin_round()` 返回的边界）；`rebuild_transcript()` 读取快照 `inputs`，在校验哈希前把
    ≤ 水位的输入按同一位置（transcript 之后、最终报告指令之前）重新插入；快照缺少对应输入时仍 fail closed。
    `MemoryStepStore` 新增 `append_input()`、`snapshot()` 输出 `inputs`。
  - 红→绿：`tests/test_m1_investigation_context.py::test_a_run_that_received_a_follow_up_between_rounds_rebuilds_and_resumes`、
    `::test_a_run_with_a_follow_up_present_from_the_start_rebuilds_its_first_round`、
    `tests/test_m1_investigation_compaction.py::test_a_follow_up_never_enters_the_folded_history_and_the_rebuild_still_matches`
    在仅去掉 rebuild 的 `inputs=inputs` 传递时均 `ContextError: INCOMPATIBLE_STATE`（临时文件替换后复原，非 checkout/stash），
    修复后绿。compaction 一致性：inputs 消息不进 `state.messages`、不被折叠、compaction 请求不携带（同一测试断言）。
  - PG：`tests/integration/test_m1_loop_resume_postgres.py::test_a_follow_up_between_rounds_then_a_restart_resumes_and_publishes`
    （follow_up 在第 1 轮请求在途时落地并栅栏该尝试；第 2 次尝试重新冻结同键边界并发送输入后崩溃；第 3 次尝试按行重建
    并发布；共 4 次物理请求）。**本地未运行**（55431 属另一 worktree），由 CI `m0-postgres` 执行。
- **P2 begin_round/水位回写/重新冻结无 PG 测试** — 提交 `5e0dbf0`，
  `tests/integration/test_m1_control_completion_postgres.py` 新增 (a) 冻结→重试同边界→commit 后 `run.input_watermark`
  推进且 `input_rounds.committed`；(b) G 代冻结未提交 + follow_up(G+1) + 重新 claim 后同键返回含 follow_up 的新水位；
  (c) 另一代已提交的键 `CONTROL_DENIED`、下一键正常冻结。本地未运行，CI 执行。
- **P2 loop 层 `assert_current()` 拒绝点无测试** — 提交 `a3b9b04`，`tests/test_m1_investigation_loop.py` 新增
  `test_control_denied_after_the_reservation_stops_the_model_request_before_it_leaves`、
  `test_control_denied_after_the_lease_renewal_stops_the_tool_dispatch`。红→绿：临时把两处 `self.store.assert_current()`
  换成 `pass` 时，模型请求发出（`model.calls != []`）、工具被派发（`transport.called is True`）；恢复后绿。
- **P3（修复）paused 事故的重复 pause 因带 payload 被放行** — 提交 `2a92c89`，`opspilot/persistence.py::control()` 守卫改为
  `action == "pause" or payload is None`；新增 PG 测试
  `test_a_redundant_pause_with_a_payload_is_still_refused_on_a_paused_incident`（含带 payload 的 follow_up 仍被记录且保持暂停）。
  本地未运行，CI 执行。
- **P3（不改，登记为待产品决策的开放项）**：
  1. 输入每轮以尾随消息累计重发（`begin_round()` 水位累积，`sequence <= watermark` 全量选取）；
  2. `INPUT_CONTENT_FIELD_MAX_CHARS = 8192` 与字段白名单 `{text, channel, question}` 为本地自选，未走冻结上限审批；
  3. `_manage_context()` 的上下文预算估算不含随后追加的 inputs 消息。

验证：`make check`：**1839 passed, 182 skipped, 2 xfailed**，ruff check/format、mypy 通过。
