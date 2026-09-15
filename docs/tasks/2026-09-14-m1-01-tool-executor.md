# M1-01 只读工具执行器（纯逻辑部分）

- 状态：进行中
- 更新日期：2026-09-14
- 依据：[M1-01 拆分](../evidence/m0-real-investigation/m0-exit-matrix.md#m1-01-任务拆分与投入估算待-gate-决定)
  「只读工具执行器」子任务；[C3 技术方案](../design/technical-proposal-2026-09-07.md)
  第 3 节（Tool Gateway 角色）、第 8 节（工具注册合同）、第 4/7 节（控制版本、
  操作身份与失效结果处理）；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)
  的 Evidence and context requirements、Runtime and human control requirements、
  Data flow contract；[SPEC](../../SPEC.md) 的有界开放 M1-01 门槛；
  冻结上限见 [v4 验收包](../testing/first-investigation-v4-2026-09-10.md)。
  相关验收条目：F3（证据可区分性）、F7（只读安全与审计），二者 `passes` 保持 false。
- 工作区：分支 `feature/m1-01-tool-executor`，
  worktree `/Users/shenghuikevin/dev/AI/production-ops-agent/.claude/worktrees/agent-addafc240c953e663`

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

- 已完成本地实现、自测与 `make check`；按任务要求**未推送、未建 PR、未合并**。
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
  待最新基线兼容修复后重跑。PR #20 的 CI/Review 仍以最新提交为准。
