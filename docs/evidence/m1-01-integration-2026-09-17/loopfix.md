# loopfix 报告：M1-01 真实 Run 的 REPORT_INVALID 诊断与修复

- 集成分支：`integration/m1-01-full`，起点 `e42d7b7`，最终 HEAD `2189e73`（已推送 `origin/integration/m1-01-full`）
- 归属 PR：**#32**（`feature/m1-01-acceptance`），最终 HEAD `9626a9e`（已推送；`4e6cb84` 为最后一个代码/证据提交，`9626a9e` 仅任务记录追加 CI 结果）
- 结论一句话：两次 `REPORT_INVALID` 是 PR #32 验收脚本传给 loop 的时间策略缺 `mode`/`window` 造成的（分类 (e) harness 上下文缺陷），loop/校验器/提示词均无缺陷；修复后真实 Run 2/2 `execution=completed`，其中 1 次无 handoff、报告经 `IncidentOutcome` 入口投影为 `report_available`。

## 1. 根因分类与证据

**Run `4758c14f`（report-2）— 分类 (e)：验收 harness 的 evidence_context 缺陷**

- 修复前 `scripts/m1_live_flash_loop.py:186-189`（`git show e42d7b7:scripts/m1_live_flash_loop.py`）：`evidence_context={"type":"opspilot-evidence-context-v4","time_policies":[{"id":"policy-window-1"}]}`，无 `mode`/`window`。
- `opspilot/investigation/reports.py:246-247` `eligible_time_policies`：`mode not in {"historical_window","current"}` → `continue`，因此 `loop.py:443-452` 给工具 view 的 `time_scope_refs` 为空集。
- `reports.py:288-301` `context_time_policy_ids` 只取 `id`，所以 `policy-window-1` 通过了第一道 `claim.time_scope_ref in policies` 检查，但在 `reports.py:344`（cited view 未 wear 该 policy）被 fail-closed 拒绝；`loop.py:366` 记 `REPORT_INVALID`。
- 模型行为正确：L2 合同（`reports.py:65-70`，`prompt_revision=prompt-replay-candidate-017c81744c26`）要求「a time_scope_ref from that context」，上下文里唯一的 id 就是 `policy-window-1`。
- 离线重放（`scratchpad/replay.py`，把报告里的 evidence_id 换成重放 Run 实际交付的 id）：
  - `parse_report(real-run-report-2.json)` → OK（schema `m0-report-v2`，4 条 fact 类 claim 全部在 `reports.py:344` 失败）
  - 脚本上下文 → `execution=failed, reasons=('REPORT_INVALID',)`，入口 `decision=handoff`
  - `historical_window` 上下文 → `execution=completed, handoff=False`，入口 `decision=report_available`
- 排除项：非 (a) 提示词与校验器一致（都用 `m0-report-v2`）；非 (b) 校验器按设计 fail-closed，loop 自身 happy-path 测试用的就是完整策略（`tests/m1_investigation_support.py`）；非 (c) 输出绑定按 v4 语义工作；非 (d) 报告 2 完全合规。v4 schema `$defs/TimePolicy.required` 含 `mode`（`IncidentScenario.v4.schema.json`）。

**Run `31c72e6f`（report-1）— 叠加 (d)：模型侧输出问题**

- 文件末尾 `...]}\n{"type":"json_object"}`（`od -c` 证实），`json.loads` 报 `Extra data (char 5898)` → `reports.py:165` `REPORT_INVALID`。合同要求「只返回一个 json object」，拒绝正确，未放宽解析器。
- 去掉尾巴并用完整策略重放：`completed` + `INCOMPLETE_INVESTIGATION`（模型自判 incomplete/inconclusive），`report_available=true`。即在旧上下文下它同样会死在 (e)。

调度者的原猜测（prompt 与校验器格式不一致 / 校验器缺陷）**被推翻**；另一全新上下文审计（redline P2-4）与本次独立得出的结论一致，已自行复核而非采信。

## 2. 修复 diff 概要（`git diff e42d7b7..47c4860 --stat -- opspilot/` 为空；未改校验器、L2 文本、冻结哈希、验收包、`feature_list.json`）

| 集成分支 | PR #32 分支 | 内容 |
|---|---|---|
| `f08b5b8` | `7a0d47a` | 脚本改用 `historical_window` 策略（窗口 = 授权查询窗口 2026-09-14T00:00–01:00Z），抽出 `build_run()`/`EVIDENCE_CONTEXT`/`QUESTION`；新增 `tests/acceptance/test_m1_live_flash_replay.py` |
| `fbd920b` | `996e879` | `tests/m1_tool_support.py::historical_window_context()`；脚本与 `assemble()` 共用 |
| `78f4256` | `4da77f1` | `test_m1_01_acceptance.py` 真实 Run 场景：参数化所有账本，断言投影与账本逐项一致；新增一行要求存在一次报告通过绑定且无 handoff 的真实 Run（`bbf10e0e`）；`scripts/m1_acceptance.py` 表格同步（原来硬断言 handoff 并印成 F3 PASS，语义反了） |
| `6cf407a` | `f280e53` | `live-runs/<run_id>/`（ledger/report/report-parsed/acceptance-outcome）、`real-run-notes.md`、刷新 `acceptance-output.txt` |
| `47c4860` | —（仅集成） | PR #33 的 `ScriptedInvestigator` 改用同一构造；#33 分支尚无该 helper，须待 #32 合并后由 #33 吸收 |
| `b5a3615` | `295bd0a` | 任务记录归因更正 + 复验记录 |
| `2189e73` | `4e6cb84` | 验收表恢复来源头（审查 F9） |

选择 `historical_window` 而非 `current`：fixture `data_as_of` 比取数时刻旧约 3.6 天，`current` 需一个不诚实的 `max_source_age_seconds`。只补 `mode`+`window`（与 loop 测试替身一致），未补 v4 `TimePolicy` 其余必填字段（见待裁定项 2）。

## 3. 测试

- 新增 `tests/acceptance/test_m1_live_flash_replay.py` 4 例：报告 2 新上下文接受 / 旧上下文拒绝；`EVIDENCE_CONTEXT` 是可绑定的 historical_window；报告 1 双 JSON 对象仍拒绝、去尾后为合法 incomplete 报告。
- `tests/acceptance/test_m1_01_acceptance.py`：参数化 4 份账本（2 旧 + 2 新）+ 1 例正向。
- 集成分支 `make check`：`1483 passed, 102 skipped, 2 xfailed`；PR #32 分支 `make check`：`1463 passed, 95 skipped, 2 xfailed`；`make acceptance` 10/10 PASS + `SECRET_SCAN_PASSED`（两分支）。
- 独立审查复跑 `tests/acceptance tests/test_m1_investigation_loop.py tests/test_m1_web_workbench.py`：77 passed。

## 4. 有界真实 Run（`M0_ENV_FILE` 指向私有 env，密钥仅脚本读取、未打印未落盘；`prompt_revision` 与旧 Run 相同）

| # | run_id | HTTP | execution | handoff_reasons | 报告 | 入口投影 | 费用上界 CNY | 证据 |
|---|---|---|---|---|---|---|---|---|
| 1 | `40b9705a-0ef7-4fe9-a50c-1a9f04e103c3` | 2 | completed | `INCOMPLETE_INVESTIGATION` | v2，模型自判 incomplete | handoff，`report_available=true` | 0.025918 | `docs/evidence/m1-01-acceptance/live-runs/40b9705a-…/` |
| 2 | `bbf10e0e-0b0e-489c-9efd-98b80ef4aa3b` | 2 | completed | 无 | v2，completed/partial，6 claims | `decision=report_available`，无 handoff | 0.02658 | `…/live-runs/bbf10e0e-…/` |

累计 2 次 / 0.052498 CNY（上限 3 次 / 0.5 CNY），达标后停止。脚本用 `MemoryStepStore`，不依赖 PG，**未启动** lab PG。收尾时发现 127.0.0.1:55431 已在监听——不是本会话启动的（本会话从未调用 `scripts.m0.postgres_lab`），应属并行的其它 agent，按「谁启动谁停」未动它。

## 5. PR 交付状态

- PR #32 最终 HEAD `9626a9e`（代码/证据止于 `4e6cb84`），均普通 push，PR 描述已追加「真实 Run 复验」小节（结果、run_id、费用、证据路径、变更、独立审查处置、局限）。
- `mergeStateStatus=CLEAN`；inline review threads 0、reviews 0。
- 新 HEAD 的 CI 为 workflow_dispatch，已触发：run `35239671187`。结果见文末「CI 结果」。
- 各 PR worktree 未发现未提交改动（#32 起始 `git status` 为空）；未碰 main，未合并任何 PR。

## 6. 独立审查处置（全新上下文 Sonnet 只读，范围 `e42d7b7..47c4860`）

| 编号 | 严重级 | 发现 | 处置 |
|---|---|---|---|
| F1–F3, F6, F7, F10–F12 | 无发现 | 根因确认（含 `context_time_policy_ids` 宽松 vs `eligible_time_policies` 严格的双标准解释）、`opspilot/` 零 diff、报告 1 双 JSON、`historical_window` 选择正确、参数化账本测试不会漂绿、证据无凭据/绝对路径且费用与账本一致、重放替身忠实、`scripts` 导入为既有惯例 | — |
| F4 | P3 | `reports.py:288-301` 与 `:223-285` 对同一 `time_policies` 字段两套严格度，是陷阱土壤 | 记为后续项（改 `reports.py` 触及校验器红线，不在本任务改） |
| F5 | P3 | M1 上下文沿用 `opspilot-evidence-context-v4` 标签但 `TimePolicy` 不含 v4 全部必填字段（既有） | 记为命名/合同后续项；文档未称 v4-compliant |
| F8 | P3 | 固定某次付费 Run 进验收套件是存在性证明非回归保证 | 接受并如实表述（测试名、notes 均限定为「一次 Run」，`passes` 未改） |
| F9 | P3 | `acceptance-output.txt` 丢失 HEAD/干净树来源行 | 采纳：`4e6cb84`/`2189e73` |

## 7. 未完成项

- PR #32 新 HEAD 的 CI 结果（见文末）；机器人安全审查按 AGENTS.md 不作门槛。
- `47c4860`（#33 `ScriptedInvestigator` 复用 helper）只在集成分支，需 #32 合并后由 #33 吸收；否则 #33 分支保留自己的手写副本（功能等价，不阻塞）。
- 后续项 F4/F5 未立 issue（外部 issue 需另行授权），已写入本报告与任务记录。
- integ 报告第 5.2 节（Web 追问文本未转发到 `DurableStore.control(payload=)`）不在本任务范围，未处理。

## 8. 需要用户裁定的事项

1. **F3 正向证据的口径**：`bbf10e0e` 是 fixture 数据、单工具、2 请求、结论 `partial` 的存在性证明。是否把它计入 M1-01「真实调查产出可用报告」的入口条件，还是要求在真实 Prometheus/更多请求预算下再跑？本次未改 `passes`。
2. **M1 evidence_context 与 v4 `TimePolicy` 的关系**（审查 F5）：继续沿用 `opspilot-evidence-context-v4` 标签只填 `id/mode/window`，还是在 M1-01 合同里明确 M1 上下文是 v4 的子集/另立版本号？这会影响 loop 与 harness 的后续接口，不在本任务自行决定。
3. **F4 双标准是否要收敛**：让 `context_time_policy_ids` 也只承认可绑定的策略（更严格、会把这类 harness 错误更早暴露为配置错误而非 REPORT_INVALID）属于校验器行为变更，需要用户批准后另开任务。

## CI 结果

workflow_dispatch CI run [35239671187](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35239671187)，head `4e6cb84`：`completed / success`。PR #32 状态：最新 CI 成功、独立审查完成且发现已处置、threads 0、`mergeStateStatus=CLEAN` 随后的仅文档提交 `9626a9e` 亦已单独跑 CI：run [35239936096](https://github.com/kevinWangSheng/production-ops-agent/actions/runs/35239936096) `success`（checks / m0-postgres 均 success），`mergeStateStatus=CLEAN`、`mergeable=MERGEABLE` → **PR 已就绪，待用户审核合并**（未合并，未碰 main）。
