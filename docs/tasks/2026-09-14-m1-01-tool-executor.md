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
