# 租约续期（lease renewal）执行报告

- 日期：2026-09-17
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-lease`，分支 `fix/lease-renewal`（自 `origin/main` `b483a12`）
- PR：**#35** https://github.com/kevinWangSheng/production-ops-agent/pull/35（提交 `91451d9` 代码+测试，`96deefa` 任务记录+ROADMAP，`a85b458` 任务记录补 CI 状态）
- 任务记录：`docs/tasks/2026-09-17-lease-renewal.md`；ROADMAP 已加一行当前状态
- 接手时 worktree 干净（`git status` 无改动），Codex 未留下任何未提交改动
- 未向调度者提问；无需用户决定的事项（见末节「需用户决定」为空）

## 1. API

```python
def renew_lease(self, lease: Lease, extend_seconds: int) -> datetime
```

位置：`opspilot/persistence.py`，紧跟 `claim()` 之后、`reserve_budget()` 之前的独立方法（49 行含 docstring），不改 `claim()` 与其它写路径。

- 返回数据库时钟下的新 `lease_until`，供调用方安排下一次续期。
- **续期不换身份**：owner / epoch / control_generation 都不变，调用方继续使用同一个 `Lease` 对象（`Lease` dataclass 未改，避免与 #26/#34/#33 里 `claim()` 构造 `Lease` 的位置参数冲突）。
- `extend_seconds <= 0` → `PersistenceError("INVALID_INPUT")`（与 `reserve_budget` 的 amount 校验同风格）。
- 任一栅栏项不满足 → `PersistenceError("CONTROL_DENIED")`，与 `reserve_budget/commit_step/commit_tool` 一致；`lease_until` 不改动。

## 2. 语义

栅栏（同一事务内，先锁 `opspilot_incidents` 再锁 `opspilot_runs` FOR UPDATE，与其它写路径同序，避免与 `control()/publish()` ABBA 死锁）：

| 项 | 条件 | 依据 |
|---|---|---|
| run 状态 | `state == 'running'` | `publish()` 同项；`waiting_human`/`blocked` 的 Run 不再由 worker 持有 |
| 身份 | `owner == lease.owner and epoch == lease.epoch` | C3 §6「续租和提交均校验执行身份与租约」 |
| 代际 | **事故行** `opspilot_incidents.control_generation == lease.control_generation` | 人工决定只递增事故行；run 行同名列是副本（与 #26 `_lease_revoked` 一致） |
| 撤销 | `lease_until IS NOT NULL` | `control()` 清空 `lease_until` 即收回权限（与 #26 一致，NULL 判撤销） |
| 过期 | `lease_until > now` | 过期即失效，只能 `claim()` 得新 epoch，不能续活 |
| deadline | `deadline > now` | 过 deadline 不续（即使 `claim()` 发的租约本身还没到期） |

新值：`LEAST(GREATEST(lease_until, now + extend_seconds), deadline)`

- 不缩短仍然更长的剩余租约（GREATEST）。
- 永不越过 Run deadline（LEAST）；`claim()` 本身不封顶，续期后会被收回到 deadline 之内。
- 检查与 UPDATE 共用同一个 `_db_now()` 值（审查意见采纳），返回值不会超过已校验的 `now + extend`。

并发：`renew_lease` 与 `claim` 都对 run 行 FOR UPDATE，串行化；claim 先赢则 epoch 不符拒绝续期，renew 先赢则 claim 看到活租约 `LEASE_ACTIVE`。与 `control()` 在 incident 行锁上串行；control 清 `lease_until` 并推进代际，两者都被栅栏拒绝。

### 默认值建议（短租约 + 续期）

**建议 `lease_seconds = 420`，每次 committer 调用前 `renew_lease(lease, 420)`。**

依据（冻结上限见 `docs/testing/first-investigation-v4-2026-09-10.md:69`、`opspilot/investigation/limits.py` on #33）：

- 当前 `run_once` 的 loop 是同步的：`investigator.investigate()` 内一次模型请求最长 `MODEL_REQUEST_TIMEOUT_SECONDS = 360 s`，期间 worker 无法续期。租约必须能撑过一次最长模型请求 + 余量，否则 worker 自己的 `commit_step` 会在模型返回后被自家过期租约拒绝。360 + 60 = 420 s。
- 工具单个 30 s / 累计 240 s，都远小于 420 s；在每次 `begin_round`/`commit_step`/`commit_tool`/`reserve_budget` 之前续期，租约剩余总是 ≥ 420 − 360 = 60 s 以上。
- 效果：硬杀 worker 后接手延迟上限从 1800 s（整个 Run wall）降到 420 s；smoke 的 20 s 只能在没有真实模型调用时成立，不能作为产品默认。
- 若要把接手延迟压到 30 s 量级，需要后台心跳线程续期（模型请求进行中也能续），这属于 #33/#30 侧的新机制，本 PR 不引入，作为后续选项记录。
- 不改冻结的 Run 上限：租约长度不是冻结项，deadline 仍由 `RUN_WALL_SECONDS` 决定，`renew_lease` 封顶于它。
- `DurableStore.claim` 的 API 默认 `lease_seconds=30` 保持不变（现有测试依赖），接线处显式传值。

## 3. 测试

新文件 `tests/integration/test_m1_lease_renewal_postgres.py`（17 用例，`M1_DURABLE_POSTGRES=1` opt-in，模块级 autouse fixture 调 `install()`）：

| 用例 | 覆盖 |
|---|---|
| `test_holder_renewal_extends_without_changing_identity` | 正常续期；身份不变；原 2 s 租约过期后仍能 `commit_step` |
| `test_renewal_never_shortens_a_longer_remaining_lease` | GREATEST 单调 |
| `test_non_positive_extension_is_invalid_input[0/-1]` | INVALID_INPUT，不改 `lease_until` |
| `test_expired_lease_cannot_be_revived_only_reclaimed_with_a_new_epoch` | 过期拒绝；重新 claim epoch+1；旧凭据此后仍拒绝；新凭据可续 |
| `test_human_control_revokes_the_lease_and_renewal_is_refused[cancel/pause/correct/follow_up]` | 人工控制后拒绝；correct/follow_up 后同一 owner 重新 claim 得新代际后可续，旧凭据仍拒 |
| `test_generation_change_with_same_owner_and_epoch_is_refused` | 仅事故代际变化（owner/epoch 相同）也拒绝 |
| `test_renewal_is_capped_at_the_run_deadline_and_refused_after_it` | 续期结果 == deadline；过 deadline 拒绝；随后 claim 得 `DEADLINE_EXCEEDED` |
| `test_deadline_passed_is_refused_even_when_the_lease_itself_is_still_unexpired` | claim 发的 600 s 租约越过 2 s deadline 时，过 deadline 后续期拒绝而不是靠 LEAST「成功」 |
| `test_a_lease_that_reached_the_deadline_is_not_extended_past_it` | 已封顶再续仍停在 deadline |
| `test_a_run_that_is_not_running_is_not_renewed_even_with_a_live_lease[waiting_human/blocked]` | 非 running 拒绝 |
| `test_only_the_holder_can_renew_under_concurrency` | 6 线程并发：持有者 4 次 ok，伪造 owner / 错 epoch 各 4 次 CONTROL_DENIED；到期后另一 worker 接手 epoch+1，旧持有者拒、新持有者可续 |
| `test_renew_lease_locks_the_incident_before_the_run` | 锁序探针（审查者用先锁 run 的模拟证实探针非空过） |

红→绿：`13 failed, 1 passed`（方法不存在，锁序测试因线程先抛错而空过）→ 实现后 `35 passed`（含旧文件 21）→ 补两条后 `38 passed`。

变异测试 9 项（逐条去掉 expiry / generation / owner / epoch / running / deadline 检查、去掉 deadline 封顶、去掉 GREATEST、先锁 run）：初次 7 红 2 存活（running 检查、deadline 检查），补上表两条测试后 **9/9 转红**。

## 4. make check / PG 结论（采纳审查意见后的最终版本）

```
.venv/bin/ruff check .            -> All checks passed!
.venv/bin/ruff format --check .   -> 412 files already formatted
.venv/bin/mypy                    -> Success: no issues found in 13 source files
.venv/bin/python -m pytest        -> 1050 passed, 92 skipped, 2 xfailed in 29.71s
```

PG 定向（lab 由 `M0_ENV_FILE=... .venv/bin/python -m scripts.m0.postgres_lab start` 启动，密钥未打印，收尾已 `stop`）：

```
M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_lease_renewal_postgres.py tests/integration/test_m1_durable_state_postgres.py -q
-> 38 passed in 13.04s
```

费用：0（无模型调用）。

## 5. 独立审查

全新上下文只读子代理，给定 C3 §6/§7、PRODUCT-CONSTRAINTS、现有栅栏与锁序不变量，未给实现者结论。结果：**无阻塞发现**，4 条 P3 全部采纳并复验：

1. docstring「不缩短」措辞不严：封顶到 deadline 时会缩短 → 已改为「除封顶到 deadline 外不缩短」。
2. 检查用 `_db_now()`、UPDATE 用 `clock_timestamp()` 两个时钟值 → UPDATE 改为传入同一 `now`。
3. `install()` 只在第一个用例调用，`-k` 单跑依赖顺序 → 加模块级 autouse fixture。
4. `deadline_seconds=2/3, lease_seconds=1` 在慢 CI 上可能先过期 → 放宽为 4 s / 2 s 与 3 s。

审查者另行验证：三方 `git merge-file` 与 `chore/durable-store-hardening`（#26）、`fix/claim-state-guard`（#34）**均 0 冲突**，合并结果可 `py_compile`；伪造 incident_id 的 Lease 因 JOIN 条件取不到行而拒绝。

## 6. CI 与机器人审查

- HEAD `96deefa`（代码内容与最终 HEAD 相同，后一提交仅文档）：`checks` pass 55 s，`m0-postgres` pass 46 s。`m0-postgres` 以 `M1_DURABLE_POSTGRES=1` 跑 `tests/integration`：`54 passed, 38 skipped`，包含新文件。`mergeStateStatus=CLEAN`，`mergeable=MERGEABLE`。
- HEAD `a85b458`（docs-only，最终 HEAD）：`checks` pass 53 s，`m0-postgres` pass 43 s；`mergeStateStatus=CLEAN`，`mergeable=MERGEABLE`。
- Codex 机器人：Code Review 状态 **Failed**（未返回任何发现，无 inline comment，无 thread）；Security Review 最终也是 **Failed**（两者均未返回任何发现，PR inline comment 数 0）。按 AGENTS.md 机器人安全审查不是交付门槛；Code Review 未产出结果，以本报告第 5 节的全新上下文独立审查作为审查闭环，并已在任务记录中披露。
- 合并授权：本任务未授权合并，PR 状态为「已就绪，待用户审核合并」。

## 7. 两处接线建议（不在本 PR 实施）

### 7a. PR #33（`feature/m1-01-progress-ui`）

**`opspilot/web/service.py:522-562` `Workbench.run_once`**：把租约默认从 Run wall 改为短租约。

```diff
@@ opspilot/web/service.py:527
-        lease_seconds: int | None = None,
+        lease_seconds: int | None = None,
     ) -> LoopOutcome | None:
         """Claim the current Run, execute one attempt, publish or hand off.
@@ opspilot/web/service.py:532-536
-        ends with the lease released and a ``run_completed`` or
-        ``run_handoff`` event. ``DurableStore`` has no lease renewal, so the
-        lease defaults to the Run wall (``run_seconds``): a lease shorter
-        than the attempt would fence the attempt's own commits.
+        ends with the lease released and a ``run_completed`` or
+        ``run_handoff`` event. The lease is short (``LEASE_SECONDS``) and is
+        renewed before every committer call; it must outlast one maximal
+        model request because the loop cannot renew while a request blocks.
@@ opspilot/web/service.py:548
-        seconds = int(self.run_seconds) if lease_seconds is None else lease_seconds
+        seconds = LEASE_SECONDS if lease_seconds is None else lease_seconds
```

并在 `opspilot/investigation/limits.py` 加（或在 service.py 顶部定义）：

```python
# 租约须撑过一次最长模型请求（loop 同步，请求中无法续期）再留余量；
# 每次 committer 调用前续期，硬杀后接手延迟上限由 1800 s 降为本值。
LEASE_SECONDS = int(MODEL_REQUEST_TIMEOUT_SECONDS) + 60  # 420
```

**`opspilot/investigation/store.py:141-170` `DurableStepStore`**：每个转发方法先续期（续期失败的 `CONTROL_DENIED` 走既有 `StepStoreError` → `run_once` 的 handoff 路径，无需新分支）。

```diff
@@ opspilot/investigation/store.py:144
-    def __init__(self, store: DurableStore, lease: Lease) -> None:
+    def __init__(
+        self, store: DurableStore, lease: Lease, *, renew_seconds: int = 420
+    ) -> None:
         self._store = store
         self._lease = lease
+        self._renew_seconds = renew_seconds
+
+    def _renew(self) -> None:
+        # 续期与提交同一栅栏（C3 §6）；被拒即本次尝试已失去 Run，停止提交。
+        try:
+            self._store.renew_lease(self._lease, self._renew_seconds)
+        except PersistenceError as exc:
+            raise StepStoreError(str(exc)) from None
@@ opspilot/investigation/store.py:152
     def reserve_budget(self, reservation_id: UUID, amount: int) -> None:
+        self._renew()
         try:
@@ opspilot/investigation/store.py:158
     def commit_step(self, logical_key: str, response: Mapping[str, Any]) -> UUID:
+        self._renew()
         try:
@@ opspilot/investigation/store.py:164
     def commit_tool(
         self, step_id: UUID, ordinal: int, result: Mapping[str, Any]
     ) -> None:
+        self._renew()
         try:
```

若 #31 的 `begin_round`/`assert_current` 已落到该类，同样在 `begin_round` 开头加 `self._renew()`（每轮模型请求前续期是最关键的一次：它保证请求期间租约 ≥ 420 s）。

`opspilot/web/store.py:49-100` 的 `IncidentStore` Protocol 与 `:249` `DurableIncidentStore` 无需改动（续期封装在 `DurableStepStore` 内）；`_EmittingCommitter`（`service.py:99`）透明转发，也无需改。内存替身 `MemoryStepStore`（`investigation/store.py:60`）无租约概念，不需变更。

smoke 路径 6 复验时把 `lease_seconds=20` 去掉，用默认值，预期硬杀后接手延迟 ≤ 420 s。

### 7b. PR #30（`feature/m1-01-restart-recovery`）

**`opspilot/worker.py:15-46` `RecoverySession`**：`_assert_current()` 现在是只读检查（`lease_current`），改为「续期即检查」——续期成功本身就证明 owner/epoch/代际/未过期/未过 deadline。

```diff
@@ opspilot/worker.py:15-28
 @dataclass(frozen=True)
 class RecoverySession:
     plan: RecoveryPlan
     lease: Lease
     store: DurableStore
+    renew_seconds: int = 420
 
     def _assert_current(self) -> None:
-        if not self.store.lease_current(self.lease):
-            raise PersistenceError("CONTROL_DENIED")
+        # renew_lease 过的是与提交相同的栅栏；成功即当前，并把租约延到
+        # 足以覆盖下一个工具调用（单工具 ≤ 30 s）。失败直接抛 CONTROL_DENIED。
+        self.store.renew_lease(self.lease, self.renew_seconds)
         current = self.store.rebuild(self.plan.incident_id)
```

`execute_pending`（`worker.py:30-41`）每个工具前已调用 `_assert_current()`，改动后自然获得续期，无需另改。`Worker.claim`/`Worker.resume`（`worker.py:60-76`）的 `lease_seconds: int = 30` 建议改为 `420` 与 #33 一致；`resume` 里 `plan.control_generation != lease.control_generation` 时的 `abandon(lease)` 保持不变。

注意 `lease_current` 在 #30/#33 上都存在，可保留给不想延长租约的纯读检查；只是 `RecoverySession` 里既要检查又要执行工具，续期是更强的保证。

## 8. 与 #26 / #34 的冲突点

- **文本冲突**：审查者三方 merge-file 对 #26、#34 均为 0 冲突。本方法插在 `claim()` 的 `return lease` 与 `def reserve_budget` 之间，这两处上下文行 #26/#34 都未改动。
- **#26（`chore/durable-store-hardening`）语义**：其 `_lease_revoked(row, lease, now)` 判定 = owner/epoch/事故代际/`lease_until IS NULL`/过期/过 deadline，与本方法栅栏逐项相同。#26 合并后可把本方法的 if 体收敛为 `not row or row["run_state"] != "running" or self._lease_revoked(row, lease, now)`（本方法的 SELECT 已经 alias 了 `incident_generation`，直接兼容）。这是可选清理，不合并也不会出错。
- **#34（`fix/claim-state-guard`）语义**：只重排 `claim()` 内的检查顺序（人工决定先于版本判定，blocked 的 Run 不再由 claim 改写），不触及续期；无交互。
- **#33 的 `lease_current`/`abandon`**：都在 `reserve_budget` 之后插入，本方法在 `reserve_budget` 之前，位置不重叠；`abandon` 只清 `owner/lease_until`，续期后 abandon 仍能按 owner/epoch/代际匹配清掉。
- **#31 的 `opspilot_inputs`/`begin_round`**：未读该分支的 persistence 改动（未确认），但本方法不依赖新表新列。

## 需用户决定

无。

## CI 结果

最终 HEAD `a85b458`：`checks` pass、`m0-postgres` pass，`CLEAN` / `MERGEABLE`。PR #35 状态：**已就绪，待用户审核合并**。PG lab 已 `stop`，worktree 干净。
