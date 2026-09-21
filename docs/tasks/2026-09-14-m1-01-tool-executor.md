# M1-01 只读工具执行器（纯逻辑部分）

- 状态：本轮新增提交待推送，推送后需等最新 CI 与已触发 code review 覆盖当前 HEAD
  （[PR #20](https://github.com/kevinWangSheng/production-ops-agent/pull/20)）
- 更新日期：2026-09-17（执行器授权复查顺序 + 证据登记原子性一节）
- 依据：[M1-01 拆分](../evidence/m0-real-investigation/m0-exit-matrix.md#m1-01-任务拆分与投入估算待-gate-决定)
  「只读工具执行器」子任务；[C3 技术方案](../design/technical-proposal-2026-09-07.md)
  第 3 节（Tool Gateway 角色）、第 8 节（工具注册合同）、第 4/7 节（控制版本、
  操作身份与失效结果处理）；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)
  的 Evidence and context requirements、Runtime and human control requirements、
  Data flow contract；[SPEC](../../SPEC.md) 的有界开放 M1-01 门槛；
  冻结上限见 [v4 验收包](../testing/first-investigation-v4-2026-09-10.md)。
  相关验收条目：F3（证据可区分性）、F7（只读安全与审计），二者 `passes` 保持 false。
- 工作区：分支 `feature/m1-01-tool-executor`，
  当前 worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-tool-executor`
  （2026-09-14 记录的 `.claude/worktrees/agent-addafc240c953e663` 为历史路径）。
  交付状态以 PR #20 当前 HEAD 为准，不在此复制提交哈希。

## 目标与范围

实现 Tool Gateway 的纯逻辑半部：在任何查询发出前完成授权判定，把模型提出的
工具调用解析为登记的不可变目标，按时间与体积设界，把结果分为互不混淆的结果类别，
并在结果进入模型上下文之前完成 raw/view/hash 证据登记。

范围内：

- scope 校验（工具、目标、时间窗、参数、控制版本、预算）。
- 精确目标解析：只用登记表中的目标身份，模型给出的名称不参与选择。
- 查询超时（注册 deadline、授权剩余时间、剩余工具时间预算三者取小）与结果大小上限。
- 五类结果分离表达：`ok / no_data / error / timeout / denied`，
  外加与状态正交的 `source_contact`（none / possible / confirmed）。
- 证据 raw / view / hash 登记，含截断、不完整、新鲜度未知的显式标记。
- 工具注册合同与目标登记合同的校验。

范围外（本次明确不做）：

- 真实数据源、容器、网络调用；全部用替身与 fixture。
- `opspilot/` 下的领域类型模块（并行任务负责 C3 第 4 节持久对象类型）。
  本模块只定义自用的最小局部结构，跨模块依赖用 Protocol 声明，后续再接。
- 数据源 payload 内部可能出现的机密的脱敏策略（见「未完成与风险」）。
- `pyproject.toml` 依赖/打包配置、`scripts/` 下既有 M0 代码、`feature_list.json`、
  SPEC 门槛陈述，均未改动。

## 前提与完成条件

- 前提：SPEC 已对 M1-01 有界开放；本子任务属于该拆分内的第一片实现工作。
  不引入新依赖，不发起真实模型或数据源调用，不产生费用。
- 完成条件：
  1. `make check` 通过并保留实际输出。
  2. 四类结果（错误 / 无数据 / 超时 / 拒绝）各有断言分离的确定性测试，
     无数据与错误不得互相代替。
  3. 越权目标、超范围查询、超时、超限四条拒绝路径各有确定性测试。
  4. 三条硬边界（目标只从登记表解析、执行器不提供写操作、凭据不进入被测代码的
     输入或输出）各有确定性测试，而不是靠约定。
  5. 任务记录、提交规范符合 AGENTS.md。

## 必要上下文

- `opspilot/tools/registry.py`：工具注册合同、目标登记表、两个只读索引与 revision。
- `opspilot/tools/outcomes.py`：`Window`、`ToolOperation`、`EvidenceRecord`、
  `ToolOutcome` 与固定 reason 词表。
- `opspilot/tools/executor.py`：`QueryScope`、`ToolRequest`、传输/证据/控制 Protocol、
  `ReadOnlyToolExecutor`。
- `tests/m1_tool_support.py`：FakeClock / FakeTransport / RecordingSink / FixedControl
  等替身与构造器；全部离线。

## 执行进展与证据

### 已完成

- 三个产品模块与三个测试文件已实现；`opspilot/__init__.py` 保持空文件，
  以免与并行任务在同一路径产生 add/add 冲突。
- 检查对象与版本：Python 3.12.13、pytest 9.1.1、ruff 0.16.6、uv 0.10.8。
- 命令与结果（worktree 根目录执行）：
  - `make check` → `uv lock --check` Resolved 38 packages；
    `ruff check .` All checks passed；`ruff format --check .` 395 files already formatted；
    `pytest` **930 passed, 54 skipped in 24.23s**（基线 800 passed / 54 skipped，
    本次新增 130 条；54 条 skip 为既有 PostgreSQL/实验 opt-in 用例，与本次变更无关）。
  - 分文件：`tests/test_m1_tool_registry.py` 57 passed、
    `tests/test_m1_tool_outcomes.py` 30 passed、
    `tests/test_m1_tool_boundaries.py` 43 passed。
  - `python3 scripts/install_gitleaks.py --directory tmp/gitleaks` →
    `GITLEAKS_INSTALLED_8.30.1`；
    `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks` →
    `SECRET_SCAN_PASSED`（该扫描是 CI 步骤，不在 `make check` 内；
    二进制在 gitignored 的 `tmp/` 下）。

### 四类结果的分离测试

| 结果类别 | 代表测试 | 断言要点 |
|---|---|---|
| 无数据 | `test_no_data_is_a_completed_query_not_an_error` | `("no_data", "NO_DATA")`，证据已登记且被采纳，`content == []` |
| 错误 | `test_source_error_is_never_reported_as_no_data`、`test_source_status_is_classified_by_the_registration`、`test_unreadable_results_are_errors_not_empty_results` | `("error", 固定 reason)`，无证据登记，view `content` 为 None |
| 超时 | `test_a_transport_timeout_is_never_recorded_as_a_completed_query`、`test_a_result_that_arrives_after_its_deadline_is_refused` | `TOOL_TIMEOUT`/`GATEWAY_TIMEOUT`，`source_contact` 为 possible/confirmed，不作为成功 |
| 拒绝 | `test_an_unregistered_target_reference_is_denied_before_any_query` 等 | `("denied", 授权类 reason)`，`source_contact == "none"`，传输未被调用 |
| 互斥性 | `test_the_four_failure_and_empty_classes_stay_separable` | 四类 status 两两不同、reason 两两不同，只有 no_data 产生观察 |

### 四条拒绝路径

| 路径 | 测试 |
|---|---|
| 越权目标 | `test_an_unregistered_target_reference_is_denied_before_any_query`、`test_a_registered_but_unauthorized_target_is_denied`、`test_a_target_from_another_data_source_is_denied`、`test_an_authorization_written_against_another_registry_is_denied` |
| 超范围查询 | `test_an_unregistered_tool_is_denied`、`test_a_registered_tool_outside_this_run_authorization_is_denied`、`test_a_window_outside_the_authorized_window_is_denied`、`test_an_undeclared_parameter_is_denied_rather_than_forwarded`、`test_control_state_stops_a_query_before_it_is_sent`、`test_a_suspension_during_flight_keeps_the_result_as_history_only` |
| 超时 | `test_a_result_that_arrives_after_its_deadline_is_refused`、`test_the_effective_timeout_is_the_smallest_of_the_three_bounds`、`test_the_remaining_tool_time_budget_also_caps_the_next_request`、`test_an_expired_authorization_deadline_denies_the_call` |
| 超限 | `test_an_oversized_result_is_refused_and_never_registered`、`test_a_window_wider_than_the_registration_allows_is_denied`、`test_the_operation_budget_stops_further_queries`、`test_the_time_budget_stops_further_queries`、`test_frozen_m1_01_ceilings_are_enforced_by_construction` |

### 三条硬边界

| 边界 | 测试 |
|---|---|
| 目标必须解析为登记的不可变目标，模型给出的名称不决定查询对象 | `test_a_model_supplied_display_name_never_selects_a_target`、`test_parameters_cannot_redirect_the_query_to_another_place`、`test_the_transport_receives_the_registry_endpoint_and_nothing_model_supplied`、`test_two_targets_sharing_a_display_name_stay_distinct_by_identity`、`test_targets_resolve_only_by_registered_identity`、`test_tool_results_are_evidence_and_cannot_change_scope_or_target` |
| 执行器不提供任何写操作 | `test_the_executor_exposes_exactly_one_read_only_entry_point`、`test_every_transport_request_declares_a_read_only_verb`、`test_a_transport_request_cannot_be_built_for_a_write`、`test_every_forbidden_verb_is_refused_at_registration`、`test_read_only_flag_cannot_be_turned_off`、`test_an_unregistered_tool_is_denied` |
| 凭据不进入被测代码的输入或输出 | `test_the_model_cannot_smuggle_a_credential_into_the_request`、`test_the_credential_handle_reaches_the_transport_but_never_the_model_view`、`test_a_secret_held_by_the_transport_never_reaches_an_outcome`、`test_the_executor_is_never_handed_credential_material_to_begin_with`、`test_targets_may_only_hold_an_opaque_credential_handle`、`test_endpoint_may_not_carry_userinfo` |

### 本次做出的设计判断

1. **异常与结果分工**：操作者/控制面写错合同（注册、目标、scope）抛
   `ToolContractError`；模型输入或数据源引起的一切返回 `ToolOutcome`。
   理由：模型输出是不可信输入，把它变成异常会让调查循环需要靠 try/except 表达常态。
2. **`source_contact` 与 status 正交**：C3 第 7 节要求「状态未知不能当作成功」，
   第 8 节说明取消不能撤回已到达数据源的只读请求。因此用 none/possible/confirmed
   单独记录「是否可能已到达数据源」，超时不伪装成未发出。
3. **超时两层**：注册的 SDK 级 `request_timeout_seconds` 交给传输层；
   网关侧再用时钟核对实际耗时，迟到结果按 `GATEWAY_TIMEOUT` 拒绝且不登记为证据。
   对应 C3 第 8 节「同时设置 SDK 超时、网关总超时」与「拒绝失效结果更新业务状态」。
4. **超限是硬拒绝，截断是显式标记**：`max_result_bytes` 超出即
   `error/RESULT_TOO_LARGE` 且不登记证据（不能对半截捕获算哈希）；
   `max_view_bytes` 只裁剪模型视图，raw 证据保持完整，视图标 `truncated`、
   `omitted_rows`、`omitted_bytes`。视图预算精确约束 `canonical(content)` 字节数。
5. **截断到空 ≠ 无数据**：视图被裁到 0 行时 `result_count` 仍是真实行数、
   status 仍为 `ok`，避免与 `no_data` 混淆。
6. **新鲜度未知保持未知**：适配层没有给出 `data_as_of` 时，
   `freshness_seconds` 为 None 而不是 0；适配层给出 naive 时间直接判 `MALFORMED_RESULT`。
7. **证据先提交后消费**：sink 登记失败或返回不匹配引用时返回
   `error/EVIDENCE_NOT_COMMITTED`，模型视图不带内容（C3 第 7 节）。
8. **在途暂停保留历史**：请求发出后控制状态变化时，结果按 `denied` 处理，
   记录以 `adopted=False` 登记为历史，视图不含内容（C3 第 4、8 节）。
9. **授权先于体量**：时间窗先判是否在授权窗口内，再判是否超过注册上限，
   使越权窗口始终报 `WINDOW_OUT_OF_SCOPE`。
10. **冻结上限写进构造校验**：注册的单工具超时 ≤ 30s、结果 ≤ 2MiB，
    scope 的工具次数 ≤ 20、累计工具时间 ≤ 240s，超出直接拒绝构造，
    对应 v4 验收包 2026-09-13 冻结值；上调需要新的用户批准而不是改代码。
11. **保留参数**：注册不得声明 endpoint/target/authorization 等网关自管参数；
    模型提交未声明参数一律 `PARAM_NOT_ALLOWED`，堵住注入 header 或改写目标的路径。

## 下一步与交接

- 已完成本地实现、自测与 `make check`；分支已推送，[PR #20]
  (https://github.com/kevinWangSheng/production-ops-agent/pull/20) 已创建并多次更新。
  **未合并**，合并由用户审核后执行。
  （2026-09-14 原记录此处写「未推送、未建 PR、未合并」，与实际不符，已按真实状态更正；
  该表述反映的是建 PR 之前的历史状态。）
- 待办（交接给后续任务）：
  1. 与并行的领域类型任务对接：目前 `ToolOperation`/`EvidenceRecord` 是本模块自用的
     最小结构，需与 C3 第 4 节的 `ToolOperation / Evidence` 持久对象合并，
     并接 Controller 提交路径（Worker 不直接写业务记录）。
  2. 真实传输适配器、凭据提供方、控制状态读取与 PG 证据存储尚未实现；
     本次全部是替身。本地演示不等于产品验收。
  3. ROADMAP 未更新：项目级状态应在本分支合并后随实际状态变化更新，
     避免与并行 worktree 冲突。
- 未完成与风险：
  - **数据源 payload 内部机密未脱敏**：本模块只保证我方凭据不进入输入/输出，
    不处理数据源自身返回内容里可能含有的机密。视图直接来自 payload，
    因此在脱敏策略成型前，可能返回机密的数据源不应登记。已写入模块文档。
  - `TransportUnavailable` 一律记为 `source_contact="possible"`（保守），
    未区分「连接被拒绝」与「已发出未读到响应」。
  - 未做并发/取消的运行期实现；C3 第 8 节的「不合作调用放入可终止子进程」
    属于传输层，不在本纯逻辑范围。
  - fixture 中避免使用形似真实凭据的字面量；本地 gitleaks 8.30.1 扫描已通过，
    但通过扫描不等于证明无泄漏。
- 本任务未做独立审查；按 AGENTS.md，安全边界变更在完成前应由未参与实现的
  Agent 以全新上下文复核，该项尚未执行。

## 当前交付更新（2026-09-15）

- PR #20 首次 CI 暴露旧分支基线未接入最新 `TID251` 数据库时钟规则；已将分支
  合并到最新 `origin/main`，并移除产品代码的宿主 `SystemClock` 默认实现，要求
  调用方显式注入时钟（真实接入由持久化层提供，纯逻辑替身由测试提供）。
- 同步修复 main 引入的 strict mypy 检查：注册表索引类型收窄、结果 reason 分支
  判定与可空时间戳守卫；新增凭据句柄进入 target registry revision 的回归测试。
- 独立审查发现并已修复：在途控制失效时证据 sink 未提交不得把未提交记录放入结果；
  新增 `test_an_uncommitted_in_flight_history_never_reaches_the_outcome`。
- 本地定向检查（mypy、ruff、format、M1 三个测试文件）通过；完整 `make check`
  已在最新基线兼容修复后重跑。PR #20 的 CI/Review 仍以最新提交为准。

## PR #20 code review 处置（2026-09-15）

- P1「凭据绑定未进入 target registry revision」：已在 `25b090b` 修复，且已在
  PR 线程回复“采纳并修复”；后续回归确认凭据句柄变化会使 revision 改变，thread 已 resolve。
- P1「scope 未绑定完整 tool registry revision」：reviewer 在当前 HEAD 返回后发现，
  已在 `235e801` 加入 `QueryScope.tool_registry_revision`、完整注册合同哈希、操作/证据审计字段，
  执行前 fail-closed 检查，并新增 9 类合同变更、稳定排序、新 scope 成功审计测试。
- 另外将 `SystemClock` 改为调用方显式注入 `Clock`，满足最新 main 的数据库时钟规则；
  strict mypy 兼容守卫一并保留。最新本地 `make check`：**1193 passed, 75 skipped,
  2 xfailed**；mypy、Ruff、format、lock、doctor 均通过。
- 修复已提交并推送到 PR #20；两个 inline thread 均已回复并 resolve。最新 HEAD 的
  CI 已通过，Code Review 已覆盖 `235e801` 且无新增发现；feature passes 仍保持 false。

## 本轮收尾（2026-09-15，T1）

### 1. 机器人安全发现：派发前未按可信时钟复核 scope deadline

Codex Security Review 在 `opspilot/tools/executor.py` 留下一条 P2 未 resolve 发现，
指出 `_reserve()` 用 `operation.started_at` 计算剩余授权时间，而该时间戳早于
`ControlAuthority.snapshot()` 的往返；慢查询跨过 deadline 后仍算出正的传输超时，
`_run()` 只核对 fetch 耗时，结果被采纳。

**独立复核结论：发现成立，已采纳并修复。** 复现（离线假时钟，非 sleep）：
scope deadline 为 `NOW+2s`，control lookup 耗 5s，修复前的实际行为是
transport 被调用、`timeout_seconds=2.0`、outcome 为 `ok`、`adopted=True`、
`model_view["content"]` 带回源数据——即授权到期后仍发出读取并把内容交给模型。

修复（`89744fd`）分两处，均复用既有 `denied` / `DEADLINE_EXCEEDED` 类别，
**未新增任何 reason 码**：

1. `_reserve()` 在 control lookup **之后**重读可信时钟（`authorized_at`），
   以该时刻计算剩余 deadline。control 读取耗时因此计入 deadline，
   而不是作为额外超时发给传输层。过期则拒绝，请求不发出。
2. `_run()` 在采纳前按可信时钟复核 deadline，与既有在途控制检查并列且排在其前。
   过期结果按既有在途失效路径处理：`adopted=False` 登记为历史、视图不含内容、
   `source_contact="confirmed"`（读取确已到达数据源，不可撤回）。

`ToolOperation.authorized_at` 记录该次检查的时刻并进入 `audit_json()`。
它不是冗余字段：`DEADLINE_EXCEEDED` 拒绝路径没有 `finished_at`/`elapsed_seconds`，
不加该字段则审计记录只剩 control 读取**之前**的 `started_at`，无法说明过期在何时被观察到。

### 2. 确定性回归测试与变异验证

新增 `tests/m1_tool_support.py::SlowControl`（control 查询按假时钟消耗墙钟时间）
与三条测试于 `tests/test_m1_tool_boundaries.py`：

| 测试 | 约束 |
|---|---|
| `test_a_deadline_that_expires_during_the_control_lookup_is_denied` | control lookup 跨过 deadline 时拒绝，`not transport.called`，无证据登记，审计中 `authorized_at` 晚于 `started_at` |
| `test_the_control_lookup_time_is_charged_against_the_request_timeout` | 10s 授权 − 3s control 读取 = 传输超时 7.0s，而不是 10.0s |
| `test_a_result_arriving_at_the_deadline_is_kept_as_history_only` | 恰好在 deadline 到达的结果不被采纳，按历史登记，视图不含内容 |

**变异验证（真实输出）**：

- 变异 1：把 `_reserve()` 改回 `remaining_deadline = (scope.deadline - operation.started_at)`
  → `2 failed, 45 passed`，失败项为
  `test_a_deadline_that_expires_during_the_control_lookup_is_denied`
  （`assert not True`，transport 已被调用）与
  `test_the_control_lookup_time_is_charged_against_the_request_timeout`
  （`assert 10.0 == 7.0`）。
- 变异 2：删除 `_run()` 中的 deadline 复核
  → `1 failed, 46 passed`，失败项为
  `test_a_result_arriving_at_the_deadline_is_kept_as_history_only`
  （`assert ('ok', None) == ('denied', 'DEADLINE_EXCEEDED')`）。

两次变异后均已还原为修复版本并复跑通过。

### 3. 同步最新 main

`origin/main` 已推进到 `b483a12`（PR #22/#23/#24/#25）。合并入本分支（`38ea27f`），
**无冲突**。实际核对而非假设：

- PR #22 的 `opspilot/persistence.py` 并发语义改动早已在本分支的合并基点
  `686965f` 之内，本轮 main 增量**未触及** `opspilot/`。
- PR #25 的模型请求名 `deepseek-flash` 只改 `scripts/m0/`、`.env.example` 与文档，
  与 `opspilot/tools/` 无交互。

### 4. 验证证据

worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m1-tool-executor` 根目录：

- `make check` → `uv lock --check` Resolved 43 packages；`ruff check .` All checks passed；
  `ruff format --check .` 420 files already formatted；`mypy` Success: no issues found
  in 17 source files；`pytest` **1208 passed, 75 skipped, 2 xfailed in 25.65s**。
  （合并 main 前的分支基线为 1193 passed / 75 skipped / 2 xfailed；
  增量来自 main 带入的测试与本轮新增的 3 条。75 条 skip 为既有 PostgreSQL opt-in 用例。）
- 定向：`.venv/bin/python -m pytest tests/test_m1_tool_boundaries.py
  tests/test_m1_tool_outcomes.py tests/test_m1_tool_registry.py
  tests/test_m1_tool_registry_binding.py -q` → **158 passed**。

### 5. PR #20 全部 review thread 处置状态

三条 thread 逐条核对（`gh api graphql` 读取 `reviewThreads`），
且不只看回复文字，实际回到代码确认修复存在：

| thread | 结论 | 代码证据 |
|---|---|---|
| P1 凭据绑定未进入 target registry revision（`registry.py`） | 采纳并修复（`25b090b`），已 resolve | `TargetRegistry` fingerprint 含 `credential_ref`（`opspilot/tools/registry.py:385`） |
| P1 scope 未绑定完整 tool registry revision（`executor.py`） | 采纳并修复（`235e801`），已 resolve | `QueryScope.tool_registry_revision` 于 `_authorize()` fail-closed 校验；`ToolRegistry` fingerprint 覆盖参数 schema、result_path、超时/体积/窗口上限、错误映射、incomplete marker、read_only |
| P2 派发前未复核 scope deadline（`executor.py`） | 采纳并修复（`89744fd`），本轮处置 | 见上第 1、2 节 |

### 6. 独立审查状态（未完成）

AGENTS.md 要求安全边界变更由未参与实现的 Agent 以全新上下文复核。
本轮派出四份独立审查，报告一度因 harness handback 失败未送达，
后续全部回传，处置见第 6b/6c 节。四份结论互相冲突，
由 team lead 逐条裁定；**未再另派第五份复审**（避免挑结论）。

实现者另自行做了对抗性边界探测（非独立审查，不替代上述要求），
覆盖 9 种时序组合，断言两条不变量：不得在 deadline 之后发出读取、
不得采纳**读取完成于** deadline 之后的数据。

| 场景（授权秒 / control 秒 / fetch 秒） | 结果 |
|---|---|
| control 跨过 deadline（2/5/0） | `denied` `DEADLINE_EXCEEDED`，未发出 |
| control 恰好落在 deadline（5/5/0） | `denied` `DEADLINE_EXCEEDED`，未发出 |
| control 早于 deadline 1s（6/5/0） | `ok`，已发出且采纳（授权内） |
| fetch 使到达晚于 deadline（10/0/20） | `timeout` `GATEWAY_TIMEOUT`，未采纳 |
| fetch 恰好在 deadline 到达（2/0/2） | `denied` `DEADLINE_EXCEEDED`，未采纳，登记为历史 |
| fetch 早于 deadline 1s 到达（3/0/2） | `ok`，已采纳（授权内） |
| control 与 fetch 都慢（10/6/5） | `timeout` `GATEWAY_TIMEOUT`，未采纳 |
| 授权剩余为 0（0/0/0） | `denied` `DEADLINE_EXCEEDED`，未发出 |
| 授权已过期（-5/0/0） | `denied` `DEADLINE_EXCEEDED`，未发出 |

九种组合均未出现「deadline 后发出」或「采纳读取完成于 deadline 后的数据」。

探测暴露出一处**原先未写明的语义判断**，已在 `2c16431` 补入代码注释：
采纳与否以**读取到达的时刻**为准，而不是采纳动作完成的时刻。
即在授权窗口内完成的读取，即便其后网关自身的 control 查询跨过了 deadline 仍会被采纳。
理由：在授权期内完成的读取本身是被授权的；若改用更晚的时钟读数，
会因网关自身 control/证据存储变慢而丢弃合法取得的证据。
这一判断应由后续独立审查复核。

### 6b. 独立审查回传后的处置（2026-09-16）

四份独立审查最终回传。结论冲突，由 team lead 逐条裁定后处置如下。
**未再另派第五份复审。**

| 审查 | 对采纳侧窗口的判断 | 是否构造了 control 再读变慢的场景 |
|---|---|---|
| 审查一 | 报 P1，复现「晚 11 秒采纳」 | 是 |
| 审查二 | 报 P2，复现「晚 2 秒采纳」 | 是 |
| 审查三 | 判 no remaining path | **否**，只推理语句顺序 |
| 审查四 | 判 no remaining path | **否**，只讨论 dispatch 竞态 |

裁定：机制成立（代码保证的是「`finished_at` 那一刻未过期」，不是「采纳那一刻未过期」），
有复现的两份胜过无复现的两份。但审查一、二都**未引用** `:512-515` 的注释，
该注释已写明以到达时刻为准是有意权衡，因此它不是疏忽。

实际处置（`116e110`），三项：

1. **注释 overclaim 已改正。** 原 `:507-511` 写「a result ... still cannot be adopted
   past its authorization」，与 `:512-515` 的到达时刻语义自相矛盾，且事实上不成立。
   改为陈述真实不变量：**不采纳任何「读取完成于授权窗口之外」的观察**，
   而不是「采纳动作完成于窗口之内」；并显式写明该权衡的代价。
2. **选定语义已由测试钉住。**
   `test_a_read_completed_inside_the_window_survives_a_late_control_re_read`
   断言「读取在窗口内完成、其后 control 再读跨过 deadline」仍然采纳。
   变异验证：把判定改成 `self._clock.now() >= deadline` → 该测试转红
   （`assert ('denied','DEADLINE_EXCEEDED') == ('ok', None)`）。
   即后人若要静默翻转该权衡，测试会拦住，必须先论证。
3. **审查一报的 P2 经自行复现，成立，已修复。**

### 6c. P2：飞行中人工暂停被 deadline 短路吞掉（成立，已修复）

自行复现（非采信审查结论）：operator 在飞行中暂停 + 结果恰在 deadline 到达时，
`89744fd` 的行为是 `("denied","DEADLINE_EXCEEDED")`、`control.calls == 1`
（在途 control 复检**根本没跑**）、`SUSPENDED` 不出现在 outcome 或审计记录中。
同一暂停若结果在 deadline 内到达则报 `SUSPENDED`、`control.calls == 2`。

判定为**真实回归**：`89744fd` 之前该场景报 `SUSPENDED`。依据
[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Runtime and human control
requirements」——late completion 不得抹去更新的人工决定；且 `_reserve()` 在
`:394-405` 本就是 control 先于 deadline，post-fetch 路径与之相反。
不是安全漏洞（两种情况都 denied 且未采纳），是**人工控制的上报/审计回归**。

修复：post-fetch 判定改为与 `_reserve()` 同序的单条链——
control 状态在前、deadline 在后。
新增 `test_an_in_flight_suspension_is_reported_even_when_the_deadline_also_passed`，
并断言 `control.calls == 2` 与 pre-dispatch 路径同样报 `SUSPENDED`。
变异验证：把 deadline 判定移回 control 之前 → 该测试转红
（`assert ('denied','DEADLINE_EXCEEDED') == ('denied','SUSPENDED')`）。

另修正两处文档 overclaim：`timeout` 与 `denied` 两个类别原先都写「came back too
late」，现按**越过的是哪条界限**区分（每请求超时 → `timeout`；
Run 授权 deadline → `denied`）；`authorized_at` 的注释补明它只由 pre-dispatch
检查设置一次，在途复检不更新它。

复跑：`make check` → **1210 passed, 75 skipped, 2 xfailed**；
M1 四个测试文件 **160 passed**。

### 7. 本轮新增的未完成项

- ~~C3 第 8 节「工具模型可见面」尚未实现~~ **已在本轮（2026-09-17）实现**，见下方
  「9. C3 第 8 节工具模型可见面（`ToolDescription`）与第 7 节第 2、3 项确定性检查」。
- 原有交接项（领域类型合并、真实传输/凭据/PG 证据存储、数据源 payload 脱敏、
  `TransportUnavailable` 未细分、并发/取消运行期实现）保持不变，均未在本轮解决。
- F3/F7 的 `passes` 仍为 `false`；本轮是纯逻辑层的安全修复，不构成产品验收证据。

### 8. 交付阻塞：secret scan 因**其它分支**的历史而失败

PR #20 最新提交的 CI：`m0-postgres` **pass**，`checks` **fail**。
失败步骤是 "Secret scan and synthetic detection self-test"，输出 `SECRET_DETECTED`。

**该失败与本分支的改动无关，本分支也无法在自身范围内修复。** 证据：
`scripts/check_secrets.py` 扫描 git 历史时传 `--log-opts=--all`，即扫描 clone 中
**全部 ref**，而非本分支可达的历史。本地用同一固定版本 gitleaks 8.30.1 与同一配置
分三种范围扫描：

| 扫描范围 | 命中数 |
|---|---|
| 全部 ref（CI 实际扫描的范围） | **1** |
| 仅本分支 HEAD 可达历史 | **0** |
| 仅 `origin/main` 可达历史 | **0** |

唯一命中位于 `feature/m1-01-intake-auth` 分支的提交 `64254df`
（`tests/test_m1_intake_auth.py:183`，合成测试夹具 `"s3cr3t-password"`，
被 `generic-api-key` 规则匹配）。该分支的**当前 tip 已从文件中移除**该字面量，
但 git 历史仍保留，故 `--all` 扫描持续命中。

影响范围不限于本 PR：`chore/durable-store-hardening` 分支的 CI 在
2026-09-15T22:47 仍为 success，自 22:55 起转为连续 failure，
与 `feature/m1-01-intake-auth` 推送的时间一致。即**该仓库当前所有分支的
`checks` 都因此变红**。

**本任务不做处置，须由用户决定**，四条路径都超出本任务授权：

1. 由 `feature/m1-01-intake-auth` 任务改写其分支历史（需 force-push 授权，
   本任务被明确禁止）；
2. 收窄 `scripts/check_secrets.py` 的扫描范围（等于削弱一项安全检查，
   须用户批准，且 AGENTS.md 禁止为迁就实现而弱化验收步骤）；
3. 在 `scripts/check_secrets.py` 的扫描配置中，为该合成夹具加一条
   **窄范围 allowlist**（精确规则 + 精确路径 + 精确值三者同时匹配）。
   该文件已有同形式的先例（M0 证据清单中两个经审查的非凭据 SHA256 摘要），
   因此这是四条路径中对检查强度损害最小的一条——它不收窄扫描范围，
   只对一个已审查的具体值开豁免。但它仍是对安全检查的修改，
   且该夹具属 `feature/m1-01-intake-auth` 任务，处置权不在本任务。
4. 保留现状并接受 `checks` 红。

在此之前，PR #20 的 `mergeStateStatus` 为 `BLOCKED`（`mergeable` 为 `MERGEABLE`），
**不能按「CI 全绿」交付**。

## 跨 PR 红线审计 P2-3 处置（2026-09-17）：工具预算跨 attempt 持久化

- 依据：对 `integration/m1-01-full`@e42d7b7 的只读审计第 6 节 P2-3；C3 第 13 节
  「预算在 PostgreSQL 原子预留和结算，未知费用保持占用，重启不能重置预算」。
- 归属判定：`_operations_used`/`_tool_seconds_used` 只存在本分支的
  `opspilot/tools/executor.py`（`git diff origin/main...feature/m1-01-tool-executor`
  只新增文件，main 无执行器）；持久化侧是新增代码，与执行器同一交付。产品当前没有把
  执行器接进 `Workbench.run_once`/`Worker` 的组合代码（`grep ReadOnlyToolExecutor(`
  仅命中测试夹具 `tests/m1_tool_support.py::build`），因此修在接口层。
- 修复（提交 `28f0b2b`，从集成分支 `7c3ff53` cherry-pick，`charge_tool` 按 main 的
  租约栅栏写法重写，语义一致）：
  - 执行器新增 `ToolUsage`/`ToolUsageLedger`，构造必须传 `ledger`；起点取
    `ledger.usage()`，每次派发前 `charge(op, 0.0)` 记次数、派发后 `charge(op, elapsed)`
    结算秒数；ledger 不可用 → 派发前拒绝 `CONTROL_UNAVAILABLE`、派发后不采纳
    （与 `EVIDENCE_NOT_COMMITTED` 同一 fail-closed 规则）。
  - `DurableStore`：`opspilot_runs` 新列 `tool_operations_used`/`tool_seconds_used`
    （沿用 `install()` 的 `ADD COLUMN IF NOT EXISTS`），新表 `opspilot_tool_charges`
    按 `(run, epoch, operation)` 去重，`charge_tool()` 走租约栅栏。
  - `opspilot/tools/ledger.py::DurableToolLedger(store, lease)` 从 `rebuild()` 读已用量。
  - 冻结上限 20 次/240 s 未改；不改验收步骤。
- 复现与验证：
  - 修复前（旧执行器，脚本）：同一 Run 第二个执行器实例再次派发 20 次、累计 240 s。
  - 修复后：`tests/integration/test_m1_tool_budget_postgres.py`
    `::test_second_attempt_of_the_same_run_inherits_used_operations_and_seconds`
    （第二 attempt 起点 3 次/18 s，全 Run 到 20 次即拒绝 `OPERATION_BUDGET_EXHAUSTED`），
    以及 charge 幂等、租约栅栏两条；`tests/test_m1_tool_boundaries.py` 末尾 6 条单元测试。
  - 本分支 `make check`：`1216 passed, 78 skipped, 2 xfailed`；
    PG 定向：`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_tool_budget_postgres.py
    tests/integration/test_m1_durable_state_postgres.py` → `24 passed`。
- 独立审查：见集成侧报告（scratchpad `reports/storefix.md`）的处置表；结论以本记录后续更新为准。
- 未完成/交接：产品组合层（loop/worker/web 服务）尚未把 `DurableToolLedger` 接入，
  接入属对应 PR 的范围；`#29` 的 `MemoryStepStore`/`#33` 的 `ScriptedInvestigator`
  经 `build()` 默认取内存 ledger，行为不变。

### P2-3 独立审查处置（2026-09-17，全新上下文只读子代理，model sonnet）

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P2 | 本分支 `charge_tool` 把 `lease_until IS NULL` 判为撤销，而同文件另三条写路径（main 旧写法）容忍 NULL | 采纳为记录：保留更严口径（与 PR #26 收敛后的 `_lease_revoked` 一致，方向只更严），加注释说明（`ffd3161`）；回改另三条属 #26 范围，不在本 PR 动 |
| 2 | P2 | 「进程中途死掉仍计次」的同 operation_id 跨 epoch 情形无测试；键含 epoch 意味着重派发会再计一次 | 采纳：这是有意的设计（C3 §7 未提交结果可能需要有界重复，真实第二次查询；累计只增不减），`charge_tool` docstring 写明理由，新增 PG 用例 `test_a_re_dispatched_operation_in_a_new_epoch_is_counted_again`（`988ec90`） |
| 3 | P3 | `DurableToolLedger.usage()` 的异常在执行器构造时未被捕获 | 采纳：构造时 `ledger.usage()` 异常 → `ToolContractError("LEDGER_UNAVAILABLE")`，不从零开始（`988ec90`） |
| 4 | P3 | 本地 `_operations_used` 在派发前 charge 确认之前就自增 | 采纳：自增移到 charge 成功之后，单元测试补断言（`988ec90`） |
| 5 | P3 | 派发后结算失败时，持久秒数停留在 0 s（次数保留），本地总量含实测秒数 | 记录不改：窗口极窄、方向为保守（结果不采纳、次数已计），注释写明 |
| 6 | P3 | （属 P2-2）blocked 后版本对回来再领取无测试 | 在 PR #34 补断言，本 PR 无关 |

审查同时确认：双 charge 协议同 epoch 内幂等；fail-closed 路径不外泄异常文本、不登记证据；锁顺序与 `install()` 演进方式与本文件一致；`bool` 被类型校验拒绝；冻结上限未改；无范围外改动。

复验（本分支 `ffd3161`）：`make check` → `All checks passed!` / `Success: no issues found in 18 source files` / `1216 passed, 79 skipped, 2 xfailed`；`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_tool_budget_postgres.py tests/integration/test_m1_durable_state_postgres.py` → `25 passed`。

## 9. C3 第 8 节工具模型可见面（`ToolDescription`）与第 7 节第 2、3 项确定性检查（2026-09-17）

- 依据：调度者任务书 `scratchpad/briefs/registry.md`；
  [C3 技术方案](../design/technical-proposal-2026-09-07.md) 第 8 节「模型可见面」段
  （`returns`/`window_format`/`values_format`/`limits`/`cannot_prove` 五字段表）；
  `docs/tasks/2026-09-15-instruction-tool-contract.md`（PR #27 分支，
  `git show origin/chore/instruction-contract-impl:<path>` 读取）「C3 第 7 节七项确定性检查的
  逐项状态」表（第 2、3 项标「未做：依赖 `opspilot/tools/registry.py`」）与
  「交接：`ToolRegistration.description` 待 PR #20 合并后承接」节。
- 范围：只做 C3 §7 七项确定性检查中的第 2、3 项。不做第 1/4/5/6/7 项的补全，
  不做 §8「模型可见面」文本的实际渲染器，不迁移 PR #27
  `opspilot/instructions/discipline.py` 里标 `tool_specific=True` 的 8 句投影语义
  （该模块在本分支不存在，迁移是 PR #27 任务记录第 4 步，前置条件是 PR #27 先合并进
  main；本轮不复制其实现，只把 `ToolDescription` 设计成能承接的形状）。

### 做了什么

`opspilot/tools/registry.py` 新增 `ToolDescription`（frozen dataclass，字段顺序与
C3 表格顺序一致：`returns`/`window_format`/`values_format`/`limits`/`cannot_prove`）：

- `returns`/`limits`/`cannot_prove` 注册期非空（`.strip()` 后非空），否则
  `ToolContractError("EMPTY_TOOL_DESCRIPTION_FIELD")`。
- `window_format`/`values_format` 注册期须含占位符，否则
  `ToolContractError("MISSING_DESCRIPTION_PLACEHOLDER")`。
  **占位符具体写法（`{window}`/`{values}`）是本轮自定，C3 原文只写「占位符」未给出
  字面 token**——仿照 `docs/tasks/2026-09-15-instruction-tool-contract.md` 引用的
  `discipline.py` 里 `{steps}` 的既有写法（`str.format` 风格），写入模块内注释说明这是
  本模块自己的选择、非 C3 逐字要求，为将来的渲染器（L3a 模板/L3b 实例，尚未建）预留
  钩子。
- 只做结构完整性检查，不判断文字质量——依据 PR #27 任务记录独立审查处置 F10：
  「D1/D5 是语义属性，自由文本无注册期判据…注册期只断言结构完整性，文字质量交人工审查」。

`ToolRegistration` 新增必填字段 `description: ToolDescription`（`__post_init__` 用
`isinstance` 校验，非法值 → `ToolContractError("INVALID_TOOL_DESCRIPTION")`）。
`ParameterSpec` 新增 `description: str = ""`（默认空字符串，向后兼容——C3 §8 的五字段
结构完整性检查只列在 `ToolDescription` 上，不要求每个参数描述本轮也做结构校验；
默认值使原本只测 `kind`/`required`/`.accepts()` 行为的既有用例不必改动）。

`ToolRegistry` 的 fingerprint 投影（`__init__` 内的工具层字典）新增两处（对应
七项检查第 2 项）：工具层的 `description` 五字段整体、以及参数层每个
`ParameterSpec.description`。任一处改动都会改变 `ToolRegistry.revision`。

### 模型可见字节是否变化

**本分支此前没有任何真实注册的工具**（`grep -rln "ToolRegistration("` 排除
`.venv/` 只命中 3 个测试文件；产品代码尚未把执行器接进
`Workbench.run_once`/`Worker` 组合，`ToolRegistration` 只在测试夹具
`tests/m1_tool_support.py::registration()` 里构造），因此**没有已冻结/已记录的
模型可见字节需要保持不变**——`description` 是本轮新增的必填字段，不存在“改变现有
字节”的兼容性问题；`tool_schema_revision` 目前也未接线到任何持久化比对
（同一落差此前已记于本文件第 7 节，未在本轮解决，接线仍属 M1-01「Flash 调查
loop」子任务）。`git grep -n "tool_schema_revision\|\.revision =="` 未发现任何写死
比对的历史哈希字面量，故本轮改动不破坏任何已记录证据。

### 与 PR #27 的接口对齐

未从 PR #27 的 `opspilot/instructions/discipline.py` 复制任何符号或字节——该文件
`find opspilot -iname "*discipline*"` 在本分支为空，只存在于
`origin/chore/instruction-contract-impl`。第 7 节第 2、3 项本身不需要它的任何符号
（`prompt_revision`/`Segment` 等只在做第 1/4/5/7 项的补全或第 4 步迁移时才用得上，
均不在本轮范围）。`ToolDescription` 五个字段是纯字符串，未来迁移
`PROJECTION_DISCIPLINE` 的 8 句时可以直接把文本填进对应字段，不需要改动本轮的类型
或校验逻辑。

C3 §7 七项检查里，第 1（部分完成）、4（部分完成）、5（部分完成）、6（暂停，等用户
裁定与归位决定的冲突）、7（部分完成）项按 PR #27 任务记录的既有结论保持原状，
本轮不动；只有第 2、3 项从「未做」变为「完成」。

### 测试与变异验证

新增/修改：`tests/m1_tool_support.py`（`description()` 构建器 + `registration()`
默认值 + 两个 `ParameterSpec` 补 `description`）、
`tests/test_m1_tool_registry.py`（`ToolDescription` 结构完整性用例，覆盖非空、
占位符存在、占位符精确匹配非子串误判、字段顺序、不可变性、`ToolRegistration`
拒绝非 `ToolDescription` 值）、`tests/test_m1_tool_registry_binding.py`
（`CONTRACT_CHANGES` 新增两项：仅工具层 `description` 不同、仅参数层
`description` 不同且刻意保留 `step_seconds` 不变以隔离维度，避免与「参数被删除」
混淆——这一点是独立审查用变异实测揪出的真实测试缺陷，见下）。

PR #20 现有测试**全部保留**，无一条被删除或弱化；本轮净增测试通过数：
`1233 - 1216 = 17`（含独立审查后删掉 1 条误导性用例，详见下方「独立审查」）。

变异验证（每条新断言手工验证能转红，验证后与保存的 `git diff` 补丁逐字节比对
确认已还原）：

| # | 变异 | 结果 |
|---|---|---|
| M1 | 移除工具层 `description` 投影 | `CONTRACT_CHANGES[3]`（工具描述变化）2 条测试转红，其余 23 条不受影响 |
| M2（第一次，暴露测试自身缺陷） | 移除参数层 `description` 投影，初版测试条目省略了 `step_seconds` | **未转红**——因为省略 `step_seconds` 本身已改变参数集合，掩盖了 description 投影是否生效 |
| M2（修复测试后重跑同一变异） | 同上，测试条目改为显式保留 `step_seconds` 不变 | `CONTRACT_CHANGES[2]`（参数描述变化）2 条测试正确转红，其余 23 条不受影响 |
| M3 | 移除 `ToolDescription` 全部结构校验（`__post_init__` 置空） | 10 条断言转红（6 条非空校验 + 2 条占位符校验 + 1 条精确匹配 + 1 条 `ToolRegistration` 转发） |
| M4 | 移除 `ToolRegistration.__post_init__` 的 `isinstance(description, ToolDescription)` 检查 | `test_registration_requires_a_structurally_complete_tool_description` 转红 |

M2 的第一次尝试本身就是一次有效的变异验证发现：独立审查在后续复核里独立复现了
同一类问题（见下），确认这不是巧合。

### 独立审查（全新上下文只读子代理，未参与实现）

按 AGENTS.md「独立审查使用未参与该方案或实现的 Agent，并以全新上下文启动」，
派发时只给目标、C3 §7/§8 原文、`docs/tasks/2026-09-15-instruction-tool-contract.md`
背景、本轮 diff 补丁与待审工件，不继承实现过程的结论。

结论：**可以按当前状态交回实现者**——未发现阻塞级问题，fingerprint 覆盖经审查自行
变异验证为非空转（在参数层与工具层各自移除投影并复跑，仅对应
`CONTRACT_CHANGES` 条目转红，其余不受影响，随后精确还原并用 `diff` 核对回原
补丁），向后兼容选择（`ParameterSpec.description` 默认空串）合理，边界纪律干净
（`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件均无
改动，未从 PR #27 复制任何代码），`ToolRegistry` 公共面（`{lookup, revision,
tool_names}`）未扩大，三个新错误码均为固定字符串、不拼接字段实际文本。

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | should-fix | `test_registration_rejects_a_description_with_a_blank_required_field` 的注释声称验证 `ToolRegistration` 转发 `ToolDescription` 的错误码，但审查用 traceback 证实 `description(returns="")` 在传入 `registration()` 之前、Python 参数求值阶段就已在 `ToolDescription.__post_init__` 内抛出，`ToolRegistration.__post_init__` 从未被进入——该用例与 `test_tool_description_required_fields_reject_blank_text[returns-...]` 完全重复，且其注释描述的场景在当前实现里没有对应代码路径（没有 try/except 转发） | **采纳，已删除该用例**，改在保留的 `test_registration_requires_a_structurally_complete_tool_description` 上补充注释说明为何「空必填字段」不会走到 `ToolRegistration` 一侧 |
| 2 | nit，不阻塞 | `window_format`/`values_format` 传非字符串（如 `None`）时报的是 `MISSING_DESCRIPTION_PLACEHOLDER` 而非类型错误码，语义上「缺占位符」与「类型错」不完全对应 | **维持现状**：与 `returns`/`limits`/`cannot_prove` 传非字符串统一报 `EMPTY_TOOL_DESCRIPTION_FIELD` 是同一既有写法（单一代码覆盖「一类违规」，而非逐字段/逐原因细分），符合本文件既有 `ParameterSpec`/`ToolRegistration` 的错误码粒度惯例，不单独为此新增代码 |

复验（处置后）：`.venv/bin/python -m pytest tests/test_m1_tool_registry.py
tests/test_m1_tool_registry_binding.py -q` → `98 passed`；`make check` →
`All checks passed!` / `422 files already formatted` /
`Success: no issues found in 18 source files` /
`1233 passed, 79 skipped, 2 xfailed`。

### 未完成与交接

- C3 §7 第 1/4/5/6/7 项仍是 PR #27 任务记录里记录的原状态（部分完成/暂停待裁定），
  本轮未推进。
- `PROJECTION_DISCIPLINE` 的 8 句 `tool_specific` 语义迁往 `ToolDescription` 字段：
  待 PR #27 合并进 main 后才可行（依赖其 `opspilot/instructions/discipline.py`），
  迁移会改变未来 Run 的 L1a 模板字节、须 bump `discipline_revision`
  （PR #27 任务记录第 428-447 行「交接」节已写明步骤，本轮不重复）。
- `tool_schema_revision` 仍未接线到任何持久化 `versions` 比对（第 7 节已记录的
  落差，本轮未解决，接线属后续 M1-01 组合层任务）。
- F3/F7 的 `passes` 保持 `false`：本轮是注册合同的结构化与确定性检查，不构成产品
  验收证据。

## 10. source 区间与秘密来源注册约束（2026-09-17）

本轮目标：为 PR #29 时间策略校验提供数据源实际区间；把执行器已有的秘密来源禁入约束变成注册期校验。工作区/分支沿用本任务，起点 `aff4586`，开始时干净。依据 C3 §8、PRODUCT-CONSTRAINTS 的证据来源与秘密不出站约束；不修改 #29、验收、冻结值或门槛。

字段合同：

- `TransportResponse.source_start_at/source_end_at` 可选，默认均 `None`，表示实际响应所代表的来源时间范围未知。不能由请求窗口、采集时间或 `data_as_of` 补出；适配器负责从来源语义确定。区间允许相等端点（单时刻），不承诺连续采样或无缺口。数据新鲜度仍单独使用 `data_as_of`。
- 两端必须同时存在且为带时区 datetime，start <= end；否则执行器返回 `error/MALFORMED_RESULT`，不登记证据、不向模型暴露内容。两端均缺失仍允许未知时间的调查证据，消费方不得据此赋予时间策略资格。保留来源偏移量，比较采用绝对时间。
- 区间进入 `EvidenceRecord` 和经过哈希的模型 view；未知为 JSON null。投影字节发生变化，`PROJECTION_REVISION` 从 v2 升至 v3，旧证据不可重新标为 v3。未修改冻结哈希/历史工件。
- `ToolRegistration.may_contain_secrets` 必填，无默认值；只有严格 bool False 接受。True 返回固定错误 `SECRET_BEARING_SOURCE_FORBIDDEN`，非 bool 返回 `INVALID_SECRET_DECLARATION`；省略由构造器拒绝。声明覆盖原始 payload，不只投影行；进入 registry revision。这是受审配置声明，不是扫描器或脱敏保证，不允许从模型输入决定。
- 真实注册仍不存在（产品代码仅声明类型；构造在共享测试夹具）；夹具显式声明 False。未来真实注册必须审查来源内容，不能机械填 False。真实适配器、#29 消费方和持久化版本接线仍属后续。

测试证据：首轮新增测试在旧实现 `5 failed in 0.19s`（新字段不存在）；实现后 `6 passed in 0.03s`。补未知区间、单时刻/非 UTC、缺失秘密声明和双端点 malformed 测试后，定向 `116 passed in 0.08s`。独立全新上下文只读审查指出 naive 单端点用例遮蔽时区分支，已采纳补双端点 naive/non-datetime 的 start/end 四项。

本轮未改 PG/ledger 路径；本地不启动共享 PG 实验环境，PG 集成检查交由 PR CI 的隔离实例，单元检查不声称 PG 证明。未发起模型调用/新增费用/依赖。项目阶段未变，ROADMAP 保留原状态；本节接续任务的具体进展。

独立复验：全新上下文只读 Agent `/root/srcrange_review` 复核最终代码与补测，独立运行 `116 passed`，无剩余阻塞发现；仅覆盖本补丁/合同测试，不代表真实适配器或产品验收。

最终 `make check` exit 0，结论行原样：

```text
All checks passed!
422 files already formatted
Success: no issues found in 18 source files
================= 1246 passed, 79 skipped, 2 xfailed in 27.91s =================
```

79 skips 为 PG opt-in，2 xfails 为既有架构标记。下一步：普通推送到 PR #20、等待当前 HEAD CI；机器人审查按本轮任务书不是门槛，不合并。#29 应传入 source 两端与可信交付参考时刻，按最老来源时间判断 current（不是最新数据的 freshness），缺失必须 fail-closed；详细建议交接至 srcrange 报告。

## 11. 执行器授权复查顺序 + 证据登记原子性（2026-09-17）

- 依据：`docs/evidence`（外部合并决策摘要）digest1.md「PR #20 §4/§5」的两条具体疑点；
  PRODUCT-CONSTRAINTS「Runtime and human control requirements」；C3 第 7 节
  （断点恢复表）与第 8 节（取消不能撤销已到达数据源的只读请求）。
  工作区/分支沿用本任务，起点 `472a4e1`（srcrange 任务已合并的状态），开始时干净。

### 疑点 1：慢账本可能让读取在授权过期后才发出

`_run()` 在 `_reserve()` 已完成 control/deadline 检查、算好 `timeout` 之后，
先执行**可能阻塞**的账本预记账 `charge(operation_id, 0.0)`，再 `fetch()`，
中间没有重新检查 deadline/控制。账本预记账本身是一次无界往返（真实环境是一次
PostgreSQL 写入）；如果它单独耗时超过剩余授权，`_reserve()` 算出的 `timeout`
已经过期，`fetch()` 仍会照常发出，读取因此可能在授权过期后才离开进程。

**复现（先红）**：新增 `SlowLedger`（`tests/m1_tool_support.py`，仿照既有
`SlowControl` 的写法，`charge()` 消耗假时钟时间，`charge_on` 限定第几次 charge
调用变慢）。构造预记账耗时 5s、deadline 只剩 2s 的场景，修复前
`transport.called` 为真——读取确实被发出。

**修复**（`opspilot/tools/executor.py`，`_run()`）：在预记账成功、
`_operations_used += 1` 之后、`fetch()` 之前，插入一次复查：先读 control
（unavailable/suspended/generation 改变均拒绝），再查 deadline（`clock.now()
>= scope.deadline` 则 `DEADLINE_EXCEEDED`）。顺序与已有的 fetch 后复查一致
（control 优先于 deadline，保证暂停即使与 deadline 同时发生也不会被吞掉，
依据 PRODUCT-CONSTRAINTS「late completion 不得抹去更新的人工决定」）。
复查失败时**不退还已记的账**——已花费的记账保持占用，与既有「未知费用保持占用」
的既定方向一致，不新增退款语义。

复查后：`transport.called` 为假、`evidence is None`、`sink.records == []`、
预记账仍然只被计入一次（`len(ledger.charges) == 1`）。

新增测试（`tests/test_m1_tool_boundaries.py`）：

| 测试 | 场景 |
|---|---|
| `test_a_slow_pre_dispatch_ledger_charge_that_crosses_the_deadline_is_denied` | 慢账本把 clock 推过 deadline |
| `test_a_slow_pre_dispatch_ledger_charge_that_crosses_a_suspension_is_denied` | 慢账本期间控制状态变为已暂停 |
| `test_the_pre_fetch_re_check_denies_when_control_becomes_unavailable` | 新复查自身的 CONTROL_UNAVAILABLE 分支（独立审查建议补的分支覆盖） |
| `test_the_pre_fetch_re_check_denies_when_the_control_generation_changed` | 新复查自身的 CONTROL_GENERATION_CHANGED 分支（同上） |

**受影响的既有测试**：新插入的复查会在 `_reserve()` 与 fetch 之间多做一次
control 读取，3 个依赖精确调用次数的既有测试因此需要同步更新
（不是弱化，是让测试继续钉住原意图，逐条见提交说明与独立审查处置）：

- `test_a_suspension_during_flight_keeps_the_result_as_history_only`、
  `test_an_uncommitted_in_flight_history_never_reaches_the_outcome`：
  `FixedControl` 新增 `later_after` 参数（默认 1，不影响其它未传参调用方），
  这两条改传 `later_after=2`，让「飞行中才暂停」继续发生在 fetch 之后
  （第 3 次调用），而不是被新插入的第 2 次调用提前捕获。
- `test_an_in_flight_suspension_is_reported_even_when_the_deadline_also_passed`：
  同上改 `later_after=2`，并把 `control.calls == 2` 改为 `== 3`。
- `test_a_read_completed_inside_the_window_survives_a_late_control_re_read`：
  `SlowControl(..., slow_on={2})` 改为 `slow_on={3}`，让「变慢的是飞行中复查」
  仍指向 fetch 之后的那次调用（现在是第 3 次），而不是新插入的第 2 次。

### 疑点 2：snapshot 与 register 之间的失败是否已由 EVIDENCE_NOT_COMMITTED 覆盖

结论：**已覆盖，不需要代码修复**，仅补一条测试证明覆盖面不局限于 `SUSPENDED`
一种原因。

`_run()` 里 `invalid` 分支（读 control → 判 suspended/generation/deadline →
建历史记录 → `_register()`）本就是：`registered = self._register(record)`；
`return self._refuse(..., evidence=record if registered else None)`。即无论
`invalid` 具体是哪个原因，只要 `_register()` 未成功提交，`evidence` 就是
`None`，`ToolOutcome.adopted`（= `evidence is not None and evidence.adopted`）
必为 `False`。既有测试
`test_an_uncommitted_in_flight_history_never_reaches_the_outcome` 已经用
`SUSPENDED` 原因验证过这一点；本轮新增
`test_an_uncommitted_deadline_denial_also_never_reaches_the_outcome`，
用 `DEADLINE_EXCEEDED` 原因重复同一断言，证明该保证不局限于某一个具体的
`invalid` 原因，而是分支结构本身的性质。

变异验证：把 `evidence=record if registered else None` 改成恒为
`evidence=record`（忽略 `registered`），两条测试（既有的 SUSPENDED 版本与
新增的 DEADLINE_EXCEEDED 版本）同时转红（`assert outcome.evidence is None`
失败），证明新测试确实在验证这条保证，不是恒真断言。

### 变异验证汇总（均先红后绿，还原后与保存补丁逐字节比对确认无残留）

| # | 变异 | 结果 |
|---|---|---|
| M1 | 整段删除新插入的 fetch 前复查 | 6 条测试转红（2 条新慢账本测试 + 3 条已同步更新调用次数的既有测试 + 1 条 `slow_on` 已改的既有测试） |
| M2 | `evidence=record if registered else None` → 恒为 `evidence=record` | 既有 SUSPENDED 测试与新增 DEADLINE_EXCEEDED 测试同时转红 |
| M3 | 新复查删去 control 三个分支（unavailable/suspended/generation），只留 deadline | 3 条测试转红（1 条慢账本-暂停测试 + 2 条本节新增的分支覆盖测试） |

### 独立审查（全新上下文只读子代理，未参与实现）

按 AGENTS.md 派发全新上下文只读审查，只给目标、约束、C3 原文与本轮 diff，不继承
实现过程结论。审查自行对 M1、M2 做了独立复现（临时删除代码、确认对应测试转红、
逐字节还原并与补丁核对一致），并额外核对了：4 处因新插入调用而调整的既有测试是否
仍在钉住原意图而非只是凑数字（结论：是，逐条给出证据）；`SlowLedger.charge_on`
的 1-based 计数是否有 off-by-one（结论：无，与既有 `SlowControl.slow_on` 同一写法）；
范围纪律（`feature_list.json`/`SPEC.md`/`ROADMAP.md`/依赖锁文件均无改动）。

**过程插曲**：审查子代理在做 M2 变异验证时尝试用 `git checkout -- <file>` 还原，
被本会话权限规则拦下（该命令属禁止的破坏性操作，见 AGENTS.md「不 force-push/
rebase/amend/reset/stash」精神的同类禁止项）。调度者发现后指示改用无损方式
（`git diff > patch` + `git apply -R` 对照），本执行者据此直接把文件手工改回原文本
并用 `diff <(git diff) <保存的最终补丁>` 确认逐字节一致，随后审查子代理自行完成
剩余工作并回传报告；过程中未使用 `git checkout`/`stash`/`reset` 等破坏性命令，
未丢失任何已有改动。

**结论：可以按当前状态交回实现者，无阻塞发现。** 唯一意见是一条覆盖面 nit——
新插入复查自身的 `CONTROL_UNAVAILABLE`/`CONTROL_GENERATION_CHANGED` 分支当时
未被专门测试直接命中（逻辑是从 `_reserve()`/fetch 后复查原样复制，风险低，但
建议补分支覆盖）。**已采纳**：新增
`test_the_pre_fetch_re_check_denies_when_control_becomes_unavailable`、
`test_the_pre_fetch_re_check_denies_when_the_control_generation_changed`
两条，并扩展 `UnavailableControl` 支持 `fail_from`（默认 1，不影响唯一既有调用点）。
采纳后复验：`pytest tests/test_m1_tool_boundaries.py -q` → `60 passed`；
变异 M3 证明两条新测试确实转红。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 422 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1251 passed, 79 skipped, 2 xfailed
```

`exit=0`。较本节起点（`1246 passed`）净增 5 个通过用例（2 条慢账本测试 + 1 条
deadline-register-fail 测试 + 2 条分支覆盖测试）。

PG 定向（本轮改动的账本/控制路径由真实 `DurableToolLedger`/`DurableStore` 消费，
不止内存替身）：`M0_ENV_FILE=/Users/shenghuikevin/dev/AI/production-ops-agent/.env
python -m scripts.m0.postgres_lab start`，
`M1_DURABLE_POSTGRES=1 pytest tests/integration/test_m1_tool_budget_postgres.py
tests/integration/test_m1_durable_state_postgres.py -q` → `25 passed`；
`postgres_lab stop`。该次 PG 验证覆盖的 `_run()` 逻辑此后未再变化
（第二轮补测只新增内存替身用例，未改产品代码），故未重跑。

### 未完成/限制

- 新插入复查仍无法完全消除竞态窗口：`_read_control()` 本身与 `fetch()` 之间
  仍有极短的间隙（纯本地计算，无 I/O），但 fetch 之后已有既有复查兜底，
  与本轮改动前的既有设计一致，不是新引入的缺口。
- 疑点 2 的「结构性保证」依赖 `invalid` 分支的 if/return 形状本身；若未来重构
  该分支为其它写法，需要重新核对该保证是否仍然成立（测试会捕捉，但值得在
  重构该分支时特别注意）。
- 组合层（#29/#30/#33）仍未接入真实执行器/账本，本轮不改变这一状态。
- F3/F7 的 `passes` 保持 `false`。

## 12. 机器人 code review 六条 thread 处置（2026-09-17 收尾）

推送第 11 节的修复（`570240b`）后，`@codex review` 在当前 HEAD 上留下 6 条此前未处理的
inline review thread（`required_conversation_resolution` 分支保护要求全部 resolve 才能
`mergeStateStatus: CLEAN`），逐条核实、处置、回复并 resolve，明细如下：

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `executor.py:530` | P2 | 预记账失败等 `_run()` 内 fetch 前拒绝路径，`ToolOperation.sent` 从 `timeout_seconds is not None` 推断，会在从未派发时误报 `true` | 采纳 | 新增 `dispatched` 字段，`sent` 直接读取它，只在真正调用 `fetch()` 前置位 | `30f3a80` |
| 2 | `executor.py:552`（本轮新增复查自身） | P2 | 复查只做二元判断，未按剩余时间收紧 `timeout_seconds`，账本+control 读取消耗的时间不会体现在实际超时里 | 采纳 | 复查内重算 `remaining` 并在小于原值时收紧 `request`/`operation` 的 `timeout_seconds` | `46cbf3e` |
| 3 | `executor.py:865`（`_result_rows`） | P2 | 深嵌套/超长整数会让 `json.loads` 抛 `RecursionError`/`ValueError`，未被捕获，异常直接冒出 `execute()` | 采纳 | `except` 子句扩至 `(UnicodeDecodeError, ValueError, RecursionError)` | `46cbf3e` |
| 4 | `executor.py:689`（成功路径） | P2 | `model_view` 与 `EvidenceRecord.view` 共享同一 dict，调用方原地修改会连带改到已提交证据 | 采纳 | `model_view=deepcopy(record.view)`，证据侧对象不再被模型侧修改影响 | `46cbf3e` |
| 5 | `persistence.py:356`（`charge_tool`） | P1 | 计次新操作的 UPDATE 无条件执行，行锁只保证串行化不保证不超额；两个从同一过期快照起步的执行器可把持久计数推过冻结的 20 上限 | 采纳 | UPDATE 加 `AND tool_operations_used<%s`，`rowcount==0` 时抛 `OPERATION_BUDGET_EXHAUSTED`（同事务回滚，不留孤儿计费行）；结算分支不受影响 | `defecd1` |
| 6 | `executor.py:578`（结算充值失败） | P1（机器人评级） | 结算充值失败时持久秒数停在预记账的 0，重启后新 attempt 会漏算这部分秒数，可重复花费同一段 240s 预算 | **不采纳，回复依据** | 与 2026-09-17 已完成的「P2-3 独立审查处置」表第 5 项是同一场景，当时评级 P3、裁定「记录不改」（理由：窗口窄、结果不采纳+次数已计两个维度仍保守）。机器人评级升到 P1 是严重度判断分歧，不是新技术事实；按 AGENTS.md 不由审查意见自行改写已记录决策，原样上报，是否升级处置交用户裁定 | 不适用（未改代码） |

全部 6 条已在 GitHub 上逐条回复（引用具体提交与测试）并 `resolveReviewThread`；`gh api graphql` 复核
`reviewThreads` 当前 `isResolved` 全部为 `true`。

第 1、2、3、4、5 项均先复现（红：构造场景证明缺陷存在或让测试在旧代码下失败）再修复（绿），
并逐项做了变异验证（改回旧逻辑，确认对应新测试转红，随后精确还原并与保存的 `git diff` 补丁
逐字节核对一致）。第 5 项另外过了真实 PostgreSQL（`M1_DURABLE_POSTGRES=1`）：
`test_charge_tool_refuses_a_new_operation_once_the_cap_is_reached`、
`test_charge_tool_settlement_is_not_subject_to_the_cap` 两条新用例 + 原有 25 条共 27 passed；
变异（还原成无条件 UPDATE）后前一条正确转红（`DID NOT RAISE`），其余不受影响。

独立审查（全新上下文只读子代理，未参与实现，只给目标/6 条机器人原文/`git diff 570240b..HEAD`）
逐项复核第 1–5 项修复：结论「可以按现状推送，无阻塞发现」，另指出一处不在本轮范围内、当前不可达
的结构性缺口——`DurableToolLedger` 的 `max_operations` 目前固定为全局冻结上限
`MAX_OPERATIONS_PER_RUN`（20），与 `QueryScope.max_operations`（允许 `(0, 20]` 内更窄的
per-Run 值）脱钩；若某个 Run 被授权的上限低于 20，两个执行器仍可能在真正的、per-Run 的上限上
重演同一竞态。审查确认目前不可利用——`ReadOnlyToolExecutor`/`DurableToolLedger` 均只在测试里
构造，组合层尚未接入（本文件第 7 节已记录同一落差）——列为交接给未来接线任务的已知项，
不阻塞本次推送。

第 6 项遗留：是否把「结算失败丢秒数」的处置从「记录不改」升级为需要持久化保守预留，
以及上述 `max_operations` 脱钩的交接项优先级，均待用户或后续任务决定，本轮不自行选择。

## 13. 机器人 code review 第二轮三条 thread 处置（2026-09-18）

推送第 12 节的 6 条处置（`fb28026`）后，`@codex review` 在当前 HEAD 上又留下 3 条新的未处理
inline review thread（`mergeStateStatus` 再次变为 `BLOCKED`），逐条核实、处置、回复并 resolve：

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `persistence.py` `charge_tool`（`executor.py:530` 触发点） | P1 | 预派发充值固定记 `seconds=0.0`，真实耗时只在 fetch 后结算时才写入；两个共享同一 Run 的执行器即使都在预记账处按行锁串行，彼此在预记账阶段都看不到对方"即将花掉的时间"，`tool_seconds_used` 250s 上限（冻结值 240s）可被超订 | 采纳（机制成立），**本轮不实现代码修复，作为已确认交接项记入** | 与既有「计次」竞态（第 12 节第 5 项）的关键区别：计次修复能生效是因为预记账**立即真实自增 1**，第二个事务能看到；秒数预记账**刻意**记 0，不产生可被下一个事务看到的变化——单纯在锁内重读判断关不上洞。验证过"预记账直接充满 timeout"的朴素写法：会撞上 `delta = max(0.0, 实际-已记录)` 这条已测试保护的"结算不向下修正"不变量，导致每次操作都按预留上限永久计费，方向相反的新错误。正确修复需要仿照已有 `reserve_budget()`/`opspilot_budget_reservations` 做真正的预留/结算两阶段协议（新增列区分"预留中"/"已结算"、覆盖崩溃恢复语义），是一次协议改造而非本轮量级的小修。已确认当前不可利用：`grep` 全仓库确认组合层（#29/#30/#33）未构造任何 `ReadOnlyToolExecutor`/`DurableToolLedger` 实例，不存在针对同一 Run 的并发执行器 | 不适用（未改代码） |
| 2 | `executor.py:887`（`_result_rows`） | P2 | `{"data":{"result":["\ud800"]}}` 这类体积合规的响应，`json.loads` 正常解码成功（孤立代理项是合法 Python str），但 `_fit_rows()` 随后对该行 `canonical(row).encode("utf-8")` 抛 `UnicodeEncodeError`，未被此前的 JSON 解码器上限修复捕获——那次只包住 `json.loads()` 本身，这次失败发生在解码成功之后的规范化重编码阶段 | 采纳 | `_result_rows()` 确认 `cursor` 为列表后，新增对每一行的规范化可编码性校验，`except (ValueError, RecursionError)` 时按既有"结构不合法"口径返回 `(None, payload)`，与 result_path 缺失/游标非列表走同一条 `MALFORMED_RESULT` 路径 | `42bb69d` |
| 3 | `registry.py:371`（`RegisteredTarget.__post_init__`） | P2 | 配置形如 `https://metrics.internal/api?token=secret` 的查询鉴权/预签名端点，既有正则接受、既有校验只挡 userinfo（`@` 前部分），密文原样存入 `RegisteredTarget.endpoint` 并流入每次 `TransportRequest.endpoint`，绕开 `credential_ref` 不透明句柄间接层 | 采纳 | 在既有 userinfo 检查之后新增：端点包含 `?` 或 `#` 一律拒绝（而非枚举"像凭据"的具体参数名——presigned URL 的签名参数名因厂商而异，枚举必然被绕过；请求期查询参数本就该走 `TransportRequest.params`） | `97785c4` |

第 2、3 项均先复现（红：证明缺陷存在）再修复（绿），并做了变异验证（改回旧逻辑确认对应新测试
转红，随后精确还原并与保存的 `git diff` 补丁逐字节核对一致）。

**独立审查**（全新上下文只读子代理，未参与实现，只给目标、3 条机器人原文、`git diff fb28026..HEAD`；
过程中两次误用 `git checkout --` 被本会话权限规则拦下，均已按无损方式——`git diff > patch` 与
`git apply -R`/直接编辑还原、`diff` 核对——纠正后继续，未丢失任何工作）：

- 第 3 项（credential query/fragment）：确认「整体拒绝」优于「凭据参数名黑名单」——预签名 URL 的
  签名参数名因厂商而异（`X-Amz-Signature`/`X-Goog-Signature`/SAS `sv`/`se`/`sp` 等），黑名单必然
  留有维护债务；`RegisteredTarget` 只由人工审阅过的运营配置构造，误伤成本接近零。判定「可以按现状
  推送，无阻塞发现」。
- 第 1 项（时间预算竞态）：独立复核了竞态机制、朴素修复为何破坏既有"结算不向下"不变量、
  `reserve_budget()` precedent 的适用范围（指出该模式目前只有"准入"半边有真实实现，"结算"半边在
  本仓库尚不存在，因此"参照现有模式"低估而非高估了本轮工作量）、以及当前不可触发的结论，均独立
  验证通过。**同意本轮延后的工程判断**，但指出与第 12 节 `max_operations` 脱钩交接项的一个关键
  差异：脱钩交接项失效方向是更保守的全局上限，而这条一旦可触发就是真正的预算超支，两者「当前都
  安全可延后」但优先级不应被同等对待——建议组合层接入并发调度时优先处理这一项。
- **第 2 项（JSON 规范化崩溃）：独立审查指出本轮修复不完整**，发现并用可执行复现证明了同一失败类型
  的第二条、当前即可触发的路径：`_record()` 里 `view["query"] = dict(plan.params)`
  （`executor.py:774`）把模型提供的工具调用参数原样存入 `view`，随后 `view_sha256=canonical_hash(view)`
  （`executor.py:800`）对整个 `view` 调用 `.encode("utf-8")`；`ParameterSpec.accepts()` 对 `string`
  类型参数只做 `isinstance` 检查，不做可编码性校验，因此模型传入 `{"expr": "\ud800"}` 这样的参数
  会在 `_record()` 内触发同一 `UnicodeEncodeError`，且这条路径不需要任何尚未接线的组合层代码——
  每次 `_run()`（包括被拒绝/失效的结果）都会执行到这里。已独立复现确认（构造
  `ToolRequest(params={"expr": "\ud800"})` 直接调用 `executor.execute()`，复现出与报告一致的堆栈）。

采纳复核意见，追加第 4 项修复（`9b38412`）：`_accept_params()` 对声明为 `string` 类型的参数值，
通过既有 `isinstance` 校验后新增 `value.encode("utf-8")` 尝试，`except ValueError`
（`UnicodeEncodeError` 的父类）时返回 `INVALID_PARAMS`，在证据构造之前、输入边界处就把这类值挡下，
不必在 `_record()` 深处再包一层。新增测试
`test_a_string_parameter_holding_a_lone_surrogate_is_an_input_error`：变异验证——删掉新增校验后
测试转红，复现出与独立审查报告完全一致的 `canonical_hash(view)` 处 `UnicodeEncodeError` 堆栈；
恢复后与保存补丁逐字节核对一致，测试转绿。

全部 3 条已在 GitHub 上逐条回复（引用具体提交、测试与变异验证；第 2 项回复已更新，补充说明
`_authorize()` 里 `operation.query = canonical(params)` 本身确实不会崩溃这一结论成立但不完整，
并记录追加的第三条 `_accept_params()` 修复）并 `resolveReviewThread`；`gh api graphql` 复核
`reviewThreads` 当前 12 条（含此前第 12 节 9 条）全部 `isResolved: true`。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 422 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1261 passed, 81 skipped, 2 xfailed
```

`exit=0`。本轮改动只涉及 `opspilot/tools/executor.py`（+24）、`opspilot/tools/registry.py`（+8）、
`tests/test_m1_tool_outcomes.py`（+22）、`tests/test_m1_tool_registry.py`（+11），未触碰
`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/`pyproject.toml`/`uv.lock`
（`git diff --stat` 逐一确认为空）。第 1 项未改代码，不需要额外 PG 定向验证；第 2、4 项均为内存路径，
不涉及持久层，沿用本节已跑的内存测试套件即可。

### 未完成/限制

- 时间预算竞态（本节第 1 项）：已确认机制成立、当前不可触发，作为交接给 #29/#30/#33 接线任务的
  已知项，本轮不实现修复；独立审查建议接线并发调度时优先于 `max_operations` 脱钩项处理。
- 组合层（#29/#30/#33）仍未接入真实执行器/账本，本轮不改变这一状态。
- F3/F7 的 `passes` 保持 `false`。

## 14. 机器人 code review 第三/四轮三条 thread 处置（2026-09-18）

推送第 13 节的修复（`7660dad`）后，`@codex review` 又留下 2 条新的未处理 inline thread
（`mergeStateStatus` 再次变为 `BLOCKED`）；处置并推送（`e641e6d`）后约 2.5 分钟内，机器人又追加
第 3 条全新 thread（与前两条无关，是独立的一次新发现，不是同一批延迟到达）。三条一并逐条核实、
处置、回复并 resolve：

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `registry.py:327`（`ToolRegistration.__post_init__`） | P2 | `max_view_bytes=1` 被接受，但 `_fit_rows()` 无论结果是否为空，`used` 都从 2（`[]` 的两个包裹字节）起步；空结果的 `content: []` 规范化后正好 2 字节，声明的上限从未真正生效，代码也未报告这个矛盾 | 采纳 | 新增共享常量 `EMPTY_VIEW_BYTES = 2`（`registry.py`），`_fit_rows()` 的 `used` 初值与注册期校验共用同一常量；校验下界从 `0 <` 改为 `EMPTY_VIEW_BYTES <=` | `2aa82e2` |
| 2 | `ledger.py:38`（`DurableToolLedger.__init__`） | P1 | `max_operations` 默认取全局冻结上限（20），与 `QueryScope.max_operations`（允许更窄的 per-Run 值）脱钩；未来若接线代码构造该账本时忘记显式传参，会静默按更宽的全局上限强制，而不是该 Run 实际被授权的更窄上限 | 采纳 | 去掉默认值，改为必填关键字参数（与本 PR 更早一轮 `persistence.charge_tool` 的同类修复 `defecd1` 同一思路）；仓库里唯一真实调用点（PG 集成测试的 `_attempt`）已更新为显式传参 | `e641e6d` |
| 3 | `registry.py:188`（`ParameterSpec.accepts` 附近的 `_accept_params`） | P2 | 声明为 `number` 的参数接受 `float("nan")`/`float("inf")`（`accepts()` 只做 `isinstance` 检查），`canonical()` 随后把它们序列化成非标准 JSON token `NaN`/`Infinity`，可能被严格的下游反序列化器拒绝，或被不同数据源不一致地解释 | 采纳 | `_accept_params()` 新增校验：`isinstance(value, float) and not math.isfinite(value)` 时返回 `INVALID_PARAMS`，与既有字符串 UTF-8 可编码性校验并列 | `7db5c0c` |

第 1、3 项均先复现（红）再修复（绿），逐项变异验证（改回旧逻辑确认对应测试转红，随后精确还原并
与保存的 `git diff` 补丁逐字节核对一致）。第 2 项的验证性质不同——见下方独立审查小节。

**独立审查**（两轮，均为全新上下文只读子代理，未参与实现；派发提示均明写禁止
`git checkout --`/`stash`/`reset`/`amend`，改用 `git diff > patch` 与 `git apply -R`/`apply` 对照）：

- 第 1 项：核实 `_fit_rows()` 在 `budget == EMPTY_VIEW_BYTES` 且非空 `rows` 时的边界行为
  （`_fit_rows([1,2,3], budget=2)` → `([], 3, 3)`，零行保留、全部行正确报告为省略，未发现漏算）；
  确认 `max_view_bytes=0` 这条参数化用例在旧代码下本就因为触达旧下界而通过，不区分新旧代码，
  只有 `max_view_bytes=1` 才是真正定位新收紧边界的用例——已在变异验证记录中注明这一点。
- 第 2 项：独立复核确认一个关键性质——**去掉默认值本身，对"显式传参"这条路径的运行时行为没有
  任何改变**（`charge()` 转发 `self._max_operations` 给 `store.charge_tool()` 的逻辑修复前后完全
  一致），唯一的行为变化是"遗漏传参"从"静默用 20"变成"`TypeError`"。因此新增的 PG 集成测试
  `test_the_durable_ledger_binds_the_cap_it_was_constructed_with_not_the_global_default`
  （构造 `max_operations=1` 的账本，证明第二次充值被按 1 而非全局 20 拒绝）**不是**这个具体缺陷的
  红绿回归测试——独立审查用变异验证直接证实：把默认值还原后，这条 PG 测试仍然通过，因为它本来就
  显式传参。真正钉住这个缺陷的是新增的纯单元测试
  `tests/test_m1_tool_ledger.py::test_max_operations_has_no_default_and_must_be_supplied_explicitly`
  （断言遗漏该关键字参数时抛 `TypeError`）——变异验证：还原默认值后此测试正确转红。PG 测试的准确
  定位是"独立证明一个调用方主动传入的更窄上限，端到端地被真实账本正确强制"，而不是这个具体 bug 的
  回归锁定；报告已按这个更准确的措辞记录，不高估也不低估其价值。`grep` 确认仓库里构造
  `DurableToolLedger(` 的地方只有测试文件，去掉默认值不会破坏任何现存调用点。
- 两轮审查均确认 `git status --short`/`git diff --stat` 在变异验证后为空，未留残留改动。

全部 3 条已在 GitHub 上逐条回复（引用具体提交、测试、变异验证结果，第 2 项的回复采用独立审查
澄清后的"端到端确认"措辞而非"回归测试"措辞）并 `resolveReviewThread`；`gh api graphql` 复核
`reviewThreads` 当前 15 条（含第 12/13 节共 12 条）全部 `isResolved: true`。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 423 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1267 passed, 82 skipped, 2 xfailed
```

`exit=0`。第 2 项涉及持久层，过了真实 PostgreSQL（`M1_DURABLE_POSTGRES=1`）：
`tests/integration/test_m1_tool_budget_postgres.py tests/integration/test_m1_durable_state_postgres.py`
→ `28 passed`（原 27 + 新增 1）。`git diff --stat` 确认本轮改动只涉及
`opspilot/tools/executor.py`、`opspilot/tools/registry.py`、`opspilot/tools/ledger.py` 及对应
测试文件（含新增 `tests/test_m1_tool_ledger.py`），未触碰
`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件。

### 未完成/限制

- 组合层（#29/#30/#33）仍未接入真实执行器/账本：第 2 项的修复只消除了"遗漏传参就静默用错默认值"
  这一种失败模式；真正把 `DurableToolLedger` 绑定到某个 Run 自己的 `QueryScope.max_operations`，
  仍要等接线任务里第一次出现真实 `QueryScope` 对象可读时才能完成。
- F3/F7 的 `passes` 保持 `false`。

## 15. 机器人 code review 第四轮一条 thread 处置（2026-09-18）

推送第 14 节的修复（`4c6adce`）后，`@codex review` 又留下 1 条新的未处理 inline thread
（`mergeStateStatus` 再次变为 `BLOCKED`，用户称此批为「第四轮」）：第 14 节第 3 项（模型参数侧
NaN/Infinity 校验）的镜像发现——数据源响应侧同样可以携带 NaN/Infinity。

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `executor.py:910`（`_result_rows`） | P2 | 数据源响应体如 `{"data":{"result":[NaN]}}` 能正常解码（`json.loads` 默认 `parse_constant` 会把裸 `NaN`/`Infinity`/`-Infinity` token 解析成非有限 `float`，属 Python 扩展行为，非标准 JSON），既有的逐行 `canonical(row).encode("utf-8")` 校验对此不生效——`canonical()` 只是原样把同一个非标准 token 重新吐出来，不会报错，该行会被当作正常观测采纳，非标准 token 被写进已提交的证据视图 | 采纳 | 在 `_result_rows()` 既有的逐行校验 `try` 块里并列新增 `json.dumps(row, allow_nan=False)`，命中同一个 `except (ValueError, RecursionError)` 分支，走既有 `MALFORMED_RESULT` 路径；该调用会递归检查整个 `row` 结构（不论嵌套多深），不只是顶层。新增参数化测试 `test_a_row_holding_a_non_finite_number_is_malformed_not_a_crash`（`NaN`/`Infinity`/`-Infinity`） | `5f6cc44` |

先复现（红：删掉新增校验，让新测试在旧逻辑下正确转红——不是崩溃，而是静默产出 `("ok", None)`
结果）再修复（绿），随后精确还原并与保存的 `git diff` 补丁逐字节核对一致。

**独立审查**（全新上下文只读子代理，未参与实现，派发提示明写禁止 `git checkout --`/`stash`/
`reset`/`amend`，改用 `git diff > patch` 与 `git apply -R`/`apply` 对照）：独立复现了底层缺陷
（`json.loads` 确实无错解码、`canonical()` 确实原样吐回非标准 token）；独立完成变异验证并确认
`git status --short`/`git diff --stat` 在还原后为空；确认 `json.dumps(row, allow_nan=False)`
对任意嵌套深度的非有限浮点数都会抛出（实测 `{"a": [1, {"b": float("nan")}]}` 与三层嵌套的元组
场景均正确抛出）；核查 `_run()`/`_record()` 控制流，确认 `cursor` 里任意一行未通过校验都会让
`_result_rows()` 整体返回 `(None, payload)`，`_record()` 根本不会被调用——包括结果被判定为
`invalid`（`adopted=False`，走 `_fit_rows(rows, 0)`）的路径，因为它消费的 `rows` 本来就是已经
通过这层校验之后的序列，不存在"零保留行绕过校验"的口子。未发现其它遗漏，结论「按现状可以
接受，无阻塞发现」。

已在 GitHub 上回复（引用具体提交、测试名、变异验证结果、独立审查确认的递归深度与控制流覆盖
结论）并 `resolveReviewThread`；`gh api graphql` 复核 `reviewThreads` 当前 **16 条**（前几轮共
15 条 + 本轮 1 条）全部 `isResolved: true`。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 423 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1270 passed, 82 skipped, 2 xfailed
```

`exit=0`。本轮改动只涉及 `opspilot/tools/executor.py`（+11）与 `tests/test_m1_tool_outcomes.py`
（+22），不涉及持久层，未额外跑 PG 定向测试；未触碰
`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件。

### 未完成/限制

- 组合层（#29/#30/#33）仍未接入真实执行器/账本，本轮不改变这一状态。
- F3/F7 的 `passes` 保持 `false`。

## 16. 机器人 code review 第五轮两条 thread 处置（2026-09-18，用户明确的最后一轮）

推送第 15 节的修复（`3012f36`）后，`@codex review` 又留下 2 条新的未处理 inline thread
（`mergeStateStatus` 再次变为 `BLOCKED`）。用户明确此为最后一轮：处置到 unresolved=0 且 CLEAN
后不再手动触发 `@codex review`。

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `executor.py:722`（`_charge`） | P2 | `_charge()` 用 `except Exception: return False` 无差别吞掉账本异常，`_run()` 两处调用点都把失败映射成通用 `CONTROL_UNAVAILABLE`——包括第 12 节第 5 项（`defecd1`）已经让 `charge_tool` 在原子上限校验失败时抛出的具体、权威的 `PersistenceError("OPERATION_BUDGET_EXHAUSTED")`。调用方看到的是像瞬时故障一样的通用拒绝，可能做不必要的重试，审计也丢失真实原因 | 采纳 | 新增 `ToolBudgetExhausted` 异常（`executor.py`，作为抽象 `ToolUsageLedger` Protocol 契约的一部分，与既有 `TransportError`/`ControlUnavailable` 同一模式）；`_charge()` 让它穿透而不是被吞掉；`_run()` 两处调用点捕获后映射为 `("denied", "OPERATION_BUDGET_EXHAUSTED")`；`DurableToolLedger.charge()` 把 `PersistenceError("OPERATION_BUDGET_EXHAUSTED")`（固定代码，非厂商文本）翻译成这个抽象异常，其它 `PersistenceError` 原样透传 | `3a84cec` |
| 2 | `registry.py:234`（`ToolDescription.__post_init__`） | P1 | C3 §8 明确禁止模型可见面出现"凭据、认证信息或具体 endpoint / base_url / 凭据句柄"，但既有校验只查非空与占位符存在，从未查内容——把真实 URL 写死进任意字段会被原样接受，成为未来渲染器发给模型的工具注册表内容 | 采纳 | 复用既有 `_ENDPOINT` 正则（`RegisteredTarget.endpoint` 格式校验用的同一条，`.search()` 而非 `.fullmatch()`），命中即抛 `CREDENTIAL_MATERIAL_FORBIDDEN`。刻意只挡具体 URL，不做关键词扫描——见下方"范围说明" | `e60fc75` |

第 1、2 项均先复现（读码/变异确认缺陷存在）再修复，逐项变异验证（改回旧逻辑确认对应新测试转红，
随后精确还原并与保存的 `git diff` 补丁逐字节核对一致）。第 1 项额外过了真实 PostgreSQL：更新了
第 15 节前新增的 PG 集成测试 `test_the_durable_ledger_binds_the_cap_it_was_constructed_with_not_the_global_default`
的断言，从 `pytest.raises(PersistenceError, ...)` 改为 `pytest.raises(ToolBudgetExhausted, ...)`，
证明这条转换路径在真实数据库上确实生效，不只是内存替身的行为。

### 第 2 项范围说明：为何只挡 URL，不做通用凭据关键词扫描

C3 §8 原文明确写了"保留参数名集合……不可平移到描述文本——授权范围内的服务名枚举与实例标识是必须
写入描述的内容"：也就是说"endpoint"/"token"/"credential" 这些词本身在描述文本里是合法甚至必要
的内容（例如"这个工具读取 /metrics 端点"、"返回内容里不会包含任何 token"这类澄清句）。基于关键词
的过滤器要么漏掉真正的密钥，要么挡下合法描述，两难。"文本里是否嵌了一个完整 URL"是语法上可以
确定性判断的，直接对应 §8 点名的"具体 endpoint / base_url"子类；"文本里是否包含一个真实凭据取值"
不是语法可判定的问题，与 `ToolDescription` 类文档字符串已经确立的原则一致——结构完整性在注册期
检查，文本内容质量（含这一类安全属性）留给人工审阅（该原则引用自 PR #27 任务记录的独立审查处置
F10）。

**独立审查**（全新上下文只读子代理，未参与实现，派发提示明写「禁止 `git checkout --`/`stash`/
`reset`/`amend`，对照用 `git diff > patch` 与 `git apply -R`/`apply`」）：

- 第 1 项：独立复现底层缺陷、独立完成变异验证、读码确认 `charge_tool` 结算分支确实不受上限
  WHERE 子句影响（结算调用点的处理确实是纯防御性、真实账本不可达）、确认 `DurableToolLedger`
  的翻译只精确匹配 `"OPERATION_BUDGET_EXHAUSTED"` 这一个固定字符串（grep 了 `persistence.py`
  全部 29 个 `PersistenceError` 抛出点，确认无同名冲突）、针对真实 PostgreSQL 重新跑通该 PG
  测试。结论：修复正确、完整，两条新测试均非空洞。
- 第 2 项：独立复核了"URL-only vs. 通用扫描"这一范围决策，**独立判断该决策正确**：URL 匹配语法
  上无歧义、与合法文本零冲突（已实测确认 `_ENDPOINT.search()` 不会误伤"the metrics endpoint"这类
  纯词语提及或不带 scheme 的裸路径），关键词扫描则会直接与 §8 原文冲突。指出写作上一处不够精确的
  表述——`RegisteredTarget` 里 `_ENDPOINT` 实际只用于端点格式校验（`INVALID_ENDPOINT`），真正的
  `CREDENTIAL_MATERIAL_FORBIDDEN` 判定用的是另外的 userinfo/查询串检查，不是同一条正则——已在
  GitHub 回复与本节措辞中改为准确表述（"复用格式校验用的同一条正则"，不再说"复用凭据检查用的
  同一条正则"）。另指出一个真实但不在本条 thread 范围内的交接项：`ParameterSpec.description`
  （同属 §8"模型可见面"概念，其自身文档字符串也引用了同一条 §8 原文）目前完全没有内容检查，存在
  与本次修复前 `ToolDescription` 相同的缺口——可用同一条 `_ENDPOINT.search()` 直接补上，是一个
  小而机械的后续修改，本轮按 thread 范围不实现，记为交接项。

全部 2 条已在 GitHub 上逐条回复（引用具体提交、测试名、变异验证结果、范围决策依据，第 2 项回复
按独立审查的措辞修正更新）并 `resolveReviewThread`；`gh api graphql` 复核 `reviewThreads` 当前
**18 条**（前几轮共 16 条 + 本轮 2 条）全部 `isResolved: true`。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 423 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1278 passed, 82 skipped, 2 xfailed
```

`exit=0`。第 1 项涉及持久层，过了真实 PostgreSQL（`M1_DURABLE_POSTGRES=1`）：
`tests/integration/test_m1_tool_budget_postgres.py tests/integration/test_m1_durable_state_postgres.py`
→ `28 passed`。`git diff --stat` 确认本轮改动只涉及 `opspilot/tools/executor.py`、
`opspilot/tools/ledger.py`、`opspilot/tools/registry.py`、`opspilot/tools/__init__.py` 及对应
测试文件，未触碰 `feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件。

### 未完成/交接项（第五轮）

- **新增交接项**：`ParameterSpec.description`（每参数模型可见文本）目前无任何内容检查，存在与
  本轮修复前 `ToolDescription` 相同的"嵌入 URL 不被拦"缺口，可直接复用同一条 `_ENDPOINT.search()`
  校验补上；本轮按用户指定的 thread 范围（仅本轮机器人指出的两条）不实现，留给后续任务或用户
  决定是否现在处理。
- 此前记录的组合层接线依赖（时间预算竞态、`DurableToolLedger.max_operations` 绑定）状态不变。
- F3/F7 的 `passes` 保持 `false`。
- 用户已明确本轮为最后一轮机器人 review 处置：处置完成、unresolved=0、`mergeStateStatus: CLEAN`
  后不再手动触发 `@codex review`；若后续仍有机器人自动/延迟触发的新 thread，按本任务已建立的
  处置流程处理，但不主动再发起新一轮触发。

## 17. 机器人 code review 第六轮两条 thread 处置（2026-09-18，用户重申的最后一轮）

第 16 节收尾后，`@codex review` 又自动/延迟触发出 2 条新的未处理 inline thread
（`mergeStateStatus` 再次变为 `BLOCKED`）。用户经由调度者重申此为最后一轮：处置到
unresolved=0 且 CLEAN 后不再手动触发 `@codex review`；若收尾时又自动出现新 thread，只在报告
里列出原文摘要，不处置。

| # | 位置 | 级别 | 发现摘要 | 判定 | 处置 | 提交 |
|---|---|---|---|---|---|---|
| 1 | `executor.py:844`（`_record()` 构造 `EvidenceRecord`） | P2 | `EvidenceRecord.view` 在每次访问时都返回同一个可变 dict 对象（含嵌套的 `content` 列表）。任何读取 `outcome.evidence.view`（或持有 record 引用的 evidence sink）后就地修改它的调用方，都能悄悄改动"已提交"的证据，而 `view_sha256` 仍然是修改前内容的哈希，导致审计校验与留存证据不一致 | 采纳 | `EvidenceRecord.view` 字段改名为私有 `_view`，新增 `view` 属性在每次访问时返回 `deepcopy(self._view)`，调用方永远拿不到内部存储对象的引用；`_run()` 原有的 `model_view=deepcopy(record.view)` 简化为 `model_view=record.view`（属性本身已返回新副本，无需再套一层 deepcopy），移除因此未使用的 `executor.py` 内 `copy` 导入 | `22e979e` |
| 2 | `executor.py:538`（`_run()` 构造 `TransportRequest`） | P2 | `plan.params`（普通可变 dict）被按引用直接传给 `TransportRequest.params`。若某个 transport 适配器就地归一化/修改 `request.params`，会同时改到 `plan.params`——而这正是 `_record()` 之后再次读取（`dict(plan.params)`）用于构建证据视图 `query` 字段的同一个对象，导致记录下来的 query 可能悄悄偏离接受时已经固定的 `operation.query`，也偏离实际派发的内容 | 采纳 | `_run()` 改为把 `MappingProxyType(dict(plan.params))`（分离且不可变的副本）交给 transport；适配器就地修改的尝试会直接抛异常，而不是静默污染 `plan.params` | `7598809` |

两项均先写确定性测试复现（红：临时撤回对应修复，让新测试在旧逻辑下转红）→ 最小修复（绿）→
精确还原对照，确认 `git status --short`/`git diff --stat` 在还原后为空、修复重新应用后两个新
测试同时转绿。新增测试：
`test_mutating_a_committed_evidence_view_does_not_corrupt_the_stored_record`
（`tests/test_m1_tool_outcomes.py`）、
`test_a_transport_that_mutates_dispatched_params_cannot_corrupt_the_recorded_query`
（`tests/test_m1_tool_boundaries.py`）。

**独立审查**（全新上下文只读子代理，未参与实现，派发提示明写禁止 `git checkout --`/`stash`/
`reset`/`amend`/`rebase`/force-push，对照用 `git diff`/`git apply -R`/`apply`）：两项修复均判定
"sound"（关闭了对应发现，未引入新问题）。

- 第 1 项：确认 `_record()` 里本地 `view` dict 在构造 `EvidenceRecord` 之前没有任何"首次读取前
  就已被别处持有并可能被修改"的别名缺口（`view` 由 `dict(plan.params)`、`plan.window.as_json()`
  等新鲜副本拼成，从未被其它代码保留引用）；确认"每次访问都 deepcopy"是必要强度而非过度设计——
  若只在构造时深拷贝一次，第一个调用方的修改仍会污染第二个调用方（如 sink）看到的内容；确认
  `deepcopy` 对 `view` 里实际存放的所有值类型（纯 JSON 安全的 str/int/float/bool/None/dict/list，
  日期一律先转 ISO 字符串）都正确适用；全仓库 grep 确认 `EvidenceRecord(` 只有一处构造点，字段
  改名不会破坏其它代码。
- 第 2 项：确认 `MappingProxyType(dict(...))`（浅拷贝级别）已经足够、并非不足或过度设计——
  `_accept_params()`/`ParameterSpec.accepts()` 已把每个被接受的参数值限制为
  `str`/`int`/`float`/`bool`，`plan.params` 里不可能出现可变容器，因此不需要深拷贝；专门检查了
  `TransportRequest` 另外两个同样按引用传递的字段（`selector`、`window`）是否存在同类"可变对象被
  多处别名持有"的问题，结论两者均已天然免疫：`RegisteredTarget.selector` 在构造时已被
  `object.__setattr__` 包成 `MappingProxyType`（`registry.py`），`Window` 本身是已冻结的
  dataclass——**未发现附近还有第三处同类未报告的实例**。
- 两项均在本次会话里独立重新跑过 `ruff check .`/`ruff format --check .`/`mypy`/
  `pytest tests/ -q`，结论与实现者自测一致（全部干净，`1280 passed, 82 skipped, 2 xfailed`）。

全部 2 条已在 GitHub 上逐条回复（引用具体提交、测试名、红绿对照结论、独立审查确认的"未发现
第三处同类实例"结论）并 `resolveReviewThread`；`gh api graphql` 复核 `reviewThreads` 当前
**20 条**（前五轮共 18 条 + 本轮 2 条）全部 `isResolved: true`。

### 验证

```text
ruff check .          → All checks passed!
ruff format --check . → 423 files already formatted
mypy                  → Success: no issues found in 18 source files
pytest                → 1280 passed, 82 skipped, 2 xfailed
```

`exit=0`。本轮两项发现均不涉及持久层（`opspilot/tools/executor.py`/`outcomes.py` 的纯内存逻辑），
未额外跑 `M1_DURABLE_POSTGRES=1` 定向测试。`git diff --stat` 确认本轮改动只涉及
`opspilot/tools/executor.py`、`opspilot/tools/outcomes.py` 及
`tests/test_m1_tool_outcomes.py`、`tests/test_m1_tool_boundaries.py`，未触碰
`feature_list.json`/`SPEC.md`/`ROADMAP.md`/`PRODUCT-CONSTRAINTS.md`/依赖锁文件。已推送
`22e979e`、`7598809`；base 分支为 `main`，普通 push 即触发了 CI（run 35424605647），
`checks`/`m0-postgres` 两个 job 均通过；`mergeStateStatus: CLEAN`，`mergeable: MERGEABLE`。

### 未完成/限制

- 此前记录的组合层接线依赖、`ParameterSpec.description` 内容校验交接项状态不变。
- F3/F7 的 `passes` 保持 `false`。
- 用户/调度者已重申本轮为最后一轮：本次收尾未再手动触发 `@codex review`；若收尾检查时发现机器人
  又自动出现新 thread，按约定只在任务报告里列出原文摘要，不在本轮处置。

## 18. 与 main 的合并冲突处置（2026-09-20）

PR #20 在 `mergeStateStatus: DIRTY`、`mergeable: CONFLICTING`。原因是分支停在 `c12066a`
之后 main 前进了 82 个提交，其中 [PR #26](https://github.com/kevinWangSheng/production-ops-agent/pull/26)
（`ddc1c2b`，确认是 `origin/main` 祖先）把租约栅栏收敛成 `_lease_revoked` 一份实现，
PR #30（`8e67237`）又在同一文件加入 `lease_current()`/`abandon()`。

合并方向为 `git merge origin/main`（不 rebase，不改写已推送历史）。

### 冲突范围

两侧自 merge-base `b483a12` 起都改过的文件**只有** `opspilot/persistence.py`
（`comm -12` 比对两侧 `--name-only` 结果）。冲突是同一位置的 add/add：本分支在
`reserve_budget()` 之后新增 `charge_tool()`，main 在同一位置新增 `lease_current()`/
`abandon()`。两者互不相关，**双方全部保留**。

### charge_tool 的租约栅栏改为复用 _lease_revoked

`charge_tool()` 原先是内联的六条件判定，并带注释说明「本文件另三条写路径仍是 main 上
容忍 NULL 的旧写法，由 #26 统一，本处不回改它们」——#26 已随 main 合入，该注释在合并
后不再成立。因此本次把内联判定换成 `self._lease_revoked(row, lease, self._db_now(conn))`，
并给 SELECT 的 `i.control_generation` 加上 `AS incident_generation` 别名以匹配该实现的
取值口径。

**六条判定条件相同**：`owner`、`epoch`、事故代际（两边读的都是 `i.control_generation`，
只是别名不同）、`lease_until IS NULL`、`lease_until <= now`、`deadline <= now`，缺一不多一。
合并后 `charge_tool` 成为第五条共用同一栅栏实现的写路径，docstring 里「Fenced by the lease
like every other write path」由此成为字面属实的陈述。

**但这不是逐字节等价，有一处真实差异**（独立审查指出，本次已复核修正措辞）：原内联判定对
`lease_until` 和 `deadline` 各调用一次 `self._db_now(conn)`，共读两次库时钟；`_lease_revoked`
只取一个 `now` 传入，读一次。`clock_timestamp()` 在事务内会前进（实测两次调用间隔
55µs–4.2ms），因此 `deadline` 恰好落在该亚毫秒窗口内时，旧写法拒绝、新写法放行——方向是
**更宽松**。评估为无害且更一致：`deadline` 是 Run 级墙钟上限、不是人工决定，不触碰
PRODUCT-CONSTRAINTS 的人工控制优先级；且单一 `now` 正是 `_lease_revoked` 其余全部调用点
（`:517`/`:636`/`:663`/`:728`/`:864`）与 `claim()`/`renew_lease()` 的既有口径，原先两次读取
才是本文件里的异类。故不改代码，只把「无行为变化」的说法修正为本段。

提交 `66f5957` 的提交信息里写的是「逐条相同，无行为变化」，该措辞同样越过了证据；提交信息
不改写历史，以本节为准。

### 对既有 code review 处置的影响：无

前六轮 review 的修复提交（`5f6cc44`/`3a84cec`/`e60fc75`/`22e979e`/`7598809`）全部落在
`opspilot/tools/*` 与 `tests/*`；本次合并在 `opspilot/` 下只动 `persistence.py`、
`recovery.py`、`worker.py`，与这些文件无交集，没有回退任何一条已处置的发现。合并前
`reviewThreads` 20 条仍全部 `isResolved: true`。

### 验证

```text
git diff --stat origin/main -- opspilot/persistence.py
  → 1 file changed, 95 insertions(+)        # 纯新增，0 删除：main 的 #26/#30 一行未丢
make check                                  → 1313 passed, 133 skipped, 2 xfailed（ruff/format/mypy 全过）
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q
  → 95 passed, 38 skipped                   # 与 CI m0-postgres 同一组 opt-in
pytest tests/integration/test_m1_tool_budget_postgres.py \
       tests/integration/test_m1_lease_renewal_postgres.py \
       tests/integration/test_m1_durable_state_postgres.py -q
  → 76 passed
```

改动后的栅栏由 `test_charge_tool_is_fenced_by_the_lease_like_every_write_path` 在真实
PostgreSQL 上覆盖（`control(..., "cancel", ...)` 之后 `charge_tool` 抛 `CONTROL_DENIED`，
且用量停在取消前的 `(1, 1.0)`），本地已实际执行通过，非仅静态推断。

### 未完成/限制

- 合并后 HEAD 变为 `ea2ac8e`，已在该 HEAD 上触发 `@codex review`，机器人返回
  「You have reached your Codex usage limits for code reviews」，**本轮未产出任何 review 或
  thread**，当前 HEAD 因此没有机器人复审覆盖（上一轮覆盖的是 `8cf6d73`）。按 AGENTS.md
  机器人额度不足不计为交付阻塞，此处如实记录该缺口。
- 仓库内针对 `charge_tool` 栅栏的回归测试只有
  `test_charge_tool_is_fenced_by_the_lease_like_every_write_path` 一条，走的是
  `control(..., "cancel", ...)` 即事故代际维度；`lease_until IS NULL`、owner 单独不匹配、
  epoch 单独不匹配、deadline 过期、run 行代际单独变化这五个维度没有入仓测试（独立审查用
  一次性脚本覆盖过，脚本未入库）。该缺口非本次合并引入，不阻塞本次交付，列入后续。
- 本次只解决合并冲突，未改动 F3/F7 的实现范围；两者 `passes` 保持 `false`。

### 独立审查（全新上下文只读子代理，未参与本次合并实现）

因机器人额度用尽无法覆盖当前 HEAD，另起一个未参与实现、全新上下文的只读子代理复核本次合并。
审查点与结论：

1. **等价性**——成立。核法不是读码推断：子代理构造了 `runs.control_generation=3` /
   `incidents.control_generation=7` 的真实行，实测两侧读到的都是 `7`（事故代际），确认
   `i.control_generation` 无别名时 psycopg `dict_row` 的取值不存在同名列歧义（该 SELECT 未投影
   `r.control_generation`）；再把 c12066a 的六条件谓词逐字复刻，与合并后的 `_lease_revoked`
   跑同一批行做 10 维差分（含专门用来暴露「读错列」的 `RUN-ROW generation bumped only`），
   **mismatches: 0**；活体 `charge_tool()` 在同样 10 个状态下结果一一对应。
2. **是否丢失 main 行为**——没有。`git diff --stat 8e67237 ea2ac8e` 为
   `14 files changed, 6177 insertions(+)`、**全树 0 deletions**，强于本节原先只核
   `persistence.py` 的那条证据。
3. **是否回退已处置的 review 修复**——没有，且是内容级证据：`opspilot/tools/` 全部文件及
   `tests/test_m1_tool_*.py`、`tests/m1_tool_support.py`、`tests/integration/test_m1_tool_budget_postgres.py`
   在 `c12066a` 与 `ea2ac8e` 之间逐字节相同（祖先关系不足以证明内容存活，故另做比对）。
4. **跨 PR 语义冲突**——未发现。实测 `abandon()` 只清 `owner`/`lease_until` 不动 epoch，而
   `claim()` 的 `epoch = int(row["epoch"]) + 1` 单调递增，故 `(run, epoch, operation_id)`
   计费键在 abandon 后不会复用（attempt2 epoch=2，同一 operation_id 重新派发被再次计次）；
   abandoned lease 上 `charge_tool` 抛 `CONTROL_DENIED`；`rebuild()` 仍是
   `SELECT * FROM opspilot_runs`，新列照常流出。
5. **`OPERATION_BUDGET_EXHAUSTED`**——仍成立。`CAP=3` 实跑：上限硬拦、被拒操作的
   `opspilot_tool_charges` INSERT 随事务回滚不留孤儿行、已计次操作的秒数结算不受上限影响。

子代理独立重跑：`make check`（`1313 passed, 133 skipped, 2 xfailed`，ruff/format/mypy 全过）、
`M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration/ -q`（`95 passed, 38 skipped`）、
四个 PG 文件定向 `79 passed`（无 skip）。

判定：**无确认缺陷，可按现状交付**；唯一必改项是本节上方已修正的措辞（把未核到的时钟差异
陈述成了已核查的等价）。独立审查不替代覆盖当前 HEAD 的机器人 code review。

## 19. 机器人 code review 第七轮四条 thread 处置（2026-09-20，合并后 HEAD）

机器人先回「usage limits」，随后仍对 `ea2ac8e` 产出 4 条 thread（2×P1、2×P2）。全部**采纳并
修复**，无拒绝项。

### P1 `opspilot/tools/registry.py:183` — 参数描述可夹带具体 endpoint

成立。`ToolDescription` 五个字段都过 `_ENDPOINT` 检查（`:263-266`），但 `ParameterSpec.description`
是**同一个** C3 第 8 节模型可见面（其 docstring 原文如此），此前只校验 `isinstance(str)`。修复
（`04d0e14`）对该字段施加同一条确定性 URL 规则。边界不变、不做关键词扫描，理由沿用
`ToolDescription` docstring，并新增
`test_a_parameter_description_may_still_name_a_reserved_word` 钉住「描述里可以出现
endpoint/token 这类词」。先红后绿：修复前两个 URL 用例 DID NOT RAISE。

### P1 `opspilot/persistence.py:600` — 同一 epoch 内重复 operation_id 只计一次

成立，且已在真实 PostgreSQL 上复现：两次真实读取（5.0s、7.0s）→ `ops=1 secs=7.0`，即两次
读取都发出、只计 1 次、秒数取 max 而非求和，真实读取可越过 20 次 / 240 秒。

**语义按合同定，未采用机器人提出的「独占预留、拒绝第二次」**：C3 第 13 节 `:457`「重试计入
次数和费用」，第 4 节 `:258` 明确不承诺外部查询 exactly-once。合同要求的是计费，不是去重或
拒绝。修复（`b559f60`）把计费行从 `(run, epoch, operation_id)` 改为**每次真实派发一行**，
主键 `dispatch_id`：

- `charge_tool` 新增必填 `dispatch_id`（与 `max_operations` 同理由：给默认值会悄悄退回旧行为）。
- 同一 `dispatch_id` 的后续调用仍按 max 抬升秒数，因此
  `test_charge_tool_counts_once_per_operation_and_settles_seconds_upward` 钉住的「重放结算不
  改变任何东西、绝不向下计费」原样成立。
- `ToolUsageLedger.charge` / `DurableToolLedger.charge` 透传；执行器在 `_run()` 为每次读取生成
  一个 id，派发前计次与取回后结算共用它。
- `opspilot_tool_charges` 主键改为 `dispatch_id`，`install()` 内含幂等就地迁移（本表由本分支
  引入，尚未进入任何产品环境）。

修复后同一场景 `ops=2 secs=12.0`。新增两个用例：重复派发都被计费（含重放结算仍幂等）、
重复派发不能越过上限。既有用例断言一字未改，只按新签名补 `dispatch_id`。

### P2 `opspilot/tools/registry.py:420` — selector 可夹带认证材料

成立。`selector` 此前只校验 str->str，受审目标配置可放 `{"authorization": "Bearer ..."}` 并原样
流进 `TransportRequest.selector`——而后者 docstring 写的是 "nothing secret"，与该类已拒绝的
endpoint userinfo / query / fragment 是同一种绕过 `credential_ref` 的路径。修复（`ff40696`）新增
`_AUTHENTICATION_KEYS`（`RESERVED_PARAMETERS` 中承载凭据的子集，不含 `target`/`target_id` 这类
定位词），selector 键按小写命中即拒；普通定位元数据不受影响，由
`test_target_selectors_still_accept_plain_targeting_metadata` 钉住。

### P2 `opspilot/persistence.py:591` — 计费的 Run 未绑定租约事故

成立，且已复现：`charge_tool` 只按 `r.run_id` 查询，一个把事故 A 的 `incident_id` 与事故 B 的
`run_id` 组合的 Lease，在 B 的 owner/epoch/代际相同时会锁住 A 却改 B 的 Run，同时绕开本文件
incident→run 的锁序。修复（`176a45b`）同时绑定 `i.incident_id`，与 `renew_lease()`/
`lease_current()` 一致。先红后绿：修复前 DID NOT RAISE 且 B 被实际计费，修复后 `CONTROL_DENIED`
且 B 停在 `(0, 0.0)`。

### 验证

```text
make check → 1323 passed, 136 skipped, 2 xfailed（ruff / ruff format / mypy 全过）
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
```

### 未完成/限制

- 本轮四条均为采纳修复，无拒绝项；GitHub 上逐条回复并 resolve。
- 修复后 HEAD 再次变化，需要覆盖新 HEAD 的复审；机器人额度状态不稳定，缺口按前节口径如实记录。
- 第 18 节记录的 `charge_tool` 栅栏回归测试维度缺口不变（仅事故代际维度入仓）。

## 20. 机器人 code review 第八轮八条 thread 处置（2026-09-20）

机器人对 `d0a7461` 与 `ff40696` 各出一轮，合计 8 条（3×P1、5×P2）。**7 条采纳修复，1 条拒绝**。

### 采纳（6 条代码修复，提交 `f5dee7f`）

| 条目 | 判定依据 | 处置 |
| --- | --- | --- |
| P1 `_ENDPOINT` 大小写敏感 | 实测 `_ENDPOINT.search("see HTTPS://...")` 为 `False`，且 `ParameterSpec` 接受该描述——第七轮刚补的规则被绕过。URI scheme 本不区分大小写（RFC 3986） | 改 `re.IGNORECASE` |
| P2 保留参数名精确大小写 | 实测 `"Authorization" in RESERVED_PARAMETERS` 为 `False` | 按小写折叠比较，与上一轮 selector 检查同口径 |
| P2 `OverflowError` 逃逸 | 实测 `fromisoformat("0001-01-01T00:00:00+14:00")` 成功、`.astimezone(utc)` 抛 `OverflowError` | 一并按无效窗口处理 |
| P2 超长整数 `canonical` 抛错逃逸 | 实测 `int_max_str_digits=4300`，`json.dumps({"n": 10**4300})` 抛 `ValueError`，而 `accepts()` 放行 | 转 `error`/`INVALID_PARAMS`，未新增 reason 码 |
| P2 结算失败把不确定接触升级为 confirmed | fetch 的失败元组第三项即接触分类（超时/不可用均为 `possible`），结算失败路径写死 `confirmed` | 沿用 `failure[2]`；`ToolBudgetExhausted` 同形状路径一并修正 |
| P1 响应 source 区间未按授权校验 | `TransportResponse` 合同写明这是「本响应所代表的真实来源时间戳」（`executor.py:251-253`） | 超出 **scope** 窗口即 `denied`/`WINDOW_OUT_OF_SCOPE`；按裸边界比较以允许单点瞬时 |

六条均先红后绿：保存 `git diff` 补丁后 `git apply -R` 仅还原实现、保留新测试，确认 9 个新用例
全部失败，再还原实现确认全绿（**过程中发现并修正了一个假绿用例**：`OverflowError` 用例原本
用 `start == end`，会先撞上 `start >= end` 的 `ValueError`，根本走不到 UTC 转换）。

**一处既有用例输入被改动并已在 PR 上说明**：`test_single_source_instant_and_offset_are_preserved`
原用 `NOW`（01:05）作来源瞬时，而 fixture scope 窗口是 00:00–01:00，新规则下即越权数据。该用例
目的是「保留非 UTC 偏移」，取值偶然，改为窗口内同偏移瞬时并注明原委；**断言未改**，新规则另立
专门用例（含「区间在 scope 内但与请求窗口不同仍应采纳」的反向用例）。

### 采纳（1 条以协议义务形式，提交 `e6976d5`）

P1「Fence evidence adoption inside the commit」：所指窗口真实存在，但结论分两层——

1. **产品的原子栅栏已存在**：`DurableStore.commit_tool` 在同一事务同一行锁内复核
   `step_generation != lease.control_generation` 与 `_lease_revoked(...)`，命中即转 `_late_result`
   仅存历史。这正是本条要求的原子校验。
2. **执行器已 fail closed**：`_register()` 在实现抛异常或返回引用不匹配时返回 `False`，执行器返回
   `EVIDENCE_NOT_COMMITTED`，内容不交给模型。

真实缺口是 `EvidenceSink` 协议没写出这条义务，实现可满足类型却丢掉保证。已在其 docstring 中
明确要求，并写明 `commit_tool` 的既有做法与执行器的 fail-closed 行为。耐久实现随组合层接线落地
（既有交接项不变）。

### 拒绝（1 条，已在 PR 上说明依据）

P2「Let elapsed timeout override transport failures」——**前提不成立且会制造假审计事实**：

- 该条称此顺序违反「executor's stated rule that any fetch which outlasts the handed-off request
  bound is a timeout」。仓库中不存在该规则：`GATEWAY_TIMEOUT` 在 `opspilot/` 下只出现一次，就是
  被指的那一行，无 docstring 亦无注释表述过它。
- 按提案前置 `elapsed > timeout` 后，transport 自报超时的调用会从
  `("timeout", "TOOL_TIMEOUT", "possible")` 改报 `GATEWAY_TIMEOUT` + `confirmed`，把「是否真的读到
  来源不可知」升级为「确认联系过来源」——正是**同一轮另一条 P2**（`:649`）要求消除的缺陷类别。
- 超时事实未丢失：记录在 `operation.elapsed_seconds`、`executor.tool_seconds_used`，并按 per-dispatch
  计费落进耐久账本。

### 验证

```text
make check → 1334 passed, 136 skipped, 2 xfailed（ruff / ruff format / mypy 全过）
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
```

### 未完成/限制

- 8 条已全部回复并 resolve（7 采纳 / 1 拒绝并说明依据）。
- 每轮修复都会改变 HEAD，机器人随即产出新一轮；本轮之后是否继续由用户决定。
- 第 18 节记录的 `charge_tool` 栅栏回归测试维度缺口不变。

## 21. 机器人 code review 第九轮三条 thread 处置（2026-09-20）

三条全部采纳修复（提交 `b0b4d07`），其中**两条是第八轮 per-dispatch 计费改动引出的后果**。

### P1 证据身份仍是稳定的 operation_id

per-dispatch 计费明确允许同一 operation 被派发两次（重试、重复投递），两次响应的字节与观察
时间可以不同。共用一个 `evidence_id` 会让按它做键的 sink 丢弃或覆盖其中一次并返回幸存者的
引用，而 `_register()` 正是拿「返回引用 == `record.evidence_id`」当作新记录已提交的证明——
模型视图于是可能描述它自己的证据链接解析不到的字节。

改为 `evidence_id = f"{operation_id}:{dispatch_id}"`，视图中 `operation_id` 原样保留作关联。

**既有断言被改动并已在 PR 上说明**：`test_ok_outcome_registers_raw_bytes_view_and_both_hashes`
原断言 `record.evidence_id == record.operation.operation_id`，钉住的正是本条要改掉的行为；改为
断言前缀关系与视图两字段各自正确，并新增 `test_two_dispatches_of_one_operation_get_distinct_evidence_ids`。

### P1 control 撤销导致的结算拒绝丢失历史

`DurableToolLedger.charge` 此前只翻译 `OPERATION_BUDGET_EXHAUSTED`，`CONTROL_DENIED` 被
`_charge()` 的 `except Exception` 塌缩成 `False`，执行器在自己的 control 复读之前返回：真的读到
来源的观察被逐出证据审计，且上报 `CONTROL_UNAVAILABLE` 而非权威暂停。

新增 `ToolControlDenied` 类型信号（与既有 `ToolBudgetExhausted` 同一做法），由 ledger 翻译，
**`_charge()` 须连同一并向上抛**——实现时踩到过：只加 except 分支而不改 `_charge`，异常仍被吞。
执行器继续走 control 复读：看到暂停/代际变化则上报权威原因并 history-only 登记证据；复读看不到
异常则仍以 `CONTROL_UNAVAILABLE` 拒绝（权威写方说了不行，且「费用无法记录的结果从不采纳」不松动）。
两条路径各有用例。

### P2 install() 每次重建计费表主键

主键替换要 ACCESS EXCLUSIVE 锁并重建索引，而本 store 设了 5 秒 statement timeout，随表增长会
阻塞正在进行的计费甚至稳定失败。整段迁移改为条件化 DO 块，只在检测到旧主键形状时执行。

真实库双向验证：连续两次 `install()` 后主键索引 oid 不变（21160 → 21160，未重建）；另造一张
`(run_id, epoch, operation_id)` 旧形状且有数据的表，同一段 DO 块仍正确迁移并回填（backfilled rows: 1）。

### 验证

```text
make check → 1337 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
```

### 未完成/限制

- 三条已全部回复并 resolve。累计 38 条 thread，全部有采纳或拒绝结论。
- 观察到的循环特征：第八、九两轮的多数发现都是前一轮修复引出的后果。是否继续由用户决定。

## 22. 机器人 code review 第十轮三条 thread 处置（2026-09-20）

三条全部采纳修复（提交 `f9202f7`）。**第 1 条是第九轮修复引入的回归。**

### P1 `ToolControlDenied` 在派发前逃出 execute()（本分支回归）

第九轮为让结算路径拿到权威原因，把 `_charge()` 改成重新抛出 `ToolControlDenied`，但只在结算处
接住；派发前那次计费仍只捕 `ToolBudgetExhausted`。实测确认逃逸：

```text
*** EXCEPTION ESCAPED execute(): ToolControlDenied: CONTROL_DENIED
transport called? False
```

修复：该处也接住并按权威 control 复读给出真实原因（复读无异常则 `CONTROL_UNAVAILABLE`）；此时
尚未派发，不登记证据。控制判定顺序抽成 `_control_invalid()`，派发前拒绝 / 结算拒绝 / 取回后复检
三处共用，避免再次漂移。

### P1 模型可见面的 URI 检测只认 HTTP(S)

`postgresql://user:pass@db.internal/x` 会把凭据直接带进送给模型的文本；`grpc://`、`wss://`、
`file://` 同样是具体 endpoint。拆成两个检测器：新增通用 `_MODEL_VISIBLE_URI`（任意 scheme）用于
两个模型可见面；`_ENDPOINT` 保持 HTTP(S) 专用，继续只管注册的传输 endpoint——放宽它会让非 HTTP
scheme 变成合法注册 endpoint，那是另一回事。

### P2 认证参数别名

大小写折叠无法拒绝不在 `RESERVED_PARAMETERS` 里的名字（`X-Api-Key`、`access_token`、
`proxy_authorization`）。改为两条规则并存：明确凭据词按折叠后子串匹配；`auth`/`cookie`/
`session`/`sig` 这类普通英文片段按分词精确匹配。

**第一版写成统一子串匹配，被自己新加的用例当场拒掉**：`author_filter` 折叠后含 `auth`。该假阳性
用例（`test_ordinary_parameter_names_are_still_accepted`）保留在仓库，防止后人再收紧成纯子串规则。

### 验证

```text
make check → 1353 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
红绿对照：git apply -R 仅还原实现、保留新测试 → 11 个用例失败；还原实现 → 全绿
```

### 未完成/限制

- 三条已回复并 resolve；累计 41 条 thread。
- **本轮的教训已记录**：第 1 条与第九轮两条一样，都是前一轮修复的后果。每次推送都会触发新一轮
  审查，本任务记录中的「循环无自然终点」观察继续成立，是否继续由用户决定。
- 每次向用户汇报 PR 状态前必须重新查询 thread，快照会在数分钟内过期（上一次汇报即因此把
  已出现新一轮的 PR 说成零未处理）。

## 23. 机器人 code review 第十一轮一条 thread 处置（2026-09-20）

P1「Recheck control before returning transport failures」——成立，已修（`3e3fa4a`）。

transport 异常路径的返回排在 control 复读之前，飞行中的人工暂停/取消会让结果报
`TOOL_TIMEOUT`/`SOURCE_UNAVAILABLE`，权威决定完全不出现在 outcome 与审计路径中。修复在该返回
之前插入一次 control 复读，命中则以权威原因拒绝，接触分类沿用 fetch 的判定（暂停并不告诉我们
来源是否被读到，仍是 `possible`）。

**刻意的范围限制**：此处只查人工/控制决定，不查 deadline。把 deadline 也放进来会把 transport
自报的超时改写成网关超时，从而把 `possible` 接触升级为 `confirmed`——正是第 20 节记录的、第八轮
已拒绝的那条提案所带来的假审计事实。为此把判定拆成 `_control_decision()`（仅决定）与
`_control_invalid()`（决定 + deadline）。

验证：`make check` → 1355 passed；PG 集成 98 passed；先红后绿（还原实现后暂停用例失败）。

### 轮次统计（截至第十一轮）

| 轮次 | 条数 | 采纳 | 拒绝 | 其中「由上一轮修复引出」 |
| --- | --- | --- | --- | --- |
| 7 | 4 | 4 | 0 | 0 |
| 8 | 8 | 7 | 1 | 0 |
| 9 | 3 | 3 | 0 | 2 |
| 10 | 3 | 3 | 0 | 1（本分支回归：`ToolControlDenied` 逃逸） |
| 11 | 1 | 1 | 0 | 1（第九/十轮 `settlement_denied` 路径的延伸） |
| 12 | 1 | 1 | 0 | 1（第十一轮的修复只打在一条分支上） |
| 13 | 2 | 2 | 0 | 1（第十二轮仍漏掉两个结算出口） |
| 14 | 1 | 1 | 0 | 0（旧的行级非有限数检查覆盖不足） |
| 15 | 3 | 3 | 0 | 1（第十轮的认证名规则未覆盖全大写） |
| 16 | 3 | 3 | 0 | 1（第十五轮的可编码性只逐字段补） |
| 17 | 2 | 2 | 0 | 2（第十六轮两条规则的收窄边界各漏一类） |
| 18 | 2 | 2 | 0 | 2（第十六轮两处处置各留半成品） |
| 19 | 1 | 1 | 0 | 1（第十八轮兜底原因写死了未核实的具体决定） |
| 20 | 2 | 2 | 0 | 1（第十二轮的统一入口丢掉了 history 保留） |
| 21 | 2 | 2 | 0 | 1（第二十轮新增的 _history 绕过响应校验） |
| 22 | 1 | 1 | 0 | 0（endpoint 检测器的第四种形式） |
| 23 | 2 | 2 | 0 | 1（第二十二轮 userinfo 分支的边界划错） |
| 24 | 2 | 2 | 0 | 2（第二十三轮两条修复各自不完整） |
| 25 | 1 | 1 | 0 | 1（endpoint 检测器点分主机分支仍限 ASCII） |
| 26 | 1 | 1 | 0 | 1（同一规则的带端口分支仍限 ASCII） |
| 27 | 1 | 部分 | 部分 | 1（同一规则，但越过了可判定边界） |
| 28 | 1 | 1 | 0 | 1（同一规则，IPv6 zone id） |
| 29 | 2 | 2 | 0 | 1（第二十四轮预留模型的后果） |
| 30 | 1 | 1 | 0 | 1（同上，执行器一侧） |
| 31 | 2 | 2 | 0 | 2（endpoint 规则与 astimezone 同类各漏一处） |
| 32 | 1 | 1 | 0 | 0（执行器未带全局/目标控制版本） |
| 33 | 3 | 3 | 0 | 1（第三十二轮的默认值 fail open） |
| 34 | 2 | 1 | 1 记录交接 | 2（第三十三轮两条各留半） |

累计 78 条 thread 全部有结论。第 9–31 轮共 40 条中有 26 条源自前一轮修复，单轮条数
8 → 3 → 3 → 1 → 1 → 2 → 1 → 3 → 3 → 2 → 2 → 1 → 2 → 2 → 1 → 2 → 2 → 1 → 1 → 1 → 1 → 2 → 1 → 2 → 1。
第 9–32 轮共 41 条中有 26 条源自前一轮修复。用户已明确：继续处理，采纳或拒绝由执行者按实际
情况判断。

## 43. 机器人 code review 第三十一轮两条 thread 处置（2026-09-20）

两条均采纳（`0e3dcbb`）。

### P1 无端口的方括号 IPv6

`_SCHEMELESS_ENDPOINT` 的方括号形式仍要求 `:port`，`[fd00::1]`、`[::1]`、`[fe80::1%eth0]` 漏检。
方括号本身才是锚点，端口一直只是替它站岗。

**去掉端口要求需要补新判别式**，否则 `数组 [1:2]`、`区间 [a:b]`、`矩阵 [i:j]`（内容同样是十六进制
字符加冒号）会一并被拒。判别式取**冒号数量**：前瞻要求方括号内至少两个冒号，真实 IPv6 字面量
必然满足，切片/区间只有一个。两方向均有用例。无方括号的形式仍要求数字端口——没有锚点时端口是
唯一判别特征。

### P2 deadline 归一化失败抛裸异常

`QueryScope` 的 deadline 通过 tzinfo/utcoffset 校验后，在 `astimezone` 抛裸 `OverflowError`，绕过
固定码边界。改为捕获并报 `INVALID_DEADLINE`。

**同类问题只修被点名的那一处**：`Window.parse()` 对同一类时间戳在本任务前面几轮就已如此处理
（第 26 节），我当时只修了窗口那一侧，没有回头看同一模块里另一处同样调用 `astimezone` 的地方。
这与第 42 节的失效注释、第 36 节的无效修复属同一模式，均由机器人而非自查发现。

验证：`make check` → 1467 passed；PG 集成 103 passed；先红后绿。


## 42. 机器人 code review 第三十轮一条 thread 处置（2026-09-20）

P2「Settle the reservation before returning on suspension」——成立，已修（`26f2ef6`）。第二十九轮
是存储侧，本轮是执行器侧的同一问题。

派发前的 control 复检或 deadline 判定拒绝时直接返回，而刚做的预留是整段 timeout：一次审计记为
`sent=false` 的操作能永久吃掉最多 30 秒，反复控制竞态可虚假耗尽 Run 的时间预算。

**同时暴露一句已失效的注释**：该处原写「once billed it stays spent，unknown cost stays occupied」，
写于预充值仍是 `0.0` 秒、无可退还之时；第二十四轮改为整段预留后即不成立，而当时未回头更正。
本轮一并改写。

处置：这些路径上读取证明没有发出，按零成本结算预留。**操作计数刻意不释放**——它在派发前记录正是
为了让空窗期的崩溃无法隐藏一次尝试；释放它需要区分「派发前被拒」与「派发前崩溃」，而耐久预充值
存在的意义恰恰是不必做这个区分。因此释放实现为对同一 dispatch 的结算（正好走第 41 节打开的
「已存在预留可结算」路径），而非撤销。释放为 best effort，账本失败不得把人工决定变成存储错误。

既有断言 `len(ledger.charges) == 1`（「预充值不退还」）改为期望「预留 + 零结算」并断言同属一个
dispatch，理由写在用例注释。

**方法教训（第六次）**：改变一项语义时，必须回头检查**依赖该语义写下的注释与断言**，而不只是
代码路径。本轮的失效注释与上一轮的过期断言都属这一类，都是由机器人而非我自己发现的。

验证：`make check` → 1460 passed；PG 集成 103 passed；先红后绿。


## 41. 机器人 code review 第二十九轮两条 thread 处置（2026-09-20）

两条均采纳（`cd0a3d3`）。

### P2 撤销后预留无法结算（第二十四轮预留模型的后果）

租约撤销后栅栏连**结算**一并拒绝，预留的整段超时永远占着 `tool_seconds_used`；恢复后的尝试继承
虚高总量，可能因根本没花掉的秒数而 `TIME_BUDGET_EXHAUSTED`。

改为先查本次 dispatch 的计费行：**行已存在即允许结算；行不存在（要新建预留）时仍按
`_lease_revoked` 拒绝**。这是在安全栅栏上开的一个口子，边界记录如下：

- 结算只把已知实际耗时写回本租约自己建立的那一行（键含 dispatch_id + run + epoch + operation_id），
  不新建预留、不采纳结果、无对外动作；
- 人工决定的上报未丢失：执行器取回后的 control 复读仍按权威原因拒绝并 history-only 登记（第
  32、33 节已建立且有用例）；
- 不开口子的代价是记账永久失真，且失真方向恰好误伤恢复后的合法执行。

PG 用例同时钉住两侧：撤销后 30 秒预留可结算回 2 秒；同一撤销租约新建派发仍 `CONTROL_DENIED`
且操作计数不变。

### P2 endpoint 缺少 authority 校验

`https:///api`、`http://:9090`、`https://metrics.internal:99999` 均注册成功（实测），随后每次授权
调用都会带着不可用 endpoint 发往传输层。新增 `_usable_authority()`：`urlsplit` 解析，host 非空、
端口在 1..65535；`urlsplit().port` 对越界端口抛的 `ValueError` 一并捕获转 `INVALID_ENDPOINT`，
避免又一个逃出固定码契约的裸异常。正常 endpoint 与大写 scheme 不受影响，两方向均有用例。

验证：`make check` → 1458 passed；PG 集成 103 passed；先红后绿。


## 40. 机器人 code review 第二十八轮一条 thread 处置（2026-09-20）

P1「Reject scoped IPv6 endpoints in model-visible text」——成立，已修（`cc79fdb`）。

方括号 IPv6 的字符类不含 zone id，`[fe80::1%eth0]:4317` 漏检。符合既有判据：方括号加数字端口
本身即无歧义锚点，zone 是地址的一部分而非新形式，放宽不引入歧义。反向用例钉住 `数组 [1:2]`、
`区间 [a:b]`、`矩阵 [i:j] 切片` 仍合法。

两个检测器共用同一处字符类，**本次先确认两处都在再一并修改**，而不是只改被点名的那一处——这正是
第 28、33 节反复记录的教训。

验证：`make check` → 1452 passed；PG 集成 102 passed；先红后绿。


## 39. 机器人 code review 第二十七轮一条 thread 处置（2026-09-20，部分拒绝）

P1「Reject single-label protocol-relative endpoints」——**部分采纳、部分拒绝**（`b5ca0ac`）。
这是 endpoint 规则连续七轮以来第一条被拒的部分，依据是可判定性，不是偏好。

### 拒绝：描述字段不采用

要求拒绝的与必须放行的，结构完全相同：

```text
要拒：//prometheus/api   //localhost/metrics   //监控/指标
要放：//shared/config    见 //文档/说明          正则 //a/b/ 的写法
```

两组都是 `//` + 标签 + `/` + 标签，没有任何语法特征能分开——唯一差别是「prometheus 恰好是个
服务名」，那是词表问题不是语法问题。采纳它等于禁止描述里出现任何 `//a/b`，并须删除
`paths like //shared/config` 这条记录既有决定的反向用例。

**与前六轮的分界**：那六轮每条都有无歧义锚点（`://`、`//`+userinfo、`//`+点分 authority、
`//`+数字端口），逐条接受并标注代价；这一条没有锚点，接受它就不再是「识别 endpoint」，而是
「禁止在描述里写两段路径」。且该规则本就无法完备——不带 `//` 的 `prometheus/api` 同样是
endpoint 且同样无法检测，接受本条只买到无界空间里一个任意切片。

### 采纳：参数名无条件拒绝含 `//`

参数名不是散文，标识符没有理由包含 `//`，因此该面直接拒绝，不必区分 endpoint 与路径，也不产生
散文那一半的误判。报告中的名称半边由此关闭。

### 根治（仍待用户决定）

同意机器人给出的另一选项——**结构化 endpoint 校验**：位置信息由注册表字段承载，散文字段不再自由
书写。七轮证明正则枚举收敛不了。属 C3 第 8 节合同变更，已在 PR 与本记录写明，不在 review 处置的
自主范围内。

验证：`make check` → 1447 passed；PG 集成 102 passed。


## 38. 机器人 code review 第二十六轮一条 thread 处置（2026-09-20）

P1「Detect Unicode ported protocol-relative hosts」——成立，已修（`981047a`）。

`//` 系列里最后一个仍限 ASCII 的分支：带端口的单标签主机。`//监控:4317` 与 `//prometheus:9090`
是同一件事，锚点是 `//` 加数字端口。

**明确记录的代价**：`见 //步骤:30 的说明` 这类中文散文现在也被拒，并为此单列用例而非隐去。接受
理由是另一边更糟——保持 ASCII-only 意味着 `//监控:4317` 能到达模型而 `//prometheus:9090` 不能。
无 `//` 锚点的散文不受影响（`按步骤:30 秒聚合` 仍合法），因为 `_SCHEMELESS_ENDPOINT` 刻意保持
ASCII：那条规则没有锚点，放开会大面积误伤中文描述。两个方向均有用例。

第 37 节提出的结构性判断不变：六轮扩充说明正则枚举收敛不了此问题，根治需要描述面结构化
（C3 第 8 节合同变更），已在 PR 上写明，留待用户决定。

验证：`make check` → 1444 passed；PG 集成 102 passed；先红后绿。


## 37. 机器人 code review 第二十五轮一条 thread 处置（2026-09-20）

P1「Detect Unicode protocol-relative hostnames」——成立，已修（`1122b77`）。

协议相对分支（无 userinfo）仍要求 ASCII 主机名，`//监控.内部/api`、`//监控.内部:4317/api` 漏检。
判据与第 32、36 节相同：锚点是 `//` 加点分 authority，字母表不是判据；`scheme://` 与 userinfo
两个分支已先后放开，这是最后一个——不放开即三条规则两套标准。

末段仍要求至少两个字母，`a//b 比较`、`见 //说明 一节`、`// TODO`、`//shared/config` 不匹配；
新增 CJK 散文用例与既有中文冒号用例一起钉住边界，确保规则不会反过来拒掉本仓库自身语言的描述。

### 已在 PR 上提出的结构性判断（尚未执行，需要合同变更）

这已是 endpoint 检测器第五轮扩充。逐条成立且都已修，但**靠正则枚举形式收敛不了**：每轮都能找到
新写法，每次放宽都在挤压合法描述的空间。真正能终结它的是让描述面结构化——endpoint / target 由
注册表字段承载，散文字段不再允许自由书写位置信息。那是合同变更（C3 第 8 节的模型可见面定义），
不在 review 处置的自主范围内，已在 thread 回复中写明，留待用户决定。在此之前继续按「有无无歧义
锚点」逐条判断。

验证：`make check` → 1441 passed；PG 集成 102 passed；先红后绿。


## 36. 机器人 code review 第二十四轮两条 thread 处置（2026-09-20）

两条均采纳（`d735cac`）。**第 2 条指出第二十三轮的修复并未真正关掉它声称关掉的竞态，且新增用例
把漏洞写成了期望行为——本任务迄今最需要记住的一次。**

### P1 userinfo 分支的 host 仍限 ASCII

`//reader:secret@监控/api` 漏检。userinfo 已构成无歧义 authority，host 字母表与判定无关——与第 32
节接受「`scheme://` 之后不限字母表」同一依据，第二十三轮我在 userinfo 分支上没保持一致。

### P2 秒数上限：从「判已用量」改为「预留与结算」

第二十三轮只在原子 UPDATE 加了「已用秒数 < 上限」，而派发前计费传的是 `0.0`，两个执行器各读到
239 秒仍都能通过、各自派发、各自无条件结算，Run 停在 241 秒。**且我当时新增的用例断言 240 → 242
成立，等于把「结算可越过上限」写成期望行为。**

按合同重做（C3 第 13 节 `:457`：「预算在 PostgreSQL 原子预留和结算，未知费用保持占用」）：

- `dispatch_id` 首次计费即**预留**（seconds 为本次派发被授权的最长时间），门禁为
  `tool_seconds_used + 预留 <= 上限`，放不下则在读取发出前拒绝；执行器改为传 `timeout` 而非 `0.0`；
- **结算释放预留**并记录实际耗时，差值可为负；重复结算不把已记录实际耗时改小；
- 传输超出时限时按真实耗时结算，可高于预留——上限约束「可以开始什么」，记录说明「实际花了多少」，
  两者不互相掩盖（单独立用例）；
- 表新增 `reserved` 列、`seconds` 允许 NULL，迁移随 `install()` 条件化执行。

既有断言 `charges == [(op, 0.0), (op, 3.0)]` 改为 `[(op, 10.0), (op, 3.0)]`。

**方法教训（第五次，最重要的一次）**：前四次是「修实例漏整类」；这一次是**修复本身无效而我没有
验证到位**——我写的用例只覆盖了「已用量已达上限」的情形，没有构造「两个执行器同时从阈值下出发」
的真实竞态，因此测试通过并不代表缺陷关闭。今后处置并发/上限类缺陷，用例必须直接复现报告中描述
的那个竞态序列，而不是一个更容易构造的近似场景。

### 验证

```text
make check → 1436 passed, 140 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 102 passed, 38 skipped
```


## 35. 机器人 code review 第二十三轮两条 thread 处置（2026-09-20）

两条均采纳（`12b82bc`）。

### P1 userinfo + 单标签 host 的协议相对 URL

第二十二轮让 userinfo 可选，却仍要求 host 带点/端口/方括号，于是
`//reader:secret@prometheus/api`——**单标签 host + 内联凭据**，危害更大的那一种——反而漏过。
userinfo 本身即无歧义 authority 标记（普通散文不写 `//x@y`），新增该分支。反向用例钉住
「邮件 a@b 的格式」仍是散文：标记是 `//` 加 userinfo，不是文本任意位置的 `@`。

### P2 耐久层未原子强制秒数上限

原子 UPDATE 只判 `tool_operations_used`；秒数仅在进程内把关，两个执行器各读到 239 秒即可各按
「还剩 1 秒」派发并结算，Run 停在 241 秒而两次观察都被采纳。次数上限早先已原子化，秒数没有，
是一处不对称。

改为同一条锁内 UPDATE 一并判定，与次数上限同形：`charge_tool` 新增必填 `max_tool_seconds`；
行锁下的计数用于区分触发的上限并报告与进程内一致的原因码；**结算已计次的派发仍不受任一上限
约束**（用例钉住 240 → 242 秒仍可结算）；`DurableToolLedger` 透传并把 `TIME_BUDGET_EXHAUSTED`
翻译成新的 `ToolTimeBudgetExhausted`，执行器据此上报与其进程内预检一致的原因码。

### 验证

```text
make check → 1434 passed, 138 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 100 passed, 38 skipped
两条均先红后绿（registry 规则的红检验单独还原 registry.py，避免新导出缺失导致收集失败）
```


## 34. 机器人 code review 第二十二轮一条 thread 处置（2026-09-20）

P1「Reject protocol-relative URLs in model-visible prose」——成立，已修（`ba5f5f1`）。

`//authority/path` 既不需要 scheme 也不需要端口，同时躲过两个检测器；给出的例子
`//reader:secret@metrics.internal/api` 还把**凭据一并内联**进模型可见文本。

新增 `_PROTOCOL_RELATIVE_URI`。**判定依据与第 32 节接受「`://` 之后不限字母表」时相同**：`//` 加
一个*可识别的 authority* 是无歧义锚点；authority 仅在带点、带端口、带方括号或带 userinfo 时才
算数，因此 `// TODO`、`//shared/config`、`//notes`、`a//b 比较` 仍是普通散文（四条反向用例）。

**同时做了结构性收口**：三个检测器收到单一谓词 `_endpoint_text()` 之后，五个描述字段、参数描述、
参数名共用它。此前每新增一种形式都要在三处调用点分别改，而前几轮恰恰是这样每次漏掉一个面
（描述改了 selector 漏、描述改了参数名漏）。现在加一种形式即同时覆盖全部模型可见面。

验证：`make check` → 1430 passed；PG 集成 98 passed；先红后绿。


## 33. 机器人 code review 第二十一轮两条 thread 处置（2026-09-20）

两条均采纳（`0f95369`）。第 1 条是第二十轮 `_history()` 引入的缺陷。

### P1 历史路径绕过响应校验

`_history()` 只重复了解析这一步。实测两个后果：

```text
source_start_at="bad" + 暂停 → *** ESCAPED execute(): AttributeError: 'str' object has no attribute 'isoformat'
超限 body + 暂停             → denied SUSPENDED，history committed: 1（绕过 max_result_bytes）
```

异常逃出 `execute()` 尤其严重，且发生在人工控制路径上——本任务前面刚修过数条同类，我自己又开了
一个。

**修法不是在 `_history()` 里补齐检查**（那会留下第三份需同步的规则，正是本任务反复栽跟头的模式），
而是把整条响应校验链抽成 `_inspect()`，返回 `(problem, rows, payload)`，采纳路径与历史路径共用：
凡在采纳路径会被拒绝的响应，历史路径同样不保留。`_inspect()` docstring 写明它是这些规则的唯一
所在地及原因。

### P1 参数名中的 endpoint

参数名只校验类型与认证别名，而它们会成为模型可见 schema 的属性名，`{"prometheus:9090": ...}`
因此绕过了施加于描述的 endpoint 规则——同一个模型可见面，两个字段两套标准。改为施加同一套检查。

### 验证

```text
make check → 1422 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
两条均先红后绿（还原实现后 5 个用例失败，其中 AttributeError 用例直接抛出）
```

### 工具注记

本轮回复因正文含反引号被 zsh 当作命令替换而解析失败（未误发，已核实两条 thread 的 last_author
仍为机器人），改为把正文写入文件并以 `gh api -F b=@file` 传参。后续含代码标记的回复一律走文件。


## 32. 机器人 code review 第二十轮两条 thread 处置（2026-09-20）

两条均采纳（`c7492fa`）。

### P1 暂停落在有效在途响应上时丢失历史

依据是已批准合同 C3 第 4 节（`technical-proposal-2026-09-07.md:112`）：「暂停使相关在途结果
失效，**仅保留历史**」。第十二轮把所有取回后拒绝统一到 `refuse_after_fetch()` 时，控制决定分支
直接返回，既不构造也不登记 history-only 证据——读取到达了来源、字节在手里却被丢弃。

新增 `_history()`：控制决定压过响应分类时，把良构响应以 `adopted=False` 提交并由 outcome 携带。
**两处刻意例外**：来源错误响应不留历史（本模块本就拒绝把错误 body 登记为证据，人工决定不会把它
变成一次观察——此例外是实现中被既有用例逼出来的）；body 从未解析成功时没有可留的观察。

**三处既有断言被改**（第 11/13 轮我自己写的 `sink.records == []`）：它们钉住的正是本轮依合同判定
为缺陷的行为，改为断言存在 `adopted=False` 的历史记录，理由写在用例注释。

### P1 国际化 URI 未被检出

`scheme://` 之后要求 ASCII 字母表，导致 `https://监控.内部/指标` 未检出。该形式本身无歧义，其后
不再限制字母表。

**无 scheme 的 `host:port` 检测器刻意仍保持 ASCII**：非 ASCII 标签 + 冒号 + 数字与本仓库自身语言
的普通散文无法区分（「按步骤:30 秒聚合」「窗口为 12:30 至 13:00」），且该规则没有 `://` 锚点可依。
两条规则依据不同——一条有无歧义锚点，另一条只有启发式——三条中文反向用例钉住该边界。

### 验证

```text
make check → 1417 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
两条均先红后绿
```


## 31. 机器人 code review 第十九轮一条 thread 处置（2026-09-20）

P2「Preserve the actual evidence-commit denial reason」——成立，已修（`bc84b76`）。

`ToolControlDenied` 覆盖租约过期、owner 变更、代际变化、deadline 一整族原因（存储层
`_lease_revoked` 正是这几条的并集）。第十八轮我在提交被拒的兜底里写死
`CONTROL_GENERATION_CHANGED`：本次快照看不到变化时，等于断言了一个没人核实过的具体决定。

**这与本任务早先修掉的「结算失败把不确定接触升级为 confirmed」是同一类——伪造审计事实**，而我
在那一条的处置里刚阐述过它的危害，随后自己又犯了一次。改为中性的 `CONTROL_UNAVAILABLE`，与
`refuse_after_fetch` 中 `settlement_denied` 的兜底同一口径（那处当时就选了中性值，第十八轮没保持
一致）。

未采用「让 sink 传回固定拒绝原因」：精确原因应留在它被决定的地方（存储层 `_late_result` 的审计），
让协议多带一个原因码等于让执行器复述一份它无法验证的结论，制造同一事实的两个可能不一致来源。

既有断言 `CONTROL_GENERATION_CHANGED` 被改为 `CONTROL_UNAVAILABLE`，并把用例改名为
`test_a_control_denied_evidence_commit_does_not_invent_a_generation_change`——它钉住的正是本轮
判定为缺陷的行为，理由写在 docstring。

验证：`make check` → 1410 passed；PG 集成 98 passed。


## 30. 机器人 code review 第十八轮两条 thread 处置（2026-09-20）

两条均采纳（`b6fc843`），且**两条都是第十六轮处置的半成品**。

### P1 sink 的控制拒绝被塌缩

第十六轮把「提交事务内校验 control 代际」写成 `EvidenceSink` 的义务，却没打通回程：
`_register()` 把任何异常塌缩成 `False`，于是**合规实现**的控制拒绝被报成 `EVIDENCE_NOT_COMMITTED`
——提交期栅栏生效了却说不出口。

沿用账本已有模式：sink 用 `ToolControlDenied` 表达控制拒绝，`_register()` 原样上抛，采纳路径走
控制优先级回报权威原因（复读见暂停 → `SUSPENDED`；复读无异常 → `CONTROL_GENERATION_CHANGED`）。
协议 docstring 补明该要求。专门用例钉住普通异常仍报 `EVIDENCE_NOT_COMMITTED`，防止类型信号吞掉
通用映射。

### P2 指纹收口只包了一半

第十六轮宣称「把保证移到唯一入口，任何字段自动覆盖」，实现却只包了 `.encode()`，`canonical()`
自身的序列化失败（`max_window_seconds=10**4300` → 裸 `ValueError`）未覆盖——**即我自己刚宣称要
终结的「修实例、漏整类」，在同一个函数里又犯了一次**。

改为捕获 `ValueError` 整体（`UnicodeEncodeError` 是其子类），编码失败仍报
`INVALID_REGISTRATION_TEXT`，其余序列化失败报 `INVALID_REGISTRATION_VALUE`。选择在收口点翻译而非
给 `max_window_seconds` 加上界：加上界只解决被点名的字段，任何进指纹的数值字段都有同样问题。

**方法教训（第四次）**：建立收口点时，必须同时验证该收口点覆盖了整条失败路径，而不只是当时那个
触发实例。前三次的教训是「找到收口点」，这次补上的是「验证收口点本身是完整的」。

### 验证

```text
make check → 1410 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
两条均先红后绿
```


## 29. 机器人 code review 第十七轮两条 thread 处置（2026-09-20）

两条都要求把第十六轮**刻意收窄**的规则放宽，因此逐条重新判断边界，而不是条件反射式放宽。
结论是两条都成立。

### P1 单标签与 IPv6 的无 scheme endpoint

第十六轮「主机必须带点」的收窄漏掉了集群内最常见的寻址形式：`localhost:4317`、
`prometheus:9090/api`，以及方括号 IPv6 `[fd00::1]:4317`（实测修复前三种全部未检出）。检测器扩为
三种无歧义形式：方括号 IPv6、点分四段 IP、**含字母的主机标签** + 数字端口——「含字母」正是把
`12:30` 挡在外面的条件。

**明确接受的代价**：`step:30` 这类散文现在也被拒。注册期固定码拒绝、操作者可见可改写，对这条
规则而言是正确的失败方向。仍不拦不带端口的裸主机名（理由同第 28 节）。反向用例钉住
`12:30`/`5xx:2xx`/`section 3/4`/`v1.2.3`。

### P2 key / certificate 凭据家族

`private_key`、`aws_access_key_id`、`ssh_key`、`client_certificate` 实测全部放行。补入整族复合词与
无歧义短词。

**裸 `key` 明确不纳入并写入注释**：`label_key`/`group_by_key`/`partition_key` 是只读指标工具的正常
查询参数。凭据家族使用该词处改为按完整复合词匹配。反向用例钉住三者仍被接受。

注释中同时写明：denylist 不可能穷尽（这已是同类第三轮扩充），真正守住边界的是秘密只存在于
`credential_ref` 之后、根本不出现在模型可提议的参数里；这张表的作用是让操作者在注册期看见错误，
不是安全边界本身——避免后人误解其地位。

### 验证

```text
make check → 1406 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
两条均先红后绿
```


## 24. 机器人 code review 第十二轮一条 thread 处置（2026-09-20）

P1「Recheck control before all successful-fetch refusals」——成立，已修（`0cfbac5`）。

第十一轮只把人工决定优先打在 transport 异常那一条分支上，正常返回的响应仍走旧路径：读取已经
发出、飞行中发生人工暂停/取消时，迟到（`GATEWAY_TIMEOUT`）、畸形、超限、来源错误标记、
`data_as_of` 非法、区间非法、区间越权这些分支都会报响应自身的分类而非权威决定，且
`settlement_denied` 在这些分支上被整个忽略。

改为所有「读取已发出之后」的拒绝共用入口 `refuse_after_fetch()`：人工/控制决定 → `settlement_denied`
→ 响应自身分类。接触分类沿用既有取值。deadline 仍不参与（同第 23 节理由）；`_authorize()`/
`_reserve()` 的派发前拒绝不受影响，那里读取尚未发出。

验证：`make check` → 1358 passed；PG 集成 98 passed；先红后绿（还原实现后两个暂停用例失败）。

**方法教训**：第 23 节的修复只覆盖了一条分支就宣告完成，本轮即为其直接后果。同类「优先级/顺序」
修复今后应先枚举同一语义下的全部返回点，再统一改造入口，而不是逐点打补丁。


## 25. 机器人 code review 第十三轮两条 thread 处置（2026-09-20）

两条均采纳修复。

### P1 结算失败的两个出口未经过控制优先级入口（`c98747b`）

第十二轮把「读取已发出之后的拒绝」统一到 `refuse_after_fetch()`，但结算失败的两个出口排在它
之前返回、没有被枚举到：通用存储失败（`CONTROL_UNAVAILABLE`）与耐久上限拒绝
（`OPERATION_BUDGET_EXHAUSTED`）。把 helper 定义提到结算块之前，两个出口一并路由。

现在 fetch 之后的 13 个返回点全部经过同一入口；`invalid` 与 `EVIDENCE_NOT_COMMITTED` 两处是有意
例外（本就排在 `_control_invalid()` 之后，已带权威原因）。

**方法教训（第二次）**：第 24 节刚把教训写成「先枚举全部返回点」，同一次改动仍漏了这两个。
本轮是逐个核对实际 `return` 语句后改的，并在提交信息中列出例外及理由。

### P2 采纳前未拒绝来自未来的来源时间戳（`c5bd96d`）

修复前实测：`data_as_of` 设为一小时后 → `status=ok`、`freshness_seconds=-3600.0`、`adopted=True`、
证据已登记、模型看到 `content`。不可能的未来元数据因此显得格外新鲜。

**按整类修**：同样的洞在 `source_start_at`/`source_end_at` 上也存在（scope 窗口伸向未来时，
声称来自未来的区间同样被采纳，实测 `adopted=True`）。统一规定：任何观察都不得声称来源时间戳
晚于它自己读取完成的时刻。

设计取舍：严格比较，不设时钟偏移容差——容差只能是任意常数，而 fail-closed 会把来源时钟错误
暴露为操作者可见的拒绝，而不是悄悄记下损坏的新鲜度。`data_as_of == finished_at` 仍有效，边界
由用例钉住。若后续在真实来源上发现该严格度不可接受，应作为有数据支撑的显式决策修改。

### 验证

```text
make check → 1363 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
两条均先红后绿
```


## 26. 机器人 code review 第十四轮一条 thread 处置（2026-09-20）

P2「Reject non-finite values throughout source payloads」——成立，已修（`1c31f72`）。

此前的非有限数校验只作用于 `result_path` 下的行，而 `json.loads` 默认接受
`NaN`/`Infinity`/`-Infinity`。复现：`{"meta": NaN, "data": {"result": [{"value": 1}]}}` →
`status=ok`、`adopted=True`、证据已登记，而**留存为证据的原始字节不是合法严格 JSON**，用拒绝
非标准常量的解码器读取直接失败。

改为在解码整个 body 时用 `parse_constant` 拒绝这三个常量；行级 `math.isfinite` 保留作纵深防御。

**红绿对照印证了覆盖缺口**：4 个参数化用例中 3 个在修复前通过（漏网），唯一被旧检查拦住的正是
行内 `{"value": NaN}`。另新增性质用例
`test_committed_evidence_bytes_always_parse_under_a_strict_reader`，把「留存证据可被严格解码器
重新解析」这条规则存在的理由本身钉住。

验证：`make check` → 1368 passed；PG 集成 98 passed。


## 27. 机器人 code review 第十五轮三条 thread 处置（2026-09-20）

三条均采纳修复（`69753f4`）。

### P2 全大写认证名漏网（第十轮规则未做全）

实测：`AUTH`/`AUTH_HEADER`/`COOKIE`/`SESSION`/`SIG` 全部 `refused=False`，而 `auth`/`cookie`
是被拒的。原因是 `_WORDS` 只在小写字符上延续 token，对原始拼写分词会把 `AUTH` 切成四个单字母。

改为先折叠大小写再按分隔符分词，并保留 camelCase 那一遍，取两遍并集：`refreshAuth` 仍被拒，
`author_filter` 仍被接受，两个方向都有用例（参数名与 selector 键两条路径都覆盖）。

### P2 `authorized_at` 记录的不是派发前最后一次检查

字段契约写明是「派发前紧邻的那次授权检查」，实际保留 `_reserve()` 更早的读数；账本写入或派发前
control 快照一慢，审计即低估授权最后核实时刻，且记录中无其他字段可推导。改为在最后一次检查处
更新。`_reserve()` 阶段被拒的场景不受影响（既有 `DEADLINE_EXCEEDED` 用例断言未改仍通过）。

### P2 不可编码文本在注册期未被拒

lone surrogate 是合法 `str` 但不可编码，通过全部注册检查后在
`canonical_hash(...).encode("utf-8")` 抛**裸 `UnicodeEncodeError`**，绕过固定码契约并能中断注册表
构造。在注册期校验：`ToolDescription` 五字段 → `INVALID_DESCRIPTION_TEXT`；
`ParameterSpec.description` → `INVALID_PARAMETER_SPEC`。

**按整类修**：selector 的键与值同样进指纹、同样抛裸异常，一并纳入 `INVALID_SELECTOR`。新增性质
用例：任何被接受的注册都能完成指纹计算而不抛契约外异常（含可编码的中文文本，确认拒的是不可编码
而非非 ASCII）。

### 验证

```text
make check → 1377 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
三条均先红后绿（还原实现后 7 个用例失败）
```


## 28. 机器人 code review 第十六轮三条 thread 处置（2026-09-20）

三条均采纳修复（`daba268`）。本轮最重要的不是三条本身，而是第 3 条促成的方法修正。

### P1 无 scheme 的具体 endpoint 未被拦下

`metrics.internal:9090`、`10.0.0.4:4317` 定位内部目标的精确度与带 scheme 的写法无异。新增
`_SCHEMELESS_ENDPOINT`，**刻意收窄**：主机须为带字母末段的点分名或点分四段 IP，端口须为数字。
`12:30`/`5xx:2xx`/`section 3/4`/`v1.2.3` 均不匹配且各有用例。不带端口的裸主机名仍留人工审查——
在自由文本里检测它会连 `config.yaml` 一起拒，那就变成本模块反复拒绝建立的关键词扫描器。

### P2 畸形 control 快照 fail-open

`bool` 是 `int` 子类：`control_generation=True` 与 scope 的 `1` 判等通过、执行器照常派发；
`suspended=0` 被当作权威「未暂停」。`ControlSnapshot.__post_init__` 改为强制非负真 int 与真 bool。
校验放在构造处，使畸形数据在适配器自己的 `snapshot()` 内失败，落进 `_read_control()` 兜底映射为
`CONTROL_UNAVAILABLE`，即 fail closed；端到端用例确认与「适配器答不上来」同样拒绝。

### P2 可编码性：从逐字段补改为单一入口保证

本条提示参数名仍漏。按提示自查后发现**仍在漏的有四个**：参数名、`result_path` 元素、
`error_classes` 键、`incomplete_marker`。

**这是同一类问题连续第三轮**（第 27 节描述字段 → 同节 selector → 本轮四个字段）。逐字段补已经
证明收敛不了，因此把保证移到唯一入口：`canonical_hash()` 要么成功，要么抛
`ToolContractError("INVALID_REGISTRATION_TEXT")`，任何现在或将来进入指纹的字段自动覆盖。既有的
按字段精确错误码保留以提供定位。模型参数路径不受影响（`UnicodeEncodeError` 是 `ValueError`
子类，执行器既有捕获仍转 `INVALID_PARAMS`）。

**方法教训（第三次，已升级为做法）**：同类缺陷第二次出现时，不应再修那一个实例，而应找到该类
问题的唯一收口点把保证一次性建立起来。本轮的 `canonical_hash` 与第 24 节的 `refuse_after_fetch`
是同一种手法，区别是本轮更早地做了这个判断。

### 验证

```text
make check → 1394 passed, 136 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 98 passed, 38 skipped
三条均先红后绿（还原实现后 13 个用例失败）
```


## 44. 机器人 code review 第三十二轮一条 thread 处置（2026-09-20）

P1「Bind all suspension generations into the query scope」——成立，已修（`d008cc6`）。**本任务近十轮
里最实质的一条，且不是前一轮修复的后果。**

`QueryScope`/`ControlSnapshot` 只带主体 `control_generation`，因此「全局或目标暂停被启用、随后
解除」这一序列无法被发现：布尔位归零、主体版本未变，一份暂停前的旧 scope 继续匹配并可发出新查询。

合同依据（已核原文）：C3 第 4 节 `:112`「任务领取、模型预算预留、网关发起请求及结果采纳均校验当前
**全局/目标**控制版本」；`:114`「解除暂停只移除该层阻挡……新尝试使用**当前所有控制版本**」，旧授权
不自动恢复。仓库 domain 层 `ScopeVersions`（subject/global/target）本就如此建模，是执行器这一侧
没带上。

处置：两个 DTO 各补 `global_suspension_generation` / `target_suspension_generation`（默认 0 以便
尚未接线的调用方构造，类型校验与既有字段同严）。**三处比较（预留、派发前、取回后）共用
`_generations_changed()` 一个谓词**——这是前面多轮「只改被点名那一处」教训的直接应用。

用例覆盖：解除后的全局/目标暂停使旧授权失效；版本在读取飞行途中变动时结果不被采纳但 history-only
保留；畸形版本号在两侧均被拒。

验证：`make check` → 1472 passed；PG 集成 103 passed；先红后绿。


## 45. 机器人 code review 第三十三轮三条 thread 处置（2026-09-21）

三条均采纳（`5037b78`）。**第 1 条直接推翻了第 44 节的取舍。**

### P1 两个新版本号必须必填

第 44 节我给它们设了默认 `0`，理由是「便于尚未接线的调用方构造」——把方便放在了 fail-closed
前面。一个没填这两个字段的 controller 适配器，在「全局/目标暂停后又解除」时与旧 scope 恰好相等
而被放行，**正是这两个字段存在的唯一理由所要挡住的那条路径**。

改为必填，与 `max_operations`/`max_tool_seconds`/`dispatch_id` 同一理由：不完整的接线必须
fail closed。27 个构造点（含 fixtures 与两个测试替身）一并更新；`FixedControl` 现可分别设置
全局/目标版本。

### P2 发放的工具上限随 Run 持久化

此前只持久化用量，上限每次由调用方传入：原本只授权 1 次操作的 Run，恢复后由带全局上限 20 的
执行器重建时即按 20 判定。新增 `tool_max_operations`/`tool_max_seconds` 两列，由 `accept()` 落库；
`charge_tool` 在行锁下取**发放值与调用方值的较小者**——发放值只收紧不放宽，`NULL` 表示未发放、
沿用调用方值。

如实记录：发放值由 `accept()` 调用方写入，而组合层接线尚未实现，因此本次落地的是**机制与强制**，
实际发放更窄上限需待接线任务；在此之前 `NULL` 路径保持既有行为。

### P2 host 需按 DNS 名或 IP 字面量校验

`_usable_authority()` 只检查非空，`https://.`、`http://-:443`、`https://_`、`https://a..b` 均注册
成功。改为：含冒号走 `ipaddress` 解析，其余按标签校验（1..63 字符、首尾非连字符、不允许空标签）。

### 验证

```text
make check → 1482 passed, 143 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 105 passed, 38 skipped
三条均先红后绿
```

**方法教训（第七次）**：为「让既有调用方不报错」而给安全字段设默认值，等于把接线缺口变成静默放行。
默认值的方向必须是 fail closed，否则宁可让调用方报错。


## 46. 机器人 code review 第三十四轮两条 thread 处置（2026-09-21）

一条修复，一条**确认成立但按依据记录为交接项**。

### P2 发放上限未覆盖续跑路径（已修）

第 45 节只给 `accept()` 加了发放上限，`new_run()` 没有：被取消的事故以新授权的更窄 scope 继续时
两列仍为 `NULL`，判定又回到调用方的更宽上限。`new_run()` 一并接收并落库。PG 用例直接走该路径
（取消 → 续跑发放 1 次上限 → 恢复执行器传 20 → 第二次操作被拒）。又一次「修实例漏整类」。

### P1 耐久事务未按全局/目标暂停代际设栅栏（成立，本 PR 不修）

**缺口真实**：执行器的三版本比较止步于其快照；`Lease` 只带事故代际，`charge_tool()`/`commit_tool()`
也只比较事故代际，暂停若落在最后快照与耐久事务之间不会被原子拦下。与 C3 第 4 节 `:112` 一致。

**不在本 PR 关闭的依据（已核实）**：

```text
persistence.py 中 suspension 相关列/表：0
persistence.py 中 "target" 出现次数：0
SuspensionState 在 domain 之外的使用：无（从未持久化）
```

存储层没有任何可比对的权威值——无全局暂停状态、无按目标暂停集合、无目标身份概念。把两个代际塞进
`Lease` 再与自己比较是形式动作。真正关闭需要持久化 `SuspensionState`（全局/目标代际 + 已解析目标
身份），并改 `control()`/`claim()`/`_lease_revoked()` 的语义——属 Controller 的暂停模型实现，跨模块，
按 AGENTS.md 应先设计并独立审查，不应在 Tool Gateway 纯逻辑半部 PR 内就地发明存储模型。

**已做**：在 `DurableToolLedger` docstring 写明该限制与关闭它的前提（写在别人会看的地方）；
PR thread 中说明依据并请用户裁决是否纳入本 PR；此处登记为交接项。

### 交接项（新增）

耐久层暂停模型：持久化 `opspilot.domain.control.SuspensionState`（全局代际、按目标代际、已解析目标
身份），`control()` 在全局/目标暂停时推进对应代际，`claim()` 快照三者，`_lease_revoked()` 比较三者，
并将其纳入 `charge_tool()`/`commit_tool()`/`publish()` 的栅栏。**在此之前，耐久写路径只按事故代际
设栅栏，这是已知且已记录的限制。**

### 验证

```text
make check → 1482 passed, 144 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 106 passed, 38 skipped
```

## 47. 终止逐轮修复循环：独立审查与拆分收尾（2026-09-21）

### 问题

第 1 至 46 节记录了约 44 轮机器人审查、100 个提交。用户核查后判定循环无终止条件：每次修复推送
触发新一轮审查，新发现落在上一轮修出来的代码上（第三十五轮 3 条 P2 中 2 条 blame 到第 14、17 轮的
修复提交），`executor.py` 由 727 行涨到 1484 行、`registry.py` 由 379 涨到 891。用户决定：停止逐轮
推送，派全新上下文的独立审查，按其结论拆分后**一次**推送。

### 第三十五轮 3 条 thread 处置（不再推代码）

- P2 复合 key 别名（`registry.py:160`）：**拒绝**。C3 §8 `:275` 规定参数名由运营方登记、注册期校验，
  不是模型对抗输入；代码已写明 denylist 不可能完备、边界由 `credential_ref` 持有；无越界复现。
  同类「再补一个别名/一种 URL 形态」的发现此后按本条处置。
- P2 重复 JSON key（`executor.py:1420`）：**拒绝**。`raw_sha256` 覆盖原始字节，投影是本代码库对同一
  字节串的确定性函数；「其它消费者可能保留第一个」是对系统外的推测。
- P2 `Window` 直接构造时 `OverflowError` 未转 `INVALID_WINDOW`（`outcomes.py:118`）：**采纳**，已复现，
  修于 `9714531`；属原始提交 `cfaf14ec` 引入。

### 独立审查（全新上下文，只读）结论与处置

| 决策点 | 审查结论 | 处置 |
|---|---|---|
| A 控制优先级 | 三元判定在 executor 内有 3 份实现，2 条退出路径无 control 复检：预扣费通用失败报 `CONTROL_UNAVAILABLE`、evidence commit 通用失败报 `EVIDENCE_NOT_COMMITTED`，暂停均未进入 outcome | `_reserve()` 与派发前复检改为调用 `_control_decision()`；两条退出路径套用同一优先级；新增 2 个测试钉住（`f79e01f`） |
| A 持久化暂停栅栏 | 全局/目标 suspension 代际未持久化，sink 无事务内栅栏时快照后的暂停仍会采纳 | 与第 46 节交接项一致，Controller 侧待办，本 PR 不闭合 |
| B persistence | 用量列、charges 表、原子预留/结算、lease 栅栏有 C3 §13/§7 依据；`tool_max_*` 列（产品代码零调用方）与 `DO $$` 同分支主键就地迁移无规范依据 | 移除 `tool_max_*` 列、参数与取 min 逻辑，移除同分支就地迁移语句，删 3 个只为其存在的 PG 用例（`d3f5a1c`）。第 45、46 节记录的「发放上限随 Run 持久化」修复由此撤回 |
| C endpoint 检测 | 三条正则加两个校验函数超出 §8 注册期规则的相称度；§8 威胁是运营方误写 | 只保留 `scheme://` 与 `//user@host` 两条有锚点的检测，删除 scheme-less 与协议相对其它分支；裸 `host:port`、`//host/path` 交人工审核；删 7 个、收窄 2 个测试（`3cd48fd`） |

审查顺带发现：无 P1。

### 验证

```text
make check → 1466 passed, 141 skipped, 2 xfailed
M0_B_POSTGRES=1 M1_DURABLE_POSTGRES=1 pytest tests/integration -q → 103 passed, 38 skipped
审查方复现脚本（S1 sink 通用失败 + 暂停、S2 预扣费通用失败 + 暂停）修复后均报 SUSPENDED
```

PG 集成用例首次运行抓到 `new_run()` INSERT 占位符数与参数数不符（本节移除列时引入），已修并入同一提交。

### 停机规则（本任务内生效）

CI 绿且独立审查完成后，修复推送引发的新一轮机器人发现批量处置：只有能引用 PRODUCT-CONSTRAINTS
或 C3 原文且有可复现测试的发现才采纳，其余按类回复拒绝并 resolve，不再逐轮推送。
