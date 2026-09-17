# 跨 PR 红线审计：`integration/m1-01-full` @ `e42d7b7`

- 审计对象：`integration/m1-01-full`，commit `e42d7b7`（merge-base 与 `main` 为 `b483a12`）。
- 方法：只读。所有代码引用取自 `git show e42d7b7:<path>`；动态验证只在 worktree
  `/Users/shenghuikevin/dev/AI/production-ops-agent-integration-full`（HEAD 同为 `e42d7b7`）的 `.venv` 中用内存 double 运行，
  无 PostgreSQL、无外网、无付费调用。PG 集成路径（`M1_DURABLE_POSTGRES=1`）未运行，涉及 `persistence.py` 的结论标注为静态分析。
- 权威依据已读：`PRODUCT-CONSTRAINTS.md` 全文；`SPEC.md` 第 6 行门槛段；C3 第 4、5、6、7、8、9、12、13 节；ADR-0003；AGENTS.md「Code Review Rules」。
- 动态验证脚本与产物：
  - `scratchpad/replay_real_run.py`（真实 Run 报告重放）
  - `scratchpad/web_checks.py`（未认证面 + 人工纠正与 claim 的时序）
  - `pytest tests/test_m1_web_workbench.py tests/acceptance tests/test_worker_recovery.py tests/test_architecture.py` → `45 passed, 2 xfailed`

## 总表

| # | 条目 | 结论 | 一句话 |
|---|---|---|---|
| 1 | 只读边界 | PASS | 无写操作/任意 shell/SQL/凭据导出/自审批路径；产品包不 import `scripts`/`tests`。仅 P3：`/openapi.json` 未认证可读（元数据）。 |
| 2 | 人工控制优先级 | **FAIL** | P1：`run_once` 在 claim 之前读取人工备注，纠正落在两者之间时，新代际的 attempt 在没看到纠正的情况下发布结论（内存 double 已复现）。另有两条 P2：#31/#33 两套人工文本通道并存；`claim()` 版本不符时把 paused/cancelled/completed 的 run 行改成 `blocked`。 |
| 3 | 业务记录恢复权威 | PASS | 恢复只读 PG（`rebuild()`），无从内存/trace/模型输出/web 会话反写业务状态的路径；`INCOMPATIBLE_STATE` fail-closed。但 2c 的「blocked 覆盖人工终态」同样触碰本条，见 P2-2。 |
| 4 | 秘密与数据出口 | PASS | 密钥只在 `DeepSeekClient` 构造参数与 `scripts/` 读取；web 只读哈希；错误只回固定码；无 LangSmith 出站。P3：输入键名过滤只到顶层；数据源自带秘密无注册期拦截（已在 docstring 声明）。 |
| 5 | 认证边界 | PASS | 所有业务路由先认证后解析；缺失/错误凭据 401 固定码；跨通道 401；SSE/证据/幂等账本均在认证后。P3：`/openapi.json` 未认证 200。 |
| 6 | 预算与 deadline | **FAIL** | 模型 HTTP 次数以 PG `reserve_budget` 为唯一权威（重试也预留）PASS；但工具次数/工具秒数只存在 `ReadOnlyToolExecutor` 内存计数，重启/新 attempt 归零，冻结的 20 次/240 s 上限是「每 attempt」而非「每 Run」（P2）。 |
| 7 | 验收与证据真实性 | **FAIL** | `feature_list.json` 无改动、三份报告 sha256 与账本一致 PASS；但两次付费真实 Run 的 `REPORT_INVALID` 是 live 脚本 `evidence_context` 缺 `mode` 导致（重放证明 report-2 在正确上下文下 `completed`），证据与任务记录未指出原因；验收测试硬断言 `handoff`，真实 Run 成功反而会让套件失败，而表格打印为「F3 PASS」（P2）。 |

## 逐条展开

### 1. 只读边界 — PASS

**核查事实**

- 工具执行器唯一出站口是 `ReadOnlyTransport.fetch`，`TransportRequest.__post_init__` 强制 `read_only is True` 且 `verb in READ_ONLY_VERBS`（`opspilot/tools/executor.py:236-239`）；`FORBIDDEN_VERBS` 含 exec/apply/delete/sql/shell 等（`opspilot/tools/registry.py:62-83`）；`RESERVED_PARAMETERS` 禁止模型选 endpoint/target/凭据（`registry.py:88-104`）；`_accept_params` 对未声明参数返回 `PARAM_NOT_ALLOWED`（`executor.py:661-673`）。
- Web 端点只有：页面读、`/intake/ui`、`/intake/events`、`/incidents/{id}/control`（人工动作，经 `DurableStore.control()`）、SSE、证据回读（`opspilot/web/app.py:229-395`）。写入的都是 Agent 自身事故状态，属 PRODUCT-CONSTRAINTS 允许的「persist its own incident state」。
- 模型无法到达 `control()`/`publish()`：`publish` 需 lease 与最终步骤一致（`persistence.py:1021-1076`）；`control` 只有认证 UI 调用。
- `DeepSeekClient` 只允许 `https://`、禁重定向、空代理（`opspilot/investigation/client.py:47-50,33-35`）。
- `tests/test_architecture.py:74-84` 断言 `opspilot/` 不 import `scripts`；grep 确认产品包也无 `import tests`。`scripts/m1_live_flash_loop.py` 反向 import `tests.m1_tool_support`（脚本依赖测试 double），不构成扩权。
- `scripts/m1_acceptance.py` 只 subprocess 调 pytest，不被产品 import。

**P3-1（#33）** `FastAPI(title=..., docs_url=None, redoc_url=None)`（`app.py:89`）未关 `openapi_url`。动态验证：`GET /openapi.json` 无凭据返回 200 及全部路由/参数 schema；`/docs`、`/redoc` 404；`/` 与 SSE 401。泄露的是路由结构而非数据。修法：加 `openapi_url=None`。

**P3-2（#33）** `python -m opspilot.web serve` 启动即 `install()` 建表（`opspilot/web/__main__.py:63-70`），docstring 已声明非部署工件。保持声明即可，不应进入生产入口。

**P3-3（#20）** 执行器 docstring 明示不对数据源返回的秘密做脱敏，「这类源不得注册」（`executor.py:21-24`），但注册期没有任何字段表达/校验该约束。属已知未建，记录不评分。

### 2. 人工控制优先级 — FAIL

#### P1-1：`run_once` 先读人工备注、后 claim，纠正落在中间即被新代际的 attempt 忽略（归属：#33 × #31）

**证据**

- `opspilot/web/service.py:529-537`：`notes = self._notes(incident_id)`（533）在 `lease = self.incidents.claim(...)`（536）之前；`_attempt` 内不再重读备注，问题文本由 `_compose_question(request.question, notes)` 组装（583、666-676）。
- 人工 `correct`/`follow_up` 经 `DurableStore.control()` 把代际 +1、run 置回 `queued`（`persistence.py:851-887`），随后的 `claim()` 以事故代际发租约（`persistence.py:604-617`）。因此备注读取早于 control、claim 晚于 control 时，attempt 持有**新代际**租约却携带**旧备注**。
- PR #31 的 `begin_round` 输入水位机制本可在租约内冻结输入（`persistence.py:962-1008`），但 web 路径的人工文本不写 `opspilot_inputs`（见 P2-1），该机制对本通道无效。

**可复现场景**（`scratchpad/web_checks.py`，内存 double，输出原样）

```
run_once execution: completed handoff: False reasons: ()
published report present: True | run state: completed | run gen: 1 | incident gen: 1
investigator question contained the correction: False
lease generation the attempt ran under: [1]
controls shown on page: [('correct', 1)]
event kinds: ['intake_accepted', 'control_applied', 'run_claimed', 'step_committed', 'tool_committed', 'step_committed', 'run_completed']
```

操作者在 `_notes()` 之后、`claim()` 之前提交 `correct`（代际 0→1）；attempt 以代际 1 领取、发布结论；页面同时显示「correct @ generation 1」和已发布报告，操作者会认为报告已纳入纠正，实际没有。`control()` 此后因 `conclusion IS NOT NULL` 拒绝一切非 cancel 动作（`persistence.py:827-831`），纠正只能经 cancel + new_run 重来。

**严重级** P1。违反 PRODUCT-CONSTRAINTS「late completion must not ... erase a newer human decision」与 C3 §4「每轮调查固定实际处理的输入水位…不能把尚未分析的输入显示为已处理」。窗口虽是两次 DB 调用之间，但无任何栅栏、无任何痕迹。

**修法** 在 `claim()` 成功后（租约内）再读备注，并把备注纳入 `begin_round` 冻结的输入（即 P2-1 的迁移）；或在 `_attempt` 内比较 claim 前后的代际/备注集合，不一致即 abandon 并重试。

#### P2-1：人工文本两套通道并存，web 路径不写 PR #31 的权威列（归属：#33 × #31）

**证据**

- PR #31 给 `DurableStore.control()` 加了 `payload`，写入 `opspilot_controls.payload` 与 `opspilot_inputs`（`persistence.py:794, 888-911`），并由 `begin_round` 按水位回读（962-1008）。
- PR #33 的 `DurableIncidentStore.control` 转发时不传 `payload`（`opspilot/web/store.py:265`），文本只落 `opspilot_web_ledger` 的 `control_intent`/`control` 两个 namespace（`service.py:280, 324-328`），由 `_notes()` 拼进问题（406-422）。
- 产品包内 `append_input`/`read_inputs`/`control(payload=...)` 无任何调用方（grep 结果仅定义处 `persistence.py:914, 1010`）。
- 任务记录自认这是 #31 合并后的待办：`docs/tasks/2026-09-16-m1-01-progress-ui.md:153,158-161,178-179`「#31 合并后应把追问/纠正文本改为经 `payload` 落入 `opspilot_controls`」。集成提交 `e42d7b7` 说明「No production code changed」，即该缝合未做。

**场景** 同 P1-1 的根因；另：`_confirm_from_audit`（`service.py:361-394`）只按 action/expected_generation/actor 匹配审计行，两次同操作者、不同文本的崩溃重试可互相确认（代码注释 369-371 自述「best effort until the audit carries text/key (PR #31)」，但 #31 已在分支上，仍未接）。ADR-0003 要求 PG 业务记录为唯一恢复权威，此处人工纠正文本的权威落在 web 账本。

**严重级** P2。**修法** `DurableIncidentStore.control` 传 `payload={"text": text}`；`_notes` 改读 `opspilot_controls.payload`/`opspilot_inputs`；`run_once` 不再拼问题，改由 loop 经 `begin_round` 注入。

#### P2-2：`claim()` 版本不符时把不可领取状态的 run 行改成 `blocked`（归属：#26/#31 × #30/#33）

**证据**（静态，未跑 PG）

- `persistence.py:587-596`：`if row["versions"] != versions: UPDATE opspilot_runs SET state='blocked'`（无状态守卫）**先于** `elif incident_state in {"completed","cancelled","paused"}` 与 `elif run_state not in ("queued","running")` 两个拒绝分支。
- `Workbench.run_once` 对 `current_run_id` 无条件 claim（`service.py:526-537`），不像 `Worker.resume` 先检查 `plan.candidate`（`opspilot/worker.py:62-64`）。
- `control()` 对非 cancel 动作只放行 `_CONTROL_OPEN_RUN_STATES`（`persistence.py:36, 844-845`），`blocked` 不在其中；`test_non_cancel_control_is_refused_from_every_unlisted_run_state`（PG 测试 1201 行）把这一点固定下来。

**场景** 人工 `pause`（run 行 `paused`）→ 部署新版本使 `run_versions` 变化 → 任一 `run_once` 触发 claim → run 行变 `blocked`、抛 `INCOMPATIBLE_STATE`，事故仍 `paused` → 人工 `resume` 得 409 `ILLEGAL_TRANSITION`，只能 cancel + new_run。对 `cancelled` run 同样被改成 `blocked`，`outcome_from_durable` 会把人工取消投影为 `blocked/INCOMPATIBLE_STATE`（`opspilot/acceptance.py:104-105`）。C3 §5 明确要求版本事故与权限操作两套语义不得互相顶替。

**严重级** P2。**修法** 把 `versions` 判定移到状态判定之后，或 `UPDATE ... SET state='blocked' WHERE state IN ('queued','running')`。

#### 已核查为 PASS 的路径

- loop：每次工具执行前 `store.assert_current()`（`loop.py` `_run_tools`），每次模型调用前 `reserve_budget` + `assert_current`（`_call_model`）；`DurableStepStore` 两者都走 `lease_current`，`_lease_revoked` 以事故代际、租约、deadline、全局/目标 suspension 为准（`persistence.py:82-108`）。
- worker/recovery：`RecoverySession._assert_current` 校验 lease + `rebuild()` 的代际与 run_id（`worker.py:19-28`）；`commit_tool` 对旧代际步骤只记 late_result（`persistence.py:765-778`）。
- 迟到发布：`publish` 在租约失效/状态不符时只写 `late_result:publish:*`（1036-1055）；web 测试 `test_control_generations_and_late_results_on_postgres` 覆盖 pause 中途。
- 执行器自身的 `ControlAuthority` 在产品里没有 PG 实现（web 测试用 `FixedControl`，`tests/m1_web_support.py:512-516`），但由于 loop 在每次工具调用前经 store 栅栏，且 `commit_tool` 拒绝旧代际，功能上人工控制仍胜过工具结果。记为 P3 缺口（#20 × #33：产品组合缺 `DurableControlAuthority`）。

### 3. 业务记录恢复权威（ADR-0003）— PASS

- `opspilot/recovery.py:57-58` `recover()` 只调 `store.rebuild()`；`rebuild()` 在单一 REPEATABLE READ 快照内读 incidents/runs/steps/inputs/rounds（`persistence.py:1078-1149`），`pending_tools` 按事故代际过滤且排除 `late_result`。
- Web `snapshot()` 的 `handoff_report` 取自 `rebuilt["steps"]` 的最后活步骤（`service.py:459-461, 706-711`），仅展示；`_confirm_from_audit` 方向是 PG 审计 → web 账本，不反向。
- `submit` 重放路径重复调用 `accept()`，`accept` 对已存在身份直接返回（`persistence.py:254-265`）。
- `INCOMPATIBLE_STATE`：`claim()` 置 `blocked` 并抛出（587-592, 618-619）；`control()` 对 blocked 只接受 cancel；`new_run` 需事故 `cancelled`（500-501）。fail-closed 成立。
- 例外：P2-2 的「版本不符把人工终态/暂停态改成 blocked」同时触碰本条（版本事故覆盖人工决定的记录），不重复计分。

### 4. 秘密与数据出口 — PASS

- 模型密钥：产品仅 `DeepSeekClient(api_key)` 构造参数并放在 Authorization 头（`client.py:40-49, 70-76`）；产品包无 `os.environ`/`.env` 读取密钥的路径。`.env` 只在 `scripts/m1_live_flash_loop.py:106-124` 经 `M0_ENV_FILE` 读取，`del key` 后不再持有。
- Web 配置只读哈希：`AuthConfig.__post_init__` 拒绝非 pbkdf2 口令与非 sha256 token（`auth.py:82-89`）；`_authorization` 不回显凭据；错误只回固定码。
- `DomainError`/`ValidationError` 渲染经 `sanitized_errors`，架构测试禁止裸 `.errors()/.json()`（`tests/test_architecture.py:150-182`）。
- 供应商错误正文丢弃：`client.py:_post` 对 `HTTPError` 只读后丢弃；执行器把任何传输异常压成固定码（`executor.py:461-469`）。
- LangSmith：产品包无引用。
- SSE/事件：`intake_accepted` 载荷含操作者 `question` 全文与 actor_id（`service.py:221-233`），只对认证 UI 用户可见，属产品功能。
- 证据回读：`/incidents/{id}/evidence/{eid}` 返回数据源原始字节（`app.py:365-395`），限认证用户且按 `run_ids` 归属校验（`service.py:495-503`）。

**P3-4（#29 × #31）** loop 对 `opspilot_inputs` 内容的键名过滤只看顶层键 `password/secret/token/authorization`（`loop.py` `_round`），嵌套值不过滤。当前无产品调用方写 inputs（见 P2-1），暂无暴露面；一旦接上 `append_input`，需改为按字段白名单投影。

### 5. 认证边界 — PASS

- 每个业务 handler 第一行 `await ui(request)` / `ui_mutation` / `event_channel`（`app.py:229-395`），路径解析在其后；`ui_mutation` 再校 Origin/Sec-Fetch-Site（`auth.py:153-167`，两者皆无视为外域）。
- 缺失凭据 401 `MISSING_CREDENTIALS`（UI 路由附 `WWW-Authenticate`），错误凭据 401 `INVALID_CREDENTIALS`，未知用户走 decoy hash 等时（`auth.py:106-121`）。Bearer 打 UI 路由、Basic 打 `/intake/events` 均因 scheme 不符 401。
- SSE：认证 → `find_incident` 404 → 流（`app.py:328-347`），流本身不持有租约/事务。
- 幂等账本：`intake:` 键全局唯一，不同 principal 同键 → 409 `INTAKE_KEY_CONFLICT`；`control` 键以 `incident_id:` 前缀（`service.py:187-205, 269`）。
- 动态验证：无凭据 `GET /` 与 SSE 均 401；`/openapi.json` 200（P3-1）。

### 6. 预算与 deadline — FAIL

**PASS 部分：模型 HTTP 次数**

- 唯一权威是 PG：`reserve_budget` 以 `(run_id, reservation_id)` 幂等、`reserved+spent+unknown+amount > limit` 拒绝（`persistence.py:641-664`）；loop 每次物理请求（含重试 `#a2`）先 `reserve_budget`（`loop.py` `_call_model`），`DurableStepStore.begin_round` 的键含代际与 epoch，故重启后的新 attempt 用新键继续累加同一 `budget_limit`（`store.py:103-113`）。`Workbench` 以 `MAX_MODEL_REQUESTS_PER_RUN=4` 建 Run（`service.py:179, 209-216`）。预留从不结算为 spent，属保守。
- Deadline：`accept`/`new_run` 落 PG，`claim` 与 `_lease_revoked` 同时校验；loop 另有每 attempt 的 `RUN_WALL_SECONDS` 与请求超时 `min(360, 剩余 deadline, 剩余 wall)`。
- `new_run` 必须先 cancel，且新 Run 新预算属 C3 §4/§13 允许的「新 Run 接续」，不是绕过。

**P2-3：工具次数/秒数上限不持久（归属：#20 × #30 × #33）**

- `ReadOnlyToolExecutor` 的 `_operations_used`/`_tool_seconds_used` 是实例字段（`executor.py:299-300, 401, 415, 457, 473`）；产品包内无任何持久化或跨实例读取（grep 仅命中 executor.py）。`commit_tool` 只存结果，不计数。
- 每次 `investigate` 都新建执行器（测试组合 `tests/m1_web_support.py:512-516`；产品无其它组合）。

**场景** Run 第 1 个 attempt 用掉 18 次工具调用后 worker 崩溃；lease 过期后 `run_once`/`Worker.resume` 以新 epoch 重新领取同一 Run，新执行器从 0 计，同一 Run 可再发 20 次（累计 38 次、最多 480 s 工具 wall），超出 v4 冻结的每 Run 上限。C3 §13 明确「重启不能重置预算」。

**无法判定** 产品当前没有把执行器接进 `Workbench.run_once`/`Worker` 的组合代码（web `__main__` 不跑 investigator），因此生产路径的实际行为无法从代码证实；上述是按现有接口的必然结果。

**修法** 把工具次数/秒数作为 PG `budget_reservations` 的第二类预留（或 `opspilot_runs` 新列）由 `commit_tool`/新的 `reserve_tool` 结算，执行器启动时从 `rebuild()` 读取已用量初始化。

### 7. 验收与证据真实性 — FAIL

**PASS 部分**

- `git diff main...e42d7b7 -- feature_list.json` 为空；PRD/SPEC/PRODUCT-CONSTRAINTS 无改动，只有 ROADMAP 增 3 段。
- 哈希抽查（对 `git show e42d7b7:` 内容计算）：
  - `docs/evidence/m1-01-acceptance/real-run-report.json` sha256 = `98d97624…0f632` = `real-run-ledger.json.report_content_sha256` ✓
  - `real-run-report-2.json` sha256 = `8f01335a…3a765` = `real-run-ledger-2.json.report_content_sha256` ✓
  - `m1-01-investigation-loop/report.json` sha256 = `d219960b…f6f9b` = `ledger.json` ✓
  - `acceptance-output.txt` 声称的 HEAD `3f88b39` 是 `e42d7b7` 的祖先 ✓；PDT/UTC 日期一致。

**P2-4：两次付费真实 Run 的 `REPORT_INVALID` 是验收脚本造成的，证据未说明；验收测试把 handoff 固定为通过条件（归属：#32 × #29）**

- `scripts/m1_live_flash_loop.py:186-189` 的 `evidence_context` 只有 `{"id": "policy-window-1"}`，无 `mode`/`window`。`opspilot/investigation/reports.py:246-247` `eligible_time_policies` 对无 `mode` 的策略直接 `continue`，因此任何 fact 类 claim 的 `time_scope_ref` 都不在 view 的 `time_scope_refs` 内 → `unsupported_citations` 为真 → `REPORT_INVALID`（`reports.py:344`）。测试 double 的上下文带 `mode: historical_window`（`tests/m1_investigation_support.py:160-171`），两者不一致。
- 动态重放（`scratchpad/replay_real_run.py`，把 `real-run-report-2.json` 的 evidence_id 替换为重放 Run 的 id 后经产品 loop）：

```
report-1 parse_report alone: (None, 'REPORT_INVALID')      # 模型正文末尾多了一行 {"type":"json_object"}，确为模型侧失败
report-2 parse_report alone: OK
A script ctx (no mode): execution=failed handoff=True reasons=('REPORT_INVALID',) report=no
B test ctx (historical_window): execution=completed handoff=False reasons=() report=yes
```

  即 Run `4758c14f…`（0.026677 CNY）产出了一份 `completed/partial`、引用正确的 v2 报告，被脚本的上下文判成无效。任务记录写「两次均为 REPORT_INVALID handoff…没有可用报告」（`docs/tasks/2026-09-16-m1-01-acceptance.md` 验证结果、补充执行节），未指出原因，读者会归因到模型。09-16 的 Run 在早期 commit 下通过，说明是 PR #29 后续收紧 `eligible_time_policies` 时脚本未同步。
- `tests/acceptance/test_m1_01_acceptance.py:149-164` 读取 `real-run-ledger-2.json` 并**硬断言** `human_interaction == "handoff"`、`report_available is False`；`scripts/m1_acceptance.py:57-62` 把该场景列为 `F3`，`acceptance-output.txt` 打印 `real-deepseek-record | F3 | ... | PASS`。语义反了：若换成一次成功的真实 Run，验收套件会失败。任务记录完成条件写「不把 handoff 当正向能力通过」，文字上做到了（列标注 historical, handoff），但断言方向与 PASS 表格合起来正是「handoff 当通过」。

**严重级** P2（不改产品红线，但影响证据可信度与用户对付费 Run 结果的判断）。

**修法** ①脚本 `evidence_context` 与 `ScriptedInvestigator` 共用同一构造函数；②在两份 ledger 旁补一条说明文件记录根因，或在任务记录「验证结果」中改写归因；③验收测试改为断言「投影字段与账本一致」而非断言 handoff，或把该场景从 F3 行移到「证据链路」行并标 `N/A`。

## 其它观察（不计分）

- `_EmittingCommitter` 注释「on this branch the loop never calls them」（`service.py:124-126`）已过期：合并 #31 后 loop 每轮都调 `begin_round`/`assert_current`。
- `Workbench.submit` 的 `deadline = now + run_seconds`（1800 s）从**接收**起算，无 worker 时 Run 会在 30 分钟后因 `DEADLINE_EXCEEDED` 永不可领取；不是红线，但与「Run 绝对期限 24 小时」的候选值差距应在 SPEC/合同层明确。
- `Worker.resume` 与 `Workbench.run_once` 是两条并行的 attempt 入口（#30 与 #33），前者有 `plan.candidate` 守卫、后者没有；建议收敛为一个。
