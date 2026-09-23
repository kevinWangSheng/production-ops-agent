# M1-01 全量集成分支验证报告（integration/m1-01-full）

- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-integration-full`
- 起点：`origin/integration/m1-01` @ `5249c667db8798bf68be37827c81d5ce05da374a`（已含 #20 #21 #26 #28 #29 #30）
- 最终 HEAD：`e42d7b72cac10044a059578f9fb7f410807e5d5a`
- 已推送到 `origin/integration/m1-01-full`（仅分支，未开 PR）

## 1. 合入的四个分支（按任务书顺序 #27 → #31 → #32 → #33）

| PR | 分支 | 合并时使用的远端 head sha | 合并提交 sha | 备注 |
|---|---|---|---|---|
| #27 | `origin/chore/instruction-contract-impl` | `090a63c38a57da975bc1b666d267a38f66fb4fa` | `872e702` | 任务书写明另一 agent 正在同步补一个未推送提交；我在开始时 `git fetch origin` 后该分支仍为 `090a63c`（无新提交到达），以此 sha 合并 |
| #31 | `origin/feature/m1-01-control-completion` | `865eebfa01eddf7d7595d0aa9053e54efa53d3a` | `c11acef` | 无冲突 |
| #32 | `origin/feature/m1-01-acceptance` | `7452ed573a834c6acff289e3718c2be97e9b7f6` | `daec7fd` | 无冲突 |
| #33 | `origin/feature/m1-01-progress-ui` | `1f495600e60ed5c7b3da7d74f6eafb210c88f4a8` | `d6e2316` | 无 git 冲突，但暴露了与 #31 的运行时接缝问题（见第 4 节），已在本分支修复（`e42d7b7`） |

## 2. 冲突清单与处置

只有 #27 合并时产生一处 git 冲突：

- **文件**：`tests/integration/test_m1_durable_state_postgres.py`
- **双方来源**：
  - HEAD 侧（`integration/m1-01`，已含 #20/#21/#26/#28/#29/#30）：在文件末尾新增了多组测试（`test_generational_fence_is_keyed_to_the_incident_not_the_run_copy`、`test_a_cleared_lease_cannot_be_used_even_when_owner_and_epoch_still_match`、`test_rebuild_drops_pending_tools_from_a_superseded_generation`、`test_install_indexes_the_incident_foreign_keys_on_an_existing_database`、`test_control_distinguishes_unknown_identity_from_a_retryable_conflict`、`test_non_cancel_control_is_refused_from_every_unlisted_run_state`、`test_rebuild_filters_pending_tools_by_the_incident_generation_not_the_run_copy`、`test_lease_identity_and_deadline_each_fence_all_four_write_paths`、`test_control_refuses_a_current_run_pointer_into_another_incident`），并新增 import `RUN_EXECUTION`、`Lease`、`Worker`。
  - #27 侧：在同一位置（文件末尾）新增 `_prompt_versions` 辅助函数 + `test_prompt_revision_change_blocks_resume_without_silent_version_swap` + `test_instance_values_alone_do_not_block_resume`，并新增 import `prompt_revision`、`render as d_render`、`REPORT_VERSION`、`report_instruction`。
- **判断**：两组新增测试彼此独立、互不重叠（不同断言对象、不同 fixture 数据），属于「双方各自在文件尾部追加内容」的典型接缝冲突，语义明确，不属于需要停止确认的歧义情况。
- **处置**：保留双方全部测试与全部 import（imports 合并去重，测试函数全部保留），#27 的三个新增定义排在 HEAD 侧全部测试之后（即文件真正的末尾）。用 `.venv/bin/python -m py_compile` 确认语法正确，`ruff check` + `ruff format` 确认风格通过。
- 未在本次合并中遇到 `ROADMAP.md`/`docs/tasks/*`/`Codex-progress.txt` 类状态文档的真实冲突：`docs/tasks/2026-09-15-instruction-tool-contract.md` 在 #27 合并时被 git 自动无冲突合并（HEAD 侧自基线以来未改过此文件，#27 侧从 209 行扩到 462 行，纯粹是单侧新增，未丢内容，已核对行数）；`ROADMAP.md` 在 #33 合并时同样无冲突（#33 在文件顶部新增一段 2026-09-17 状态记录，未触碰其它日期条目）。

## 3. `make check` 结论

- 合并 #27/#31/#32/#33 后、修复前：`7 failed, 1468 passed, 102 skipped, 2 xfailed in 65.13s`（`make: *** [check] Error 1`）
- 复跑一次定位失败原因后：`6 failed, 1469 passed, 102 skipped, 2 xfailed in 29.91s` —— `tests/test_m0_pg_live_probe.py::test_private_worker_invalid_body_has_no_export_or_config_read` 单独运行必过、两次全量运行中一次失败一次通过，判定为该用例自身对资源/时序敏感（起子进程 + `os.pipe` + `pass_fds`，5 秒超时）导致的偶发 flake，与本次四个分支合并无关，**不在本分支修**，记录供各 PR owner 参考（不确定具体应归属哪个 PR，未在合并前的 `integration/m1-01` 基线上单独复现验证，见第 6 节问题清单）。
- 稳定复现的 6 个失败均为同一根因：`tests/test_m1_web_workbench.py` 下 6 个用例 `AttributeError: '_MemoryCommitter' object has no attribute 'begin_round'`（`opspilot/web/service.py:128`）。判定为 #31×#33 接缝问题，已在本分支修复（见第 4、5 节），修复提交 `e42d7b7`。
- 修复后、`make setup` 同步 #33 新增的 `web` 依赖组（fastapi/jinja2/uvicorn，来自该 PR 自身的 `pyproject.toml`/`uv.lock` 变更，非本次新增）后，完整 `make check` 结论行：

  ```
  ================ 1475 passed, 102 skipped, 2 xfailed in 28.45s =================
  ```
  退出码 0（`doctor` / `uv lock --check` / `ruff check` / `ruff format --check` / `mypy` / `pytest` 全部通过）。

## 4. PostgreSQL 定向测试

- 读取 `/Users/shenghuikevin/dev/AI/production-ops-agent/.env` 中的 `OPSPILOT_DATABASE_URL`（未打印凭据），尝试用 `psycopg.connect(..., connect_timeout=5)` 连接。
- 结果：**连接失败**，`OperationalError: connection failed: connection to server at "127.0.0.1", port 55431 failed: could not receive data from server: Connection refused`（该端口应为本地 lab PG 实例，当前未在监听）。
- 按任务书要求：**PG 定向测试未执行**，未自行拉起数据库或容器。相关测试路径已用 `grep -rl M1_DURABLE_POSTGRES tests/` 确认覆盖 `tests/integration/test_m0_*_postgres.py`、`test_m1_durable_state_postgres.py`、`test_m1_control_completion_postgres.py`、`test_m1_web_postgres.py`，均在本次 `make check` 中以 `SKIPPED ... explicit PG opt-in required` 形式呈现（未被跳过之外的方式处理，未虚构通过）。

## 5. 接缝检查（#31 × #33 交汇点，只读核对 + 一处已修复）

### 5.1 `_EmittingCommitter` 转发 `begin_round`/`assert_current` —— 生产代码侧已就绪，测试替身侧缺失（已修复）

- `opspilot/web/service.py:122-128`：`_EmittingCommitter.begin_round`/`assert_current` 已经用 `getattr(self._base, ...)` 转发，注释明确写着「PR #31 extends the committer seam with begin_round/assert_current... on this branch the loop never calls them」——#33 作者已预留了转发路径。
- 但 `tests/m1_web_support.py:405-`（`_MemoryCommitter`，供 `test_m1_web_workbench.py` 用作 `store=` 传入真实 `InvestigationLoop`）在 #31 合入前编写，未实现这两个方法。#31 的 `opspilot/investigation/loop.py:278-280`、`:412-415`、`:512-515` 让 loop 在每轮开始与每次工具/请求前调用 `store.begin_round()`/`store.assert_current()`，两分支合并后 `_MemoryCommitter` 缺方法直接 `AttributeError`，6 个 web workbench 用例全部炸穿。
- **已修复**（提交 `e42d7b7`，只改 `tests/m1_web_support.py`，未改任何生产代码）：给 `_MemoryCommitter` 补齐 `begin_round(logical_key) -> (logical_key, [])` 与 `assert_current() -> None`，语义对齐同文件已有的 `_run()`（吊销即抛 `CONTROL_DENIED`）以及 `opspilot/investigation/store.py` 里 `MemoryStepStore.begin_round`（同协议的参考实现：不做输入重放，只做吊销守卫）。修复后 6 个用例与全量 `make check` 均通过。

### 5.2 追问/纠正文本的权威位置 —— **未接上，存在两套并存实现**（未修复，需 PR owner 处理）

任务书点名要核对「追问文本权威位置应迁到 `opspilot_controls.payload`」是否真的接上。核查结论：**没有接上，且确实存在两套并存实现**：

1. **#31 建立的权威路径**（`opspilot/persistence.py`）：
   - `DurableStore.control(..., payload: dict[str, Any] | None = None)`（`opspilot/persistence.py:788-794`）在 `action in {"follow_up","correct"}` 时，把 `payload` 写入 `opspilot_controls.payload`（审计行，`:888-899`）**并且**写入 `opspilot_inputs.content`（`:900-911`）。
   - `begin_round()`（`opspilot/persistence.py:962-1008`）读取 `opspilot_inputs`（按 `input_watermark` 截止）作为 `inputs` 返回给调用方。
   - `opspilot/investigation/loop.py:278-311`（#31 新增）把 `begin_round()` 返回的 `inputs` 拼进发给模型的 `investigation_inputs` 消息。
   - 即：只有通过 `DurableStore.control(..., payload=...)` 写入的追问/纠正文本，才会真正送到模型。

2. **#33 的 Web 层完全没有走这条路径**：
   - `opspilot/web/store.py:48-62`（`IncidentStore` Protocol）与 `:262-264`（`DurableIncidentStore.control`，包生产用的 `DurableStore` 适配器）里 `control()` 签名都只有 `(incident_id, expected_generation, action, actor)`，**没有 `payload`/`text` 形参**，`DurableIncidentStore.control()` 直接 `return self._store.control(incident_id, expected_generation, action, actor)`，永远不传 `payload`。
   - `opspilot/web/service.py:238-335`（`Workbench.control()`）确实从 HTTP 请求里取到了 `text`（`:246`），但只把它写进：(a) `self.ledger.put("control_intent"/"control", key, intent)`（`:280`,`:324`，落地到 `opspilot/web/store.py:153-207` 的 `DurableWebLedger`，即独立的 `opspilot_web_ledger` 表，命名空间 `control_intent`/`control`），(b) `self.events.append(incident_id, "control_applied", payload)`（`:323`，事件流，供 SSE/前端展示）。`_apply()`（`:337-345`）调用 `self.incidents.control(summary.incident_id, expected, action, actor_id)` 时**文本被丢弃**，从未到达 `self._store.control(..., payload=...)`。
   - `_confirm_from_audit` 的 docstring（`opspilot/web/service.py:363-370`）里 #33 作者自己写着：「Best effort until the audit carries text/key (PR #31): two crashed attempts by one actor with different texts could still cross-confirm.」—— 说明这个缺口在 #33 落笔时就已经被作者意识到、留作对 #31 的前向引用，但合并后仍未闭环。
   - `tests/m1_web_support.py:169-` 的 `MemoryIncidentStore.control()`（#33 的测试替身）同样只记录 action 名称，不接受也不存储任何文本 —— 与生产侧的缺口完全对称，因此现有 `test_m1_web_workbench.py` 用例都不会因为这个缺口而失败（没有用例断言追问文本进入模型输入）。

- **实际影响**：一旦 #31 与 #33 都合并到 main，通过 Web UI 提交的「追问/纠正」文本会被正确记录在 Web 层账本与事件流里（用户在页面上能看到自己提交的文本、请求会幂等/审计），**但正在跑的调查 loop 永远收不到这段文本** —— `opspilot_inputs`/`opspilot_controls.payload` 侧永远是空的。这不是显示问题，是「人工追问对模型不生效」的功能性缺口，考虑到 PRODUCT-CONSTRAINTS 对人工控制优先级的要求，建议在 #33（或 #31、#33 任一方）合并 main 前先处理。
- **未在本分支修复的原因**：修复需要改动 `IncidentStore` Protocol 签名（`opspilot/web/store.py`）、`DurableIncidentStore.control()`、`MemoryIncidentStore.control()`（测试替身，保持对称）、以及 `Workbench.control()`/`_apply()` 的调用方式，涉及跨文件的协议签名决定（payload 形状、是否要保持向后兼容 `IncidentStore.control()` 四参数调用方等），不属于「改动明确且局部」，按任务书规则记录、不擅自决定语义，交给 PR owner 处理。

### 5.3 是否存在两套并存的 round 栅栏

核查结论：**没有发现**。`_EmittingCommitter` 只做事件旁路，`begin_round`/`assert_current` 全部转发给 `self._base`；生产路径最终落到 `opspilot/investigation/store.py:159-184`（`DurableStepStore`，转发到 `DurableStore.begin_round`/`lease_current`），测试路径落到 `_MemoryCommitter`（本次已补齐，语义对齐 `MemoryStepStore`）。未见到第二套独立实现 round 栅栏或 lease 校验的代码。

## 6. 本分支所做的修复提交

| 提交 sha | 文件 | 对应问题 | 对应 PR（接缝双方） |
|---|---|---|---|
| `e42d7b7` | `tests/m1_web_support.py`（仅测试替身，未改生产代码） | `_MemoryCommitter` 缺 `begin_round`/`assert_current`，6 个 `test_m1_web_workbench.py` 用例 `AttributeError` | #31（新增协议方法）× #33（测试替身未跟进） |

未做其它修改（未做重构、未改验收步骤、未改 `feature_list.json` 的 `passes`、未新增依赖 —— #33 自带的 `fastapi`/`jinja2`/`uvicorn` 依赖组是该 PR 自身内容，非本任务新增）。

## 7. 需要交给具体 PR owner 的问题清单

1. **#33 owner（`feature/m1-01-progress-ui`，可能需与 #31 owner 协商）**：追问/纠正文本未从 Web 层转发到 `DurableStore.control(..., payload=...)`，导致合并 #31 后 Web UI 提交的追问/纠正对正在运行的调查 loop 不生效（详见第 5.2 节，含文件:行）。需要：(a) 扩展 `opspilot/web/store.py` 的 `IncidentStore.control()` Protocol 签名以携带文本/payload；(b) `DurableIncidentStore.control()` 转发给 `DurableStore.control(..., payload=...)`；(c) `MemoryIncidentStore.control()`（测试替身）做对称实现；(d) `Workbench.control()`/`_apply()` 把已经捕获的 `text` 传下去。建议同时补一条集成测试断言追问文本确实出现在下一轮 `investigation_inputs` 里，覆盖现有用例的盲区。
2. **待确认归属（#33 或更早的基线，需要各 PR owner 自行在各自分支上复现确认）**：`tests/test_m0_pg_live_probe.py::test_private_worker_invalid_body_has_no_export_or_config_read` 在全量 `make check` 中偶发失败（两次全量运行各命中一次通过一次失败，单独运行必过），怀疑是子进程/管道超时对资源竞争敏感，与本次合并的四个分支无必然因果关系，未定位到具体归属分支，本分支未修，交给各 PR owner 在 CI 多次重跑核实是否为已知 flake。

## 8. 建议的 PR 合并顺序

1. **#27**（`chore/instruction-contract-impl`，base=main）：与 M1-01 系列无功能耦合，只有一处机械性的测试文件尾部追加冲突，可独立最先合并。
2. **#31**（`feature/m1-01-control-completion`，base=integration/m1-01）：为后续 #33 依赖的 `StepCommitter.begin_round`/`assert_current` 协议来源，建议先于 #33 合并。
3. **#32**（`feature/m1-01-acceptance`，base=integration/m1-01）：本次核查中与 #27/#31/#33 均无接触面（零冲突、零运行时耦合），可在 #27/#31 之后、#33 之前或之后并行处理，不构成顺序约束。
4. **#33**（`feature/m1-01-progress-ui`，base=integration/m1-01）：**建议最后合并，且在合并 main 前先解决第 5.2/7.1 节的追问文本转发缺口**（或者与 #31 owner 一起确认该缺口是否已有后续 PR 承接、并在 #33 的任务记录里显式记录为已知限制）。本分支的 `begin_round`/`assert_current` 测试替身修复（`e42d7b7`）建议由 #33 owner 择机吸收进其自己的分支（cherry-pick 或等价改动），否则一旦真实 main 上先有 #31 再合 #33，`feature/m1-01-progress-ui` 分支自身的 CI 会先复现同样的 `AttributeError`。

---
报告生成时未执行任何破坏性操作；本地未修改除 `integration/m1-01-full` 外的任何 worktree/分支；`integration/m1-01-full` 已推送到 `origin/integration/m1-01-full`，未创建 PR，未合并到 main。
