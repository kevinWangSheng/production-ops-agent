# 合同预审：已结算的同轮兄弟工具结果先落库（F2 断点③ 修复，改 #309 R-5）

- 预审者：Claude Opus 5.5，全新上下文，只读。基线 `main` `200172c`；F2 D 证据读自 `poa-f2-d` `bd45d57`。没跑测试，没碰 PG/kind。「已核对」指读过代码。
- 用户 2026-10-22 已裁决：断点③记 FAIL，不改 U7 判据；本预审只针对产品修复。

## 1. 现状

**已核对（事实）**
- 并行路径 `_run_tools_parallel`（`opspilot/investigation/loop.py:815-926`）：只有 `executor.execute` 在线程池里跑。主线程按 index 循环：`next_commit = index`，然后一直等 `dispatched[index]` 完成（`:907-923`），之后才 `_dispatched_view` + `_deliver`。`_deliver` 依次做 `store.commit_tool` → `delivered`/`evidence_ids` → tool 消息 → `capture_delivery`（`:951-984`）。所以 t1 比 t0 先完成时，t1 的结果只在内存里等着，这就是断点③失败的机制。
- 这样设计的理由（#309 合同 U-3 A、R-5，`docs/tasks/2026-10-22-309-loop-dedupe-parallel-contract.md`）：让已提交行、覆盖消息和下一次模型请求与串行时逐字节相同（U-4）；`StepCommitter`/`EmittingCommitter` 只在单线程使用；事件 `tool_committed` 的顺序不变。U-3 A 当时已经写明代价：「崩溃时……重复外部查询最多 N−1 次」。
- 存储本身不要求按顺序：`commit_tool` 按 ordinal 追加，重复提交是幂等的，租约失效时记 late_result 并抛 `CONTROL_DENIED`（`opspilot/persistence/steps.py:183-240`）；内存版也一样（`opspilot/investigation/store.py:266-278`）。
- 模型可见顺序：`pair_tool_results` 按 tool_calls 顺序重排结果（`messages.py:136-139`）；覆盖消息用的是 `state.delivered` 的追加顺序（`loop.py:139`）。重建时 `_ordered_results` 按 ordinal 排序，有 ordinal 缺失就算 pending（`context.py:796-813`，另一处 `:1387-1398` 也排序）。
- evidence 的 `committed` 列：执行器先 `register`，这时 committed=false，同字节的重放可以覆盖投影（`web/evidence.py:107-118,324`）。工具结果提交时，`EmittingCommitter.commit_tool` 调 `_pin_evidence` → `evidence.commit`，置 committed=true 并标记知识草稿陈旧（`progress.py:291-311,484-497`；`web/evidence.py:252-290`）。`evidence_id = operation_id:dispatch_id`，其中 dispatch_id 是每次派发新生成的 uuid4（`executor.py:919,1612`）。所以重派会产生新的 evidence_id，epoch 1 里 t1 那行会一直是 committed=false 的孤行。
- resume 是否重派：`RecoverySession.execute_pending` 串行处理 `pending_tools`，也就是 rows 里缺失的 ordinal（`worker.py:155-204`、`runner.py:322-343`）。只看 `tool_results`，不看 charge 和 evidence。C3 §7 表：「响应已提交，部分工具未完成 → 只处理尚未完成的工具操作；工具结果已经提交 → 复用」。外部查询不承诺 exactly-once（`technical-proposal-2026-09-07.md:250-258`）。
- 按存储顺序读 `tool_results` 的地方：页面 `_step_view`（`web/service.py:2234-2250`）、`acceptance._committed_evidence`（`acceptance.py:226-240`）、`announce_recovered_tools`（`progress.py:340`）。SQL 审计用的是 `jsonb_array_elements`，按 ordinal 或集合匹配，与顺序无关（`acceptance_audit.py:214`、`acceptance_investigation.py:130,193`）。
- 断言提交顺序的现有测试：`tests/acceptance/test_309_loop_dedupe_parallel.py:350`（`[0,1]`，Barrier 下两条几乎同时完成）和 `:509`（fence 场景 `commit_attempts == [0]`，t0、t1 谁先完成不确定）；`tests/test_309_parallel_postgres_impl.py:127`；`tests/test_309_loop_parallel_impl.py:93,107,128,188`。
- harness：`committed_ops` 用 `range(s["tool_results"])` 推出已提交的 ordinal（`poa-f2-d/scripts/f2_kill_resume.py:357-359`），默认已提交的是前缀。kill 条件是「本 epoch 只有一条未结算 charge，并且它是卡住的工具」（`:246-255,784-797`），没有等兄弟结果落库。

**推断**
- 修复后仍有一个毫秒级窗口：兄弟调用已经结算，但主线程还没提交它。cap=2 时如果两条都真在途，kill 后两条都会重派，这是并行本身的性质。所以产品能保证的是：「kill 时已提交的不重派，重派只限 kill 时还没提交的调用，最多 cap 条」。U7 的「≤1（仅在途那一条）」要以断点③的定义成立为前提：一条卡住，兄弟已经结算并提交。

## 2. 决定表

### 需用户裁决（3 项）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| U-1 | 修复方式 | **A** 一个结果在主线程被看到已完成，就立即 `commit_tool`（含 pin 和 `tool_committed` 事件）；`seen_tool_calls`、`delivered`/`evidence_ids`、tool 消息和 `capture_delivery` 仍按 index 处理。改动集中在 `loop.py`，模型可见内容不变，恢复路径和 C3 不动。**B** 持久提交顺序不变，resume 时复用「已结算、evidence 未提交」的结果：串行下的毫秒窗口也一并消除，但要新写一条恢复路径（从 evidence 行重建 model_view、在 epoch 2 pin、处理 charge 归属），会碰 `worker.py`/`runner.py`，还要解释 C3 §7「证据提交成功后才允许消费」。**C** 默认并行数改成 1：与 C3 §13「工具并行数 2」冲突，等于不修。 | **A** | `loop.py` | |
| U-2 | 改写 #309 的 U-3 A / R-5 / R-8 / R-12 | 新口径：**持久提交按完成顺序，模型可见面按 index**。（a）`tool_committed` 事件和 late_result 可能乱序，例如 ordinal 1 先于 0；页面实时日志按事件序号原样显示。（b）R-8 改为：某条调用抛出非预期异常时，已经被观察到完成的更高 index 结果可能已经提交，恢复只重派缺失的 ordinal。（c）R-12 改为：fence 失败后第一次提交遇到 `CONTROL_DENIED` 就停，这一次可能是任意 index。替代方案是事件延后到按 index 发出，但崩溃后已提交行就没有对应事件（`announce_recovered_tools` 只补报重派的 ordinal），会丢事件，不可取。 | 采纳新口径 | #309 合同追加 r2；`docs/development.md:135`；2 个独立验收断言（`:350` 改为按排序或集合比较、`:509` 改为允许任一 index）由测试作者改 | |
| U-3 | 断点③ 的判定读法与复跑 | U7 判据不变。harness 的 kill 条件加一条：「同一 step 本 epoch 内已结算的兄弟调用都已出现在 `tool_results` 里，并且状态稳定 1 s」（与 ① 的 1 s 稳定写法相同），`committed_ops` 改为读实际 ordinal。代价：断点③ 的定义写明「兄弟已结算并提交」；上面说的毫秒窗口不在判据覆盖范围内，要在证据里如实写。替代方案：不加等待，修复后仍有概率在窗口内 kill 得到 2，结果不稳定。 | 采纳 | `scripts/f2_kill_resume.py`、F2 证据 README | |

### 可逆默认

| 编号 | 推荐与理由 |
|---|---|
| R-1 | 把 `_deliver` 拆成 `_commit_durable(step_id, index, view)` 和 `_deliver_visible(...)`。每次 `wait` 醒来后，先按 index 升序提交所有「已完成、成功、未提交、index > 当前轮」的 future，再继续按 index 交付。当前轮次的调用仍然先提交再交付。duplicate view（不派发的那种）在自己轮次提交，因为它依赖 `seen_tool_calls` |
| R-2 | `commit_tool` 写入时按 ordinal 排序（`steps.py` 的 UPDATE 前做 `results.sort`，`MemoryStepStore` 同样处理）。完整 step 的行与改动前逐字节相同，页面 `_step_view` 和 `_committed_evidence` 不用改；旧行本来就是有序的 |
| R-3 | R-2（批内重复）的派发闸门仍以交付 index `next_commit` 为准，不以持久提交为准，派发决定与现在完全相同 |
| R-4 | 提前提交时遇到 `CONTROL_DENIED` 或 `EVIDENCE_PROJECTION_FAILED`，照现有方式 `_halt_from_store`。之后不再提交，也不再派发；在途调用由 pool 退出时等它结束，结果丢弃 |
| R-5 | 异常路径不额外排空：已提交的就是「观察到即提交」的那部分，异常在出错调用自己的轮次原样抛出 |
| R-6 | cap=1 和单调用的串行路径不改，本来就是立即提交。U-4 的 cap=1 逐字节等价仍然成立 |
| R-7 | 不改 `CONTEXT_POLICY`、`prompt_revision`、tool schema，不加迁移：行结构不变，重建与提交顺序无关，在途 Run 新旧代码都能续跑 |
| R-8 | `execute_pending` 保持串行；预算、charge、`tool_seconds_used` 口径不变（修复后只是少一次重派的 charge）；#308 的超限记录在重派时会再登记一次，修复后这种情况变少，不用另外处理 |

## 3. 允许修改的文件、可观察行为与证据

- 产品：`opspilot/investigation/loop.py`；`opspilot/persistence/steps.py`（只加排序）；`opspilot/investigation/store.py`（`MemoryStepStore` 排序）；`docs/development.md:135` 那一句。不改：`context.py`、`messages.py`、`worker.py`、`runner.py`、`executor.py`、`progress.py`、迁移、C3。
- 合同与测试：lead 在 #309 合同追加 r2（U-2 的三点）；独立测试作者改 `test_309_loop_dedupe_parallel.py:350,509` 两处；实现者自己的 `tests/test_309_*impl.py` 由实现者对照 r2 调整。
- 给独立测试作者的确定性可观察行为：
  1. （内存版，红绿明确）cap=2、两条调用。t0 的 transport 等待条件「store 里已有 ordinal 1」，超时 2 s 即失败；t1 立即返回。改动前会超时（红），改动后 t0 能看到 ordinal 1 再返回（绿）。同时断言下一次模型请求、覆盖消息、`IncidentOutcome` 与 cap=1 相同，行里的 ordinal 是 `[0,1]`。
  2. （PG 版，复现断点③）`M1_DURABLE_POSTGRES=1`，`LEASE_SECONDS=1`，cap=2。t0 的 transport 阻塞在一个 Event 上；在守护线程里跑 attempt 1；轮询 PG，直到 step 的 `tool_results` 含 ordinal 1；然后放弃这个线程（模拟死亡，不释放租约）；等租约过期，用新的 runner/Worker 执行 resume。断言：t1 的 transport 请求总共 1 次，t0 共 2 次；`tool_operations_used == 3`；最终结论能引用 epoch 1 的 t1 evidence_id；之后放行旧线程，它的提交被 fence 记为 late_result，已提交行不被改写。改动前 t1 是 2 次（红）。如果要换成真实 SIGKILL，就在子进程里跑 attempt，测试核对自己起的 PID 后再 kill。
  3. 回归：fence 和异常场景按 r2，提交过的行不被改写，失败后不再派发。
- 最小真实证据：能复用 F2 D harness。前提是 D 先合并（证据类，预授权），并按 U-3 改两处读数逻辑。然后在 otel-demo 档、默认并行 2 下重跑一次 `--breakpoint tool_in_flight`（上次 ③ 花了约 0.34 CNY，要占 kind，跑前查 swap）。产出 `summary.json` 和 epoch 2 的 trace 链接，作为本 PR 的 ADR-0006 证据，同时让断点③ 有机会翻成通过。旧的 FAIL 记录保留，新增目录记录这次复跑。

## 4. 是否纳入 M2 收尾，与 F2 E 的关系

建议纳入，并排在 F2 E 之前。F2 第 2 步要求「bounded repeated work」，U7 判据不改，断点③ 失败的情况下 E 不能把 F2 的 `passes` 翻成 true。顺序：D 合并（FAIL 照实记录）→ 新开 issue 和修复 PR（用户门：状态恢复和 #309 合同变更），附 ③ 复跑证据 → E 引用复跑结果。也可以让 E 自己复跑 ③，但那样会把产品修复的证据和验收混在同一个 PR，不推荐。

## 5. 未确认

- 页面或其他消费者是否依赖 `tool_committed` 事件按 ordinal 到达：只查了 `incident.html:390-400`（追加式日志）和 `acceptance_audit.py:226,281`（按存在性 join），没有逐个查外部导出和 outbox。
- 在 epoch 1 提前 pin 的 evidence，在 resume 后是否与知识草稿的陈旧标记有其他交互：推断与顺序无关，没跑过验证。
- harness 快照里 `tool_results` 字段是计数而不是 ordinal 列表，具体出处在 `poa-f2-d` 的快照查询里，没逐行读。
- 修复后毫秒窗口在真实 Run 里出现的概率：没测过。


## 裁决（2026-10-23）

用户裁决 U-1 A（完成即提交）、U-2 采纳新口径（持久提交按完成顺序，模型可见面按 index）、U-3 采纳（kill 前等兄弟提交并稳定 1 s，毫秒窗口如实写入证据）；R-1–R-8 可逆默认照做。
