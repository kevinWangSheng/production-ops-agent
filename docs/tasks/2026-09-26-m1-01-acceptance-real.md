# M1-01 验收入口：持久化场景改由真实产品在 PostgreSQL 上产生

- 状态：进行中
- 更新日期：2026-09-26
- 依据：ROADMAP「M1-01 剩余工作」第 5 项；[验收记录](2026-09-16-m1-01-acceptance.md)独立审查 P2 与 Codex P1
  （四个 durable 行喂的是手写状态/标记，`late_result_rejected`/`worker_resumed`/`STALE_CONTROL_GENERATION`
  无产品路径产出）；[ADR-0005](../adr/0005-handoff-and-deadline-terminal.md)（#44 交接不发布、#45 清扫、
  #47 超时续 Run）；AGENTS.md「验收入口是外部 `IncidentScenario -> IncidentOutcome`」「验收步骤只能收紧」。
  功能 ID M1-01。
- 工作区：`feature/m1-01-acceptance-real`，`../production-ops-agent-acceptance-real`

## 目标与范围

让 `make acceptance` 的每个持久化场景都由产品代码在真实 PostgreSQL 上写出、再由入口投影读回：
DurableStore、InvestigationRunner/Worker/RecoverySession、`control()`、超时清扫。不再有手写的 run 状态
或标记。`outcome_from_durable` 按 #44/#45/#47 之后的行形状投影（交接停在 `waiting_human`、结论为空）。
不含：web/worker 入口（另一分支）、`passes`、v4 包、重构、依赖。入口合同 `IncidentScenario ->
IncidentOutcome` 未改。

## 前提与完成条件

- 前提：PG 实验实例；模型与工具用既有确定性替身，不需要真实模型。
- 完成条件：四个 durable 行 + 新增交接/超时行全部由产品路径产生；`make check`、PG 集成、`make acceptance`
  通过；独立审查无未处置 P1/P2。

## 投影规则（`opspilot/acceptance.py::outcome_from_durable`）

只读产品记录：`DurableStore.rebuild` 快照、`DurableIncidentStore.control_audit`（`opspilot_controls`）、
`DurableEventLog` 的 `run_handoff` 事件。

| 可观察项 | 来源 | 旧实现 |
|---|---|---|
| 行状态 | 仅接受产品写出的终态 `completed / waiting_human / paused / cancelled / blocked`；`queued`/`running`/`failed`/`budget_exhausted` 行拒绝 `UNKNOWN_DURABLE_STATE`；`completed` 却无已发布结论拒绝 | 接受 `failed`/`budget_exhausted` 行；`completed` 无结论投影为 completed |
| 交接（`waiting_human`） | 用产品自己的 `pending_conclusion` 读当前代际的已提交结论步骤 → execution / reasons / evidence；无结论步骤（清扫）时理由取该 Run 的 `run_handoff` 事件（`parked=True`），没有事件则理由为空、不猜 | 抛 `UNKNOWN_DURABLE_STATE` |
| `report_available` | 只在已发布结论 execution=completed 且 schema 为字符串时为真；停靠的「完成形」结论步骤不算 | 同前半 |
| `late_result_rejected` | 快照 `steps` 中存在 `status='late_result'` 行（`commit_step`/`commit_tool`/`publish` 被栅栏拒绝时写入） | 测试手写标记 |
| `STALE_CONTROL_GENERATION` | late 行的 `control_generation` ≠ 事故当前代际；同代际的 late 行（只是租约过期）不加此理由 | 随手写标记附带 |
| `worker_resumed` | `run.epoch > 1`（每次 `claim()` 递增） | 测试手写标记 |
| `human_interaction` | `controls` 中 `resulting_generation == 当前代际` 的那条人工动作；更早的动作是历史 | 调用方传 `action=` |
| `blocked` 理由 | 事件 `parked=False` 的 reasons，否则 `INCOMPATIBLE_STATE`（行本身说不出是版本不兼容还是不可解码） | 固定 `INCOMPATIBLE_STATE` |

事件只补理由，不改变从行读出的状态（ADR-0003：投影非权威）。

## 场景（`tests/integration/test_m1_01_acceptance_postgres.py`，需 `M1_DURABLE_POSTGRES=1`）

| 行 | 产品路径 | 断言要点 |
|---|---|---|
| durable-handoff-pg | runner 两次 `MODEL_UNAVAILABLE` → `hand_off` | 行 `waiting_human`、结论 NULL；投影 failed/handoff/(MODEL_UNAVAILABLE,)、无报告；`follow_up` 仍能重排队 |
| deadline-exceeded | `claim` 后把数据库 deadline 移到过去（与清扫套件同一手法，唯一的测试干预）→ runner 轮询清扫 | 投影 failed/handoff/(DEADLINE_EXCEEDED,) 来自事件；无事件时理由为空 |
| pause-cancel | runner 在第一轮工具后崩溃（租约在手）→ `control(pause)` → `control(cancel)` | paused/cancel 各自为终态、`human_interaction` 来自审计；暂停中 runner `control_denied`；已提交证据仍可见 |
| late-result | executor hook 在真实查询发出前 `control(cancel)` → `commit_tool` 拒绝并写 `late_result:tool:*` 行；loop 停机、`hand_off` 被栅栏拒 | runner `control_denied`；投影 cancelled、`late_result_rejected`、`STALE_CONTROL_GENERATION`、证据为空 |
| worker-restart | 崩溃 → 等租约过期 → 新 Worker（新 owner）resume，epoch 2，不重查 | completed、`worker_resumed`、证据与已发布结论一致、有报告；首轮完成的 Run 无该标记 |
| incompatible-state | Worker 版本不同 → `claim()` 写 `blocked` | blocked/(INCOMPATIBLE_STATE,)；兼容 Worker 不静默恢复；`cancel` 仍可用 |

单元层（`tests/acceptance/test_m1_01_acceptance.py`）：删除四个手写行；新增 `test_projection_*` 钉住上表规则
（MemoryStepStore 真实 loop 行 + 明示的形状构造，不列入验收表）。`durable-handoff` 行保留并改标签：
MemoryStepStore 发布交接结论是 ADR-0005 之前的形状，入口仍拒绝把它当报告（收紧保留）。

`scripts/m1_acceptance.py`：按 pytest 摘要区分 PASS / SKIPPED / FAIL，未开启 PG 的行打印 SKIPPED 且整体退出 1，
不再把跳过当通过。

## 执行进展与证据

- 先红后绿：新规则 8 个单元用例在旧实现下失败（`UNKNOWN_DURABLE_STATE` / 缺 kwargs），实现后 29 → 36 passed。
- `make check` 退出码 0：ruff/format/mypy 通过（42 source files），1945 passed / 233 skipped / 2 xfailed。
- PG 集成（55431 被 web-worker 分支的实例占用，先在 scratchpad 起了一次性 PostgreSQL 17.9 于 55432，
  用脚本把 `scripts.m0.postgres_lab.DSN` 改到 55432 后收集）：新增 6 例全过；全套
  `tests/integration` 179 passed / 54 skipped（跳过为 `M0_B_POSTGRES`/`M0_CONTROL_POSTGRES` 门）。
  说明：第一次全套运行只改了 loop-resume 模块的 DSN，其余模块连到了 55431 约 70 秒，出现 6 个失败，
  判定为两实例互相干扰（未逐一核实），已向 lead 披露；改为源头改 DSN 后复跑全绿。
- `make acceptance`（55431 自有实例）：待 55431 释放后执行并刷新
  `docs/evidence/m1-01-acceptance/acceptance-output.txt`。
- 未跑真实模型 Run：本项只改验收入口、测试与运行器，未触碰 loop / runner / 恢复 / 校验代码。

## 下一步与交接

- 独立审查（全新上下文）；处置 P1/P2。
- PR → CI → 机器人分诊一次 → CLEAN、0 未处理 thread；不合并（用户门：验收）。
- 未验证/留给 owner：`blocked` 行无事件时投影固定为 `INCOMPATIBLE_STATE`，`_block_undecodable`
  （`INCONSISTENT_STATE`）路径只有事件能区分；清扫停靠的 Run 若事件写入失败，行说不出原因（#44 已记的
  投影补写待办）。
- 55432 一次性实例：任务结束 `pg_ctl stop`，数据目录在 scratchpad，随会话丢弃。
