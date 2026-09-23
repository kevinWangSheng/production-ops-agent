# UI 端到端 smoke 报告（PR #33 owner 视角，2026-09-17）

- 端到端工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-integration-full-ui`，分支 `integration/m1-01-full-ui`
  （`make setup` 完成；专属 PG `scripts.m0.postgres_lab`，端口 55431；web 服务 `python -m opspilot.web serve` 绑定 `127.0.0.1:18033`）。
- 修复落地：`feature/m1-01-progress-ui`（PR #33），普通 push，无 force。
- 临时工件（未提交）：`scripts/tmp_ui_worker.py`（轮询 queued 事故并调用 `Workbench.run_once`，fixture 替身 / 可选 live），
  `tmp/ui-smoke/`（env、web.log、worker*.jsonl、SSE 抓取、A/B/C/D/E/F/G 事故 id）。
- 服务与进程：web（pid 文件 `tmp/ui-smoke/web.pid`）已 kill 并确认 `WEB_STOPPED`；`pgrep -f tmp_ui_worker.py` 为空；
  两个 worktree 的 PG lab 均已由所属脚本 stop（数据保留）。

## 修复提交（PR #33 分支，均已 cherry-pick 到 `integration/m1-01-full-ui` 并在该分支复验）

| sha（#33） | 内容 | 集成分支 sha |
|---|---|---|
| `61f270f` | 吸收集成分支 `e42d7b7`：`tests/m1_web_support.py::_MemoryCommitter` 补 `begin_round`/`assert_current` | （原始 `e42d7b7`） |
| `f2aecb7` | `app.py` `openapi_url=None`（审计 P3：未认证可读 `/openapi.json`） | `8936ccb` |
| `65843df` | integ.md §5.2/§7(1)：`IncidentStore.control(..., payload)` 协议；`DurableIncidentStore` 按 `DurableStore.control` 签名检测并转发 payload；`Workbench._apply` 传 `{"text","channel":"web"}`；内存替身按 #31 语义镜像（含 keep_paused、`inputs`、`begin_round` 返回输入）。同一提交修复 redline.md P1-1：`run_once` 不再在 claim 前读笔记，笔记只在租约内读，#31 路径下不拼进 question。新测试：`test_a_note_applied_after_the_claim_fences_the_attempt_instead_of_being_ignored`、`test_openapi_schema_is_not_served`；PG 测试 `test_control_generations_and_late_results_on_postgres` 在 payload 支持的 base 上断言 `opspilot_inputs`、`opspilot_controls.payload` 与模型消息 `investigation_inputs` | `af1f33f` |
| `e958c5a` | smoke 发现：持有中的租约不再每次轮询写 `run_claim_refused` 事件；测试 `test_a_held_lease_is_a_quiet_refusal_not_an_event_per_poll` | `e22e8ad` |
| `9bbca15` | 任务记录追加集成 smoke 与接缝修复 | — |

验证：#33 分支 `make check` → `1467 passed, 97 skipped, 2 xfailed`；#33 base（无 #31）PG：web 2 passed、全部集成 59 passed；
集成分支（含 #31）`tests/test_m1_web_workbench.py` 23 passed、`M1_DURABLE_POSTGRES=1 tests/integration/test_m1_web_postgres.py` 2 passed
（payload 分支实际执行）。CI（`workflow_dispatch`）：run [35239844780](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35239844780)
对 `e958c5a` success；run [35240005464](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35240005464) 对 `9bbca15` success。
PR #33 `mergeStateStatus=CLEAN`、`mergeable=MERGEABLE`；PR 描述已追加「集成 smoke」小节。

## 7 条路径

| # | 路径 | 结果 | 证据 |
|---|---|---|---|
| 1 | web + worker 启动，专属 PG | PASS | `tmp/ui-smoke/web.log`（uvicorn 200/201/303 行）；worker 日志 `tmp/ui-smoke/worker1.jsonl` `worker_start`；PG 由 `postgres_lab start` 启动、smoke 后 stop |
| 2 | 认证提交 fixture 事故（目标 `checkout-prod`），未认证/错凭据拒绝 | PASS | 无凭据 `HTTP/1.1 401` + `www-authenticate: Basic realm="opspilot"` `{"code":"MISSING_CREDENTIALS"}`；错口令 `401 {"code":"INVALID_CREDENTIALS"}`；Bearer 打 UI 路由 401；Basic 打 `/intake/events` 401；已认证 `/openapi.json` 404。UI 表单 → `201 {"incident_id":"444459ec-…","run_id":"f2c7d4e1-…","sequence":1}`（A）；event_token → `201 {"incident_id":"f3a68ba5-…","delivery_key":"evt:3daef276…"}`（B） |
| 3 | SSE 进度与游标续接 | PASS | `cursor=0` 抓到 `id 1..6`：intake_accepted, run_claimed, step_committed, tool_committed, step_committed, run_completed；`Last-Event-ID: 3` 重连只得 `id: 4 5 6`（不丢不重）。DB `opspilot_subject_events` 同序列 |
| 4 | 证据 raw/view/hash 回读 | PASS | DB `opspilot_evidence` 行 `b73a5f42-…-t0` raw_sha256 `7110b8ba…` view_sha256 `994cc688…`；UI `/incidents/A/evidence/<id>` 返回同一对 hash，`hashes_verified: true`，本地对 `raw_utf8` 重算 sha256 相等；view content `[{"metric":"http_errors_rate","value":0.042}]` |
| 5 | 人工控制（#31 语义）：追问、纠正、取消；迟到结果拒绝；历史可见 | PASS | C：attempt 1 工具轮内 `follow_up`（gen 0→1，200）→ attempt 以 `CONTROL_DENIED` 结束，`opspilot_steps` 留 `late_result:step:g0:e1:round-1`；`opspilot_controls.payload` = `{"text":"Also compare…","channel":"web"}`，`opspilot_inputs` seq 1 同文本；再 attempt（epoch 不变、代际 1）模型消息含 `investigation_inputs[…text…]`，完成并发布，页面 `Facts (1)`、`history only`、决定表含 follow_up。D：attempt 内 `correct`（→1，late_result 历史）→ `cancel`（→2）→ 过期 `correct`(expected 1) → `409 CONTROL_CONFLICT current_generation 2`；页面 `incident-state cancelled`、`run-state cancelled`、correct/cancel 两行、纠正文本可见、`Last attempt: failed` |
| 6 | 重启恢复（#30 语义）：硬杀 worker，重启续接或 `blocked(INCOMPATIBLE_STATE)` | PASS（见备注） | E：`kill -9` 于工具轮内，页面 `run-state running / epoch 1`；立即重启的 worker 因租约（20 s）未到期得到 `LEASE_ACTIVE`，到期后 `run_claimed epoch 2`，步骤键 `g0:e2:round-1/2`，完成并发布，页面 `epoch 2 / concluded / Facts (1)`。F：worker `state=v2` claim → `INCOMPATIBLE_STATE`，`opspilot_runs.state=blocked`，页面 `run-state blocked` + `run_claim_refused` 事件；随后 v1 worker 不再 claim（run 保持 blocked/epoch 0） |
| 7 | PR #32 `IncidentOutcome` 外部投影 | PASS | `outcome_from_durable`：A → `final_state completed, evidence_ids ["b73a5f42-…-t0"], report_available true`，UI 同显 completed/concluded 且链接同一 evidence；D（action=cancel）→ `cancelled, actions [cancel, late_result_rejected], handoff_reasons [STALE_CONTROL_GENERATION], report_available false`，UI 同显 cancelled / no conclusion / history only |

备注（路径 6）：续接由本 PR 的 `run_once` 在租约到期后重新 claim 并从头再跑一次 loop（#31 的 `g{gen}:e{epoch}` 键使旧 epoch 的步骤只作历史），
**未走 PR #30 的 `Worker.resume()/RecoverySession`（已提交工具计划的续接）**；两者并存但 web 层未接 #30，见「交给其它 PR」。

## 真实 Run（1 次，机制全部跑通后）

- 事故 G `8b58f0ae-1aca-56db-b3f0-0ad3fffadf7f`，run_id `69fdca8c-466d-5459-9bf5-944cfd48c32c`，经 UI 表单提交、`UI_WORKER_MODE=live` 执行，
  密钥仅由 `scripts/m1_live_flash_loop.read_key` 从 `M0_ENV_FILE` 读取，未打印。
- 2 次 HTTP（`deepseek-flash`，1209 / 3227 tokens），**费用上界 0.024175 CNY**（≤ 0.2 CNY）。
- 结果 `failed / REPORT_INVALID`；证据 `75fc340c-…-t0` 已登记；页面显示「Unpublished handoff report」（Facts 2 / Hypotheses 1 / gaps 7），无结论、控制保持开放。
  与集成分支 `6cf407a`/`b5a3615` 已归因的 harness 时间策略问题（live 请求用的 `time_policies: [{"id": …}]` 无 mode/window）一致；本次未再重跑（额度 1 次），不作为正向能力证据。

## PR #33 已知未完成项在合并分支上的核实

| 项 | 结论 |
|---|---|
| 同一事务提交（C3 §6） | 仍存在：intake 账本、`accept()`、事件序列分别提交（账本先行，重试幂等） |
| 无租约续期 | 仍存在：#30/#31 均未增加续期 API；`run_once` 租约默认取 Run wall；smoke 用 20 s 租约 |
| 追问文本权威位置 | **已解决**（`65843df`）：`opspilot_controls.payload` + `opspilot_inputs` 实测落库并进入模型输入 |
| SSE 每次轮询新建连接 | 仍存在（`DurableEventLog.read_after` 每次开事务） |
| `rebuild()` `pending_tools` 读顶层 `tool_calls` | 仍存在（`opspilot/persistence.py:1132`，loop 提交在 `assistant` 下） |

## 交给其它 PR 的问题（本任务未改）

1. **#26/#29（预算结算）**：`opspilot_runs.budget_spent` 从未写入，完成的 Run 仍是 `budget_reserved=2, budget_spent=0`
   （`opspilot/persistence.py` 只在 `reserve_budget` 增加 reserved，无 settle/release 路径；复现：任一完成 Run 查 `opspilot_runs`）。
2. **#30 × #33（恢复路径）**：web 层 `run_once` 不使用 `Worker.resume()/RecoverySession`，硬杀后的续接是重新 claim 并从头跑；
   已提交但未执行完的工具计划不会被 `execute_pending` 接续。需要决定 worker 组合层归谁（#32 的薄组合层或后续任务）。
3. **#31（暂停语义）**：带内容的 `follow_up`/`correct` 在 paused 下被接受并记录输入、事故保持 paused（`keep_paused`），本 PR 的替身已镜像；
   请 #31 owner 确认这是有意的产品语义（C3 §4「追问与纠正不得静默解除人工暂停」的解释）。
4. **#31（`pending_tools`）**：`persistence.py:1132` 仍读顶层 `tool_calls`，loop 提交在 `response["assistant"]["tool_calls"]`，`rebuild()["pending_tools"]` 恒空。
5. **集成分支 `47c4860`**（`ScriptedInvestigator` 改用 `historical_window_context`）依赖 #32 的 helper，不在 #33 base，未吸收；合并顺序按 integ.md §8。
6. **本 PR 自身记录（非缺陷）**：对 web 之外创建的事故（无 `intake:` 账本行）`run_once` 抛 `INCONSISTENT_STATE`，轮询型 worker 需按 `intake_key` 前缀过滤（临时脚本已如此）。

## 需要用户决定的事项

- 追问在 paused 下的接受语义（上表第 3 项）以及 worker 组合层归属（第 2 项），影响 #31/#30/#33 的合并顺序与后续任务拆分。
