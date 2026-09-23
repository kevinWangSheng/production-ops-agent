# M1-01 全 PR 重建集成与终验报告（integration/m1-01-full-r2）

- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-integration-full`
- 起点：`origin/main` @ `b483a1243e16737d492e22dd02acfc65decffd7b`（干净重建，不基于上午的 `integration/m1-01-full`）
- 最终 HEAD：`846db18adf00f6e9165b7e90319dc26c4ae1b2bc`
- 已推送到 `origin/integration/m1-01-full-r2`（仅分支，未开 PR，未合并 main，未合并任何 PR）

## 0. 关于 PR #30 head 变化（用户已预告）

用户在派发本任务时提示 PR #30 可能在我合并后才推送新提交。我在完成全部 13 个 PR 首轮合并、`make check` 通过后，
按要求 `git fetch` 一次并用 `gh pr list` 复核全部 open PR 的 `headRefOid`：只有 **#30 变了**
（`bd47a9fec8ab9f5eb314588c8c33d4a95c066239` → `a8c28e5c54d977aeda37bed47de094c2334014d3`，新增 5 个提交，
主题是把 `Worker.resume()/RecoverySession` 接到 PR #35 的 `renew_lease`，即 lease-wire.md §7b 的接线），
其余 12 个 PR head 未变。已额外 `git merge --no-ff origin/feature/m1-01-restart-recovery` 合入这个增量
（提交 `846db18`，无冲突），并在此之后重跑 `make check`、PG 定向测试、`make acceptance`。**下表「合入 sha」列的
#30 即为这个最终 head。**

## 1. 合入的全部 13 个 PR（严格按任务书顺序）

| 顺序 | PR | 分支 | 使用的 head sha | 合并提交 sha | 冲突 |
|---|---|---|---|---|---|
| 1 | #27 | `chore/instruction-contract-impl` | `cf8f17d817e9baef2ff6c82b0fdb6679d628fa6f` | `c230446` | 无（干净 main 起点，无竞争内容） |
| 2 | #34 | `fix/claim-state-guard` | `f67446e28892188866de0a318f2e2a901cd0b350` | `54ebaf5` | 1 处，测试文件尾部追加（机械） |
| 3 | #35 | `fix/lease-renewal` | `a85b458355d7a99fb4b302acb391c073f8066997` | `a506ecc` | 无 |
| 4 | #36 | `fix/pg-live-probe-flake` | `080c62cc417965624032bebc32bbabbac476d75d` | `ac71bf1` | 无（同时修复了上午 integ 报告记录的 flake） |
| 5 | #26 | `chore/durable-store-hardening` | `8ff588068887d7560c41a7b7245fb54fbc52ebe6` | `b3f057d` | 2 处，含 `claim()` 真实语义冲突（详见 §2.1） |
| 6 | #21 | `feature/m1-01-intake-auth` | `5e581ee6d2447a469df4de736e7612692cf24736` | `b680ee1` | 1 处，测试文件尾部追加（机械） |
| 7 | #20 | `feature/m1-01-tool-executor` | `aff4586a5d67432bffecf165a9d9c395a80b0171` | `157a669` | 无 |
| 8 | #28 | `feature/m1-01-human-control` | `49a51755a9baa7e3424e5ca8047c17c847727171` | `7ce3326` | 1 处，`ROADMAP.md` |
| 9 | #30（首轮） | `feature/m1-01-restart-recovery` | `bd47a9fec8ab9f5eb314588c8c33d4a95c066239` | `15790e1` | 2 处，含 `rebuild()["pending_tools"]` 真实语义冲突（详见 §2.2） |
| 10 | #29 | `feature/m1-01-investigation-loop` | `26d0c70cd29e8a2cda64a50e4b9483f46e214966` | `1918170` | 6 处，含 3 处 add/add 与 2 处 `persistence.py` 真实语义冲突（详见 §2.3） |
| 11 | #31 | `feature/m1-01-control-completion` | `865eebfa01eddf7d7595d0aa9053e54efa53d3ac` | `2283a91` | 4 处，含 `claim()`/`commit_step` 相关真实语义冲突（详见 §2.4） |
| 12 | #32 | `feature/m1-01-acceptance` | `9626a9e003854541b60e0f6997c9d28b863b48b8` | `4c5e992` | 2 处（详见 §2.5），另发现并修复 1 处接缝 bug（`549eb7a`） |
| 13 | #33 | `feature/m1-01-progress-ui` | `f6decd6ca6f0a8c40c54056d1bed78d5580abc94` | `10a3ba6` | 1 处，`ROADMAP.md`；另发现并修复 1 处接缝 bug（`4765978`） |
| — | #30（增量） | `feature/m1-01-restart-recovery` | `a8c28e5c54d977aeda37bed47de094c2334014d3` | `846db18` | 无（fetch 后按用户提示补合） |

`make check`/`make acceptance`/PG 定向测试均在合入 `846db18` 之后的最终状态上运行，不是中间态。

## 2. 冲突清单与处置

规则复述：代码冲突按双方语义合并、保留双方测试；`ROADMAP.md`/`docs/tasks/*` 类状态文档双保留、按日期排序；无法确定语义时停止记录到「阻塞」——本次全部冲突均判断为语义明确，无阻塞项。

### 2.1 PR #26（durable-store-hardening）× 已有基线 —— `claim()` 真实语义冲突

- **文件**：`opspilot/persistence.py`，`DurableStore.claim()`。
- **双方来源**：HEAD 侧已含 #34（`fix/claim-state-guard`）对 `claim()` 的重写——用 `r.*` 通配 SELECT，检查顺序为
  「incident_state 终态优先于版本判定」（修正 main 上「版本事故会顶掉人工决定」的旧 bug）。#26 侧独立重写了同一函数——
  改用显式列名 + `r.state AS run_state` 别名（避免 `SELECT r.*,i.control_generation` 时 `control_generation` 因同名
  被 `dict_row` 静默去重取到错误值，这正是 #26 标题里的「A 类确定性缺陷」），但检查顺序仍是 main 上「版本判定优先」的旧序，
  因为 #26 基于 main、不知道 #34 的修复。
- **处置**：采用 #26 的显式列 SELECT（避免通配符缺陷），同时保留 #34 的检查顺序（human-control-first）与「已 blocked 不静默恢复」
  分支；把 HEAD 侧遗留的 `row["state"]`（`r.*` 通配下的裸键）改写为 `row["run_state"]`（配合新 SELECT 别名），否则会在
  `#26` 的新 SELECT 下产生 `KeyError`——这是两侧非冲突区域自动合并后残留的不一致，已一并修正并跑通
  `tests/test_architecture.py::test_persistence_sql_never_selects_a_qualified_star`（#26 自带的回归测试，专门检测
  `SELECT <alias>.*` 写法）确认无遗漏。
- **文件**：`tests/integration/test_m1_durable_state_postgres.py`——尾部追加冲突，双方各自新增的测试函数（HEAD 侧 #27/#34
  的 `_prompt_versions`/`test_prompt_revision_change_...`/`test_version_change_cannot_override_...`；#26 侧 9 个
  代际栅栏/租约/索引测试）互不重叠，全部保留。
- 修复提交：`b3f057d`。

### 2.2 PR #30（restart-recovery，首轮）× 已有基线 —— `rebuild()["pending_tools"]` 真实语义冲突

- **文件**：`opspilot/persistence.py`，`rebuild()` 的 `pending_tools` 列表推导式。
- **双方来源**：HEAD 侧（#28）加了 `status in {"response_committed","tool_result_committed"}` 过滤（排除 `late_result`
  历史行，避免迟到证据被当作待办重新派发）。#30 侧把工具计划长度/取值从裸字典访问
  `(step["response"] or {}).get("tool_calls", [])` 改成新引入的 `_tool_plan()` 辅助函数（同时兼容 loop 提交的
  `{"assistant": {...tool_calls}}` 形状与 M0 旧 harness 的顶层形状——这正是 ui-e2e 交接项里提到的
  「`pending_tools` 恒空」缺陷的修复）。
- **处置**：两者正交，合并为同时使用 `_tool_plan()`（#30）与 `status` 过滤（#28）。若只取一侧：只取 #28 会在真实
  loop 提交的响应形状下漏算工具计划长度（`pending_tools` 恒空，Worker 恢复时无计划可续）；只取 #30 会让
  `late_result` 历史重新被当作待办。
- 修复提交：`15790e1`（同一提交内完成，未单独拆分接缝修复提交，因为这是初次合并即遇到的冲突而非事后测试发现）。

### 2.3 PR #29（investigation-loop）× 已有基线 —— 6 处冲突，含 3 处 add/add 与 2 处真实语义冲突

- **`opspilot/instructions/__init__.py`、`opspilot/instructions/discipline.py`、`tests/test_instruction_discipline.py`**
  （3 处 add/add）：#27 与 #29 各自独立创建了同名新模块（都在解决「L1 指令收敛」问题）。#29 自己的
  `opspilot/investigation/loop.py`（本分支）第 76-88 行的 docstring 明确写着：「discipline.template_projection
  in this branch only hashes LAYER_TEMPLATE segments... PR #27, not yet merged, already fixes this upstream...
  deferred to the #27 -> #29 merge」——#29 作者自己承认并预告了这次合并该以 #27 为准。逐函数名比对确认 #27 版本
  （346 行实现 / 742 行测试）是 #29 版本（267 行 / 516 行）的严格超集（#29 的每个测试函数名都能在 #27 里找到同名版本，
  #27 另有 `test_variants_mapping_is_read_only`、`test_face_hash_...` 等 #29 没有的补充）。**采用 #27 版本整体**，
  丢弃 #29 的重复实现。
- **`opspilot/persistence.py`**（2 处）：
  1. 新方法追加位置冲突：HEAD 侧（#30）的 `_tool_plan()` 与 #29 侧新增的 `_SETTLEMENTS` 字典（预留结算目标映射）
     加在同一位置，互不相关，两者都保留。
  2. `commit_step()` 的「fenced 回复不丢弃」实现冲突：HEAD 侧（#26/#28 已收敛）用共享的 `self._late_result()`
     辅助方法（按保留前缀 key 做 `ON CONFLICT (run_id, logical_key) DO NOTHING` 去重）+ 显式 `conn.commit()` 后
     `raise`；#29 侧独立实现了同一目标，但用内联 SQL + 每次生成新 `uuid4()` 后缀的 key（**不去重**，重试会不断插入
     新的 late_result 行）+ `fenced` 标志延后到 `with` 块外 `raise`。采用 HEAD 侧的收敛实现（更安全、已去重），
     并确认 #29 自带的回归测试 `test_a_fenced_model_reply_is_retained_as_late_result_history` 只断言可观察的
     `late_result` 数量与内容，两种实现下都能通过。
- **`opspilot/investigation/loop.py`、`opspilot/investigation/store.py`**：这两个文件在 #29 内部无冲突（#31 合并时才与之冲突，见 §2.4）。
- **`tests/integration/test_m1_durable_state_postgres.py`**：同样是尾部追加冲突，HEAD 侧全部测试保留，追加 #29 的
  2 个预算结算测试。
- 修复提交：`1918170`。

### 2.4 PR #31（control-completion）× 已有基线 —— 4 处冲突

- **`ROADMAP.md`**：确认 #31 相对自己的 base（`integration/m1-01`）对本文件**无净变化**，冲突纯粹是三方合并基准点
  不同导致；保留 HEAD 累积的状态条目。
- **`opspilot/investigation/store.py`（`DurableStepStore`）**：HEAD 侧（#29）新增的 `_attempt_reservation()`
  （按 epoch 给预算预留 id 加命名空间）与 #31 新增的 `assert_current()`/`begin_round()`（`StepCommitter` 协议扩展）
  加在同一位置，互不相关，两者都保留。
- **`opspilot/investigation/loop.py`（`_call_model`）**：#31 自己的分支没有 #29 的「按结算结果 settle 预算」功能，
  它的 diff 因此把 #29 依赖的具名变量 `reservation`（后面 `self._settle(request.run_id, reservation, "unknown")`
  要用）连带删掉了。保留具名变量（#29）+ 追加 #31 的 `self.store.assert_current()` 校验——两者缺一不可，合并后
  才是正确版本。
- **`opspilot/persistence.py`（`claim()`）**：把已收敛的 incident_state/run_state 检查顺序（#26 SQL 加固 + #34
  人工优先修复）与 #31 新增的全局/目标级 suspension 检查（`opspilot_scope_controls`/`opspilot_target_suspensions`）
  合并；#31 是基于 #26/#34 之前的基线写的，因此不需要与它们协调。**原样保留了 #31 的 suspension 判断条件**，
  包括其中一句看起来恒假的冗余子句（`row.get("target_generation",0)==0 and row.get("target_suspended",False)`——
  当前一个 `row["target_suspended"]` 已经是 `False` 时这句必然也是 `False`），因为这是 #31 自己的逻辑、不是本次
  合并引入的，已记录到 §5 交给 #31 owner 确认。
- 修复提交：`2283a91`。

### 2.5 PR #32（acceptance）× 已有基线 —— 2 处冲突 + 1 处接缝 bug

- **`scripts/m1_live_flash_loop.py`**：确认 HEAD 侧与 #29 原始版本逐字节相同（无其它已合并 PR 碰过这个文件），
  #32 是对 #29 该文件的连贯演进（拆出 `build_run()`、接入 `opspilot.acceptance`、按 Run 分目录存证据、收紧凭据
  只读 `M0_ENV_FILE`）。**整体采用 #32 版本**而非手工拼接。
- **`tests/m1_investigation_support.py`（`assemble()`）**：采用 #32 新增的共享 fixture `historical_window_context()`
  替换手写的 `evidence_context` 字典，符合该 fixture 自身文档「避免直播脚本/loop 替身/web 替身三处独立漂移」的
  设计意图。
- **接缝 bug（合并本身无冲突，全量测试跑出来的）**：`historical_window_context()` 缺少
  `"all_authorized_targets": True`。它在 #32 自己开发时依据的 eligibility 检查下是对的（`target_refs` 缺省即
  视为覆盖全部 target），但 #29 后续收紧了 `opspilot/investigation/reports.py` 的判定（`fix: reject a time
  policy scoped to no targets instead of every target [#F3]`，已在本分支合入），要求显式声明覆盖范围。两者组合后
  每条引用 `policy-window-1` 的事实都绑定失败 → `REPORT_INVALID`，5 个测试转红。已在 #32 自己分支的独立 worktree
  里复现确认**这些测试在 #32 自己的 head 上原样通过**（证明不是 #32 单独的缺陷），加回该字段后全量转绿
  （1531 passed）。
- 修复提交：合并 `4c5e992`，接缝修复单独提交 `549eb7a`。

### 2.6 PR #33（progress-ui）× 已有基线 —— 1 处冲突 + 1 处接缝 bug

- **`ROADMAP.md`**：同 §2.4，双保留按日期排序。
- 其余全部（含 `opspilot/investigation/store.py`、`opspilot/persistence.py`，#31 的 `begin_round`/`assert_current`
  与 #35 的 `renew_lease` 都会经过这里）**自动合并无冲突**。
- **上午 integ.md 报告的发现已在 #33 上游修复**：`tests/m1_web_support.py::_MemoryCommitter` 在本次的 #33 head
  （`f6decd6`）上已经自带 `begin_round`/`assert_current`，无需再本地打补丁。
- **接缝 bug（同 §2.5 的根因，不同文件）**：`tests/m1_web_support.py::ScriptedInvestigator` 手写的
  `evidence_context` 同样缺 `all_authorized_targets: True`，导致 `tests/test_m1_web_workbench.py` 9 个用例
  `REPORT_INVALID`。同样的字段修复后全部转绿（1558 passed）。
- 修复提交：合并 `10a3ba6`，接缝修复单独提交 `4765978`。

## 3. `make check` 结论

最终 HEAD（`846db18`，含 #30 增量）上：

```
================ 1558 passed, 137 skipped, 2 xfailed in 39.45s =================
```

退出码 0；`doctor`/`uv lock --check`/`ruff check`/`ruff format --check`/`mypy`（39 source files）全部通过。
过程中每合入一个 PR 都单独跑过 `mypy` + `ruff` + 全量 `pytest`（非 PG 部分），未出现除本报告已记录的 5 处冲突与
2 处接缝 bug之外的其它红。

## 4. PostgreSQL 定向测试

- 用 `M0_ENV_FILE=/Users/shenghuikevin/dev/AI/production-ops-agent/.env .venv/bin/python -m scripts.m0.postgres_lab start`
  启动本 worktree 专属本地 PG lab（`tmp/m0-b/postgres`，端口 55431，仅本机 socket，密钥/DSN 由脚本自行处理，
  未打印）。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`：

  ```
  83 passed, 54 skipped in 58.30s
  ```

  54 个跳过全部是 M0 系列测试要求的其它 opt-in 环境变量（未设置，任务书未要求设置）；M1-01 相关的 6 个 PG 测试
  文件（`test_m1_durable_state_postgres.py`、`test_m1_control_completion_postgres.py`、
  `test_m1_lease_renewal_postgres.py`、`test_m1_lease_renewal_wiring_postgres.py`（含 #30 增量新加的
  接线测试，因 #35 已合入而不再自跳过）、`test_m1_tool_budget_postgres.py`、`test_m1_web_postgres.py`）单独重跑
  确认 **83 passed / 0 skipped**——全部因 `M1_DURABLE_POSTGRES=1` 而应执行的用例均已执行且通过。
- 测试后 `... postgres_lab stop`，数据目录保留（脚本设计如此），服务进程已确认关闭。

## 5. `make acceptance` 结论（PR #32 验收入口）

```
| scenario | feature | evidence level | result |
|---|---|---|---|
| normal-report | F3 | deterministic loop + read-only evidence | PASS |
| fault-report | F3 | deterministic provider failure handoff | PASS |
| budget-refusal | F2 | deterministic budget refusal | PASS |
| deadline-refusal | F2 | deterministic deadline refusal | PASS |
| pause-cancel | F2/F12 | durable human control projection | PASS |
| late-result | F2/F12 | stale completion rejection | PASS |
| worker-restart | F2/F8 | committed evidence survives worker restart | PASS |
| incompatible-state | F2/F8 | incompatible state blocked handoff | PASS |
| real-deepseek-records | F3 | recorded real Runs: projection equals ledger | PASS |
| real-deepseek-report | F3 | one recorded real Run: bound report, no handoff | PASS |
```

10/10 PASS（全部确定性场景，含两条「recorded real Runs」是回放已保存的历史真实 Run 记录，本次未发起新的真实模型调用）；
随附的 `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks` → `SECRET_SCAN_PASSED`。

## 6. UI smoke 路径 6（重启恢复）在默认租约配置下的复验

- 参考材料：`/private/tmp/claude-501/.../scratchpad/reports/ui-e2e.md`（另一 agent 在
  `production-ops-agent-integration-full-ui` worktree 对 PR #33 做的 7 条路径端到端 smoke，路径 6 用的是
  `lease_seconds=20` 的加速配置）与该 worktree 下 `scripts/tmp_ui_worker.py`（未提交的临时 fixture worker）。
- 本次复验**只做路径 6**，且**不传 `lease_seconds` 覆盖**，走 `Workbench.run_once()` 的生产默认值——由于
  `DurableIncidentStore.renewal_supported`（检测到 #35 的 `renew_lease`）为真，默认值是 `LEASE_SECONDS =
  int(MODEL_REQUEST_TIMEOUT_SECONDS) + 60 = 420` 秒。
- 工作区：本 worktree 专属 PG lab（同 §4，复用同一端口 55431，先确认无其它进程占用）；
  `python -m opspilot.web serve` 绑定 `127.0.0.1:18034`；临时 fixture worker 脚本仿照
  `tmp_ui_worker.py` 改写（去掉 `lease_seconds` 覆盖，补上 `all_authorized_targets: True` 修复），
  未提交，存于本次会话 scratchpad（`.../scratchpad/ui-smoke/`），不进任何 git 仓库。
- 步骤与结果：
  1. `POST /intake/ui` 提交事故 `5de12f39-3252-56de-8395-1437b1cec963` → `201`，run `8e54d435-…`。
  2. worker1（`UI_WORKER_DELAY=30`）claim 成功进入工具轮 2 秒后 `kill -9`；确认进程已死。
  3. 页面 `run-state: running`，`epoch: 1`（租约仍持有中，Run 未被标记失败）。
  4. worker2 立即重试 claim → `attempt_done outcome: null`（`LEASE_ACTIVE` 静默拒绝，不写
     `run_claim_refused` 事件——对应 ui-e2e.md 记录的 `e958c5a` 修复，在本分支上行为一致）。
  5. 真实等待 420 秒（用真实系统时钟，非 FakeClock；等待期间无其它操作）直到租约到期。
  6. worker3 重新 claim → 成功，`fixture_outcome execution: completed`，`attempt_done outcome: completed`。
  7. 页面最终状态：`run-state: completed`，`epoch: 2`（由 1 递增），`concluded`，`Facts (1)`——与 ui-e2e.md
     用 20 秒加速租约观察到的机制完全一致，证明该机制在**生产真实默认租约时长**下同样成立，不是被加速配置
     意外掩盖的行为。
  8. 清理：web 服务、PG lab 均已停止确认；`pgrep` 确认无残留 worker/web/PG 进程；本 worktree
     `git status` 干净（smoke 脚本与产物全在 session scratchpad，未进仓库）。
- 备注（与 ui-e2e.md 一致）：本条路径走的是 `Workbench.run_once()` 重新 `claim()` 从头再跑一次 loop
  （#31 的 `g{generation}:e{epoch}` 键让旧 epoch 步骤只作历史），**不是** PR #30 的
  `Worker.resume()/RecoverySession`（续接已提交工具计划）路径；两者在本分支上并存但 web 层未接 #30，
  这是 ui-e2e.md 已经记录、本次复验未改变结论的已知交接项（见 §7 第 2 条）。

## 7. 需要交给具体 PR owner 的问题清单

1. **#31 owner**：`opspilot/persistence.py::claim()` 里 suspension 判断的第三个子句
   `row.get("target_generation", 0) == 0 and row.get("target_suspended", False)` 在其前一个子句
   `row["target_suspended"]` 已经参与同一个 `or` 时恒为假（如果 `target_suspended` 为真，前一子句已经短路；
   如果为假，`row.get("target_suspended", False)` 也为假）——本次合并未改动其语义，原样保留，但建议确认是否
   为笔误（也许原意是判断别的字段，如 `global_generation` 不匹配的情形）。
2. **#30 × #33 owner（协商）**：web 层 `Workbench.run_once()` 不使用 `Worker.resume()/RecoverySession`，硬杀后
   的续接是「重新 claim 并从头跑一次 loop」，已提交但未执行完的工具计划不会走 `execute_pending` 续接
   （ui-e2e.md 已记录，本次 §6 复验在默认租约下确认现状不变）。需要决定 worker 组合层最终归属谁实现。
3. **#20/#26/#29 owner（协商，预算结算）**：ui-e2e.md 记录 `opspilot_runs.budget_spent` 从未在完成的 Run 上
   写入（`reserve_budget` 只增加 `budget_reserved`，虽然 #29 新增了 `settle_budget`/`_SETTLEMENTS`，但
   web 层的 `run_once` 路径未调用它——本次未逐行复核 web 路径是否已接线，转记于此供相关 owner 确认，因为这
   超出本任务的合并范围）。
4. **#33 owner（记录用，非缺陷）**：对非 web intake 创建的事故（无 `intake:` 账本行），`run_once` 会抛
   `INCONSISTENT_STATE`；轮询型 worker 需要按 `intake_key` 前缀自行过滤（本次临时 smoke worker 已同样处理）。

（追问文本权威位置——上午 integ.md 报告 §5.2/§7 第 1 条的发现——已由 #33 commit `65843df` 解决：
`IncidentStore.control(..., payload)` 已按 `DurableStore.control` 签名检测并转发，`Workbench._apply` 传
`{"text","channel":"web"}`，本次已在代码层核实签名与调用链一致，且全量测试含
`test_a_note_applied_after_the_claim_fences_the_attempt_instead_of_being_ignored` 通过，视为已关闭，不再重复列入问题清单。）

## 8. 建议的合并顺序

沿用上午 integ.md 报告的整体思路，结合本次发现更新如下：

1. **#36、#27、#34、#35**（均 base=main，互相独立，零冲突）：可最先合并，无先后约束。**#27 优先于 #29**——
   #29 自己的代码已经预告并依赖 #27 的 `discipline.py` 收敛版本（§2.3），#29 合并前应先有 #27，否则
   `prompt_revision`/`template_projection` 的语义会短期倒退到 #29 的旧版本。
2. **#26**：依赖顺序上应晚于 #34（`claim()` 语义合并需要 #34 的检查顺序修复打底，见 §2.1），但 #26 本身
   base=main、可独立合并；建议顺序为 #34 → #26。
3. **#21、#20**：与其它 PR 无交集，可在 #26 之后任意时点合并。
4. **#28、#30、#29、#31**（stacked 于 `integration/m1-01`）：
   - #30 与 #29 之间无强依赖，但 #30 的 `_tool_plan()` 是 #29 之后众多 loop 语义（`pending_tools` 非空）
     的前提，建议 #30 先于或紧邻 #29。
   - **#31 应晚于 #29**：#31 自己的 `_call_model` 分支没有 #29 的预算结算变量 `reservation`，#31 独立合并到
     main 时若 #29 已先合并，#31 的 PR 分支本身需要 rebase 补上这处修复（本次集成分支已经手工合并解决，但
     PR 各自独立合并到 main 时这处会重新出现，需 #31 owner 在 rebase 时留意）。
5. **#35 应先于 #30/#33 的「接线」代码在生产 main 上生效**：#30（`RecoverySession._renew`）与 #33
   （`Workbench.run_once` 的 `renewal_supported`）都是通过 `getattr(store, "renew_lease", None)` 做的
   「可选能力」接入，#35 缺席时是 no-op、不改变现有行为，因此 #35 与 #30/#33 的合并先后顺序在功能上不是
   硬阻塞，但只有 #35 合并后 #30/#33 的续期能力才会真正生效——建议 #35 尽早合并以缩短「接线已写但未生效」的窗口。
6. **#32、#33 最后合并**，且合并前需先应用本报告 §2.5/§2.6 的 `all_authorized_targets` 接缝修复（或等价方案）——
   这处修复目前只存在于本集成分支，**尚未反馈进 #32/#33 各自的 PR 分支**，若不处理，#32/#33 各自独立合并到
   main（在 #29 已合并的前提下）时会各自复现这 5+9 共 14 个 `REPORT_INVALID` 失败。

## 9. 边界确认

- 未合并任何 PR 到 main，未碰 main（全程操作 `integration/m1-01-full-r2` 分支）。
- 未删除/stash/改写其它 worktree 内容；`production-ops-agent-integration-full-ui`、
  `production-ops-agent-integ-record`、`production-ops-agent-integration-full-store` 等其它 worktree
  仅做只读参考（读取 `docs/tasks/*`、`reports/ui-e2e.md`、`scripts/tmp_ui_worker.py` 作为实现参考），
  未写入、未删除。
- 未做重构、未改验收步骤、未改 `feature_list.json` 的 `passes`、未新增依赖（#33 自带的 web 依赖组是该 PR
  自身内容）。
- 未对外发起新的真实模型调用；`make acceptance` 的两条「real-deepseek」场景是回放已保存记录，UI smoke 全程
  用 fixture 替身。
- PG lab、web 服务、临时 worker 进程均已确认停止/清理；本 worktree `git status` 干净。

---
报告生成时未执行任何破坏性操作。分支 `integration/m1-01-full-r2` 已推送到
`origin/integration/m1-01-full-r2`，未创建 PR，未合并到 main。
