# 租约续期接入 RecoverySession（lease-wire.md §7b）执行报告

- 日期：2026-09-17
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m1-restart-recovery`，分支 `feature/m1-01-restart-recovery`（base `chore/durable-store-hardening`）
- PR：**#30** https://github.com/kevinWangSheng/production-ops-agent/pull/30
- 接手起点：`git status` 干净，`git log -3` 显示另一 agent 刚推送的 `bd47a9f`（记录 pending_tools 读取修复的任务记录提交）等三个提交，均无冲突地作为本次工作的起点
- 任务记录：`docs/tasks/2026-09-16-m1-01-restart-recovery.md` 追加节「租约续期接入 `RecoverySession`（lease-wire.md §7b）」
- 未向调度者提问；末尾「需用户决定」为空

## 1. 依据与做法

依据 `lease.md`（PR #35 `fix/lease-renewal` 的执行报告）第 1、2、7b 节：PR #35 给 `DurableStore` 新增
`renew_lease(lease, extend_seconds) -> datetime`（与其它写路径共用同一栅栏：owner/epoch/事故代际/未撤销/未过期/未过
deadline 全部满足才续期，否则 `PersistenceError("CONTROL_DENIED")`；`extend_seconds<=0` 则 `INVALID_INPUT`）。

**PR #35 尚未合并**，本分支的 `DurableStore` 没有 `renew_lease`；未复制其实现。做法：

- `RecoverySession.execute_pending` 在每次 `execute(item)` 之后、`self.store.commit_tool(...)` 之前调用新增的
  `_renew()`；通过 `getattr(self.store, "renew_lease", None)` 以「可选能力」接入——store 没有该方法时是 no-op，
  行为与改动前完全一致；#35 合并后自动生效，不需要再改这个分支的代码。
- 拒绝语义与本模块其它写路径一致：`_renew()` 原样抛 `PersistenceError`，不吞、不改包装类型。
- `Worker.claim`/`Worker.resume` 的 `lease_seconds` 默认值与 `RecoverySession.renew_seconds`/
  `Worker.resume(renew_seconds=...)` 统一为新增常量 `DEFAULT_LEASE_SECONDS=420`（见下方独立审查发现 1）。

## 2. 测试

**单测**（`tests/test_worker_recovery.py`，内存 `_RecordingStore` 双测替身，不需要 PG）：
store 无 `renew_lease` 时行为不变；有该能力时验证调用顺序恰为 `execute → renew → commit`；续期被拒
（`CONTROL_DENIED`）时该工具结果不提交、异常原样上抛；新增回归测试锁定四处默认值都等于
`DEFAULT_LEASE_SECONDS`。

**PG 集成测试**（`tests/integration/test_m1_lease_renewal_wiring_postgres.py`，跳过条件
`M1_DURABLE_POSTGRES!=1 or not hasattr(DurableStore,"renew_lease")`，本分支上恒跳过）：用
`git checkout -b tmp/lease-wire-30-verify`（本地临时分支，`feature/m1-01-restart-recovery` +
`origin/fix/lease-renewal`，三方 merge 无冲突）验证了两轮（初版与独立审查修复后各一次）：

1. 短初始租约 + 两个 pending 工具，仅靠续期让第二个 `commit_tool` 不因租约到期被拒；
2. 工具执行期间人工 `cancel`，续期本身被拒、结果确未提交。

两轮均针对真实 PostgreSQL 通过（与 #35 自带 17 例、既有 `test_m1_durable_state_postgres.py` 一起共
`55 passed`，无回归）。临时分支只用于本地验证：跑完即 `git branch -D` 删除，**未提交、未推送**该合并；
过程中遇到与另一并发 worktree（`production-ops-agent-m1-progress-ui`，同样运行 `scripts/m0/postgres_lab.py`）
共享的硬编码端口 55431 冲突，改用本 worktree 独有的临时端口做验证（脚本改动未提交，用
`git diff`+`git apply -R` 无损撤销，未使用 `git checkout --`/`stash`/`commit --amend`）。

## 3. 独立审查（全新上下文只读子代理，未见实现者结论）

- **发现 1（已修复，`1d55e9f`）**：续期发生在 `execute()` 之后，对循环里**第一个**（或唯一一个）pending
  工具，其自身执行期间只受初始 `claim()` 的 `lease_seconds` 保护，续期帮不上——若该工具执行时长接近改动前的
  默认初始租约（30s），`renew_lease` 本身会因租约已过期被拒，接线对这唯一工具等价于没有收益。
  修复：`Worker.claim`/`Worker.resume` 的 `lease_seconds` 默认值改为与 `renew_seconds` 相同的
  `DEFAULT_LEASE_SECONDS=420`；已 grep 确认本分支没有任何调用方依赖旧默认值 30，风险低。
- **发现 2（有依据的疑点，未改代码，仅记录）**：`420` 借自 lease.md §2「`MODEL_REQUEST_TIMEOUT_SECONDS`
  (360s)+60s 余量」的推导，但那是给会调模型的循环用的；`RecoverySession` 本身不调模型，只重放已提交的工具
  计划，本分支代码里也没有强制「单工具 ≤30s」的常量或校验（该说法只出现在 `docs/evidence/m0-*` 的 M0 实验
  报告里，与本分支生产代码无关联）。420s 对这条路径偏保守但不算错，已在 `opspilot/worker.py` 的
  `DEFAULT_LEASE_SECONDS` 注释里如实标注「未独立针对工具执行时长重新推导」，留给后续复核。
- 无阻塞发现：拒绝语义一致、三个新单测非同义反复、PG 测试跳过条件符合预期、未改动
  `persistence.py`/`recovery.py`/`_assert_current`/`publish`/`claim` 的既有语义、未超出任务范围。

## 4. make check / PG 结论

```
.venv/bin/ruff check .            -> All checks passed!
.venv/bin/ruff format --check .   -> 415 files already formatted
.venv/bin/mypy                    -> Success: no issues found in 15 source files
.venv/bin/python -m pytest        -> 1060 passed, 93 skipped, 2 xfailed
```

PG 定向（本 worktree 专属 lab，端口 55431，跑完已 `stop`）：

```
M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_durable_state_postgres.py -q
-> 37 passed
```

费用：0（无模型调用）。

## 5. GitHub 机器人 Code Review（不是交付门槛，仍逐条处置了返回的发现）

第一次 `@codex review`（2026-09-17T19:23:16Z）针对 HEAD `384d62c` 返回一条**与本次租约续期任务无关**、
但在本分支范围内的新发现（P2，`opspilot/persistence.py:71`）：`_tool_plan()` 的 `value or []` 会把
`{}`/`""`/`0` 这类**存在但畸形**的 `tool_calls` 值静默当成「合法无工具」，绕过 `rebuild()` 本该走的
`INCONSISTENT_STATE` fail-closed 校验（该函数由今天早些时候另一轮修复 `4273a38` 引入，不是本次续期改动
引入的）。**采纳并修复**（`a8c28e5`）：拆出 `_tool_calls()`，缺失/`None` 才算合法无工具，存在但非
list 一律返回 `None` 触发既有 `isinstance` 校验；扩展了顶层与 loop 两种形状的既有畸形测试，覆盖
`{}`/`""`/`0` 三个 falsey 值；PG 定向复验 `37 passed`。已在该 thread 下回复采纳说明并 `resolveReviewThread`，
当前 PR 无未处理 thread（`isResolved==false` 数量为 0）。

第二次 `@codex review`（2026-09-17T19:33:26Z，针对修复后的 HEAD `a8c28e5`）**截至本报告完成时尚未返回**。
按 AGENTS.md：机器人 Code Review 不是交付门槛，当前额度不稳定，不因其不可用而阻塞合并；若它之后返回了
发现，按相同流程另行处置（采纳并修复，或拒绝并在 PR 回复依据）。

## 6. CI

`workflow_dispatch`（该 PR base 非 `main`，不会自动触发）对 HEAD `a8c28e5` 的 run
[35265574868](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35265574868)：
`checks` success（55s）、`m0-postgres` success（50s）。`mergeStateStatus=CLEAN`，`mergeable=MERGEABLE`。

## 7. 提交

- `9d23b9f` feat: renew the lease between executing and committing a pending tool
- `711c67d` style: format the lease-renewal wiring PG test and drop an unused local
- `1d55e9f` fix: default the initial claim lease to match the renewal length（独立审查发现 1）
- `384d62c` docs: record the lease-renewal wiring (lease-wire.md §7b) in the task record
- `a8c28e5` fix: fail closed on a falsey malformed tool_calls value [review]（机器人 Code Review 发现）

均已 `git push origin feature/m1-01-restart-recovery`（普通 push，非 force）。PR #30 描述已更新，追加
「租约续期接入 RecoverySession（lease-wire.md §7b）」一节，说明依赖顺序、可选能力接法与验证证据。

## 8. 依赖顺序与未完成项

- **依赖顺序：#35 需先合并**，本分支的续期接入才会真正生效；#35 合并前，本改动对生产行为无影响
  （`getattr` 找不到 `renew_lease`，等价于未改动）。
- 未完成项：第二次 `@codex review`（19:33:26Z）截至本报告完成时未返回，按上文第 5 节处置原则不阻塞；
  如后续返回发现，需要另行核查并处置。
- 合并授权：本任务未获得合并授权，PR #30 状态为「实现完成、CI 通过、独立审查与已返回的机器人审查发现均已
  处置、`CLEAN`/`MERGEABLE`——待用户审核合并」。

## 需用户决定

无。
