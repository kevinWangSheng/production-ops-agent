# M1-01 调查 loop 长程执行边界改造

- 状态：**实现完成；独立审查 2 P1 / 4 P2、机器人第一轮 4 条 P1、第二轮 1 P1 / 2 P2、第三轮 2 P1 / 1 P2 全部采纳修复并复验；第四轮 3 P2 采纳、1 P1 拒绝（F5）；随后结构收敛 + 全新上下文独立审查（1 P1 / 2 P2 / 6 P3，P1–P2 与 3 条 P3 已修复）；机器人第五轮 3 P1 / 1 P2 采纳；按停机规则不再逐轮改码，待最新提交 CI；PR #29 待最新提交 CI 通过后即「PR 已就绪，待用户审核合并」**
- 更新日期：2026-09-22
- 前序：[Flash 调查 loop 任务记录](2026-09-16-m1-01-investigation-loop.md)、PR #29（`9f3506f`，`CLEAN`，待用户审核合并）
- 依据：SPEC 有界开放 M1-01；PRODUCT-CONSTRAINTS；C3 §5/§7/§13；ADR-0002/0003/0004；
  v4 冻结包 B2 段；[上游对标调研](../research/upstream-agent-loop-benchmark-2026-09-21.md)；
  [设计草案（含独立审查与实施差异）](../design/investigation-loop-long-horizon-2026-09-21.md)
- 用户决定（2026-09-21）：改造直接算在 #29 内（一个 PR，整体完成后合并；此前的 A 方案作废）；压缩「按参考的来」→ HolmesGPT 两段式（单结果 stub + LLM 摘要 compaction）
- 工作区：worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-loop-long-horizon`，
  实现在 `feature/m1-01-loop-long-horizon`（起点 #29 头 `9f3506f`）完成后快进到 `feature/m1-01-investigation-loop`；本任务专属 PG：`../production-ops-agent-loop-long-horizon/tmp/m1-lh/postgres`，端口 55432

## 目标与范围

把 PR #29 的单 Run 内存 loop 改造成：从 PostgreSQL 业务行重建模型上下文并继续、可测量的上下文预算与
Holmes 式压缩、模型/工具/活跃时间/上下文预算分离且重启不重置、显式 handoff 与跨 Run 续接上下文构造器。
范围外：UI、intake、observer、postmortem、自动创建新 Run、修改 11 个 `passes`、修改 v4 冻结值、引入框架。

## 实现（提交 `cf56d46..HEAD`）

- `opspilot/investigation/limits.py`：`RunLimits` / `M1_FROZEN_LIMITS`；loop 的上限从常量变为参数，产品边界（runner）拒绝超冻结值。
- `opspilot/persistence.py`：`opspilot_runs.input` 输入快照；预留表 `reserved_seconds/seconds`（活跃时间先预留后结算）；`run_usage()`；`block(lease)`；`accept/new_run` 可选 `input`。
- `opspilot/investigation/store.py`：接缝增 `usage()`、seconds 参数、`MemoryStepStore.snapshot()`；`DurableStepStore` 每次物理请求前续租。
- `opspilot/investigation/context.py`（新）：`InvestigationInput`、步骤键 `{segment}:round-{n}`、`rebuild_transcript()`、估计器 + 校准、`visible_view()` stub、`fold_digest()`/`compaction_message()`、`context_policy_revision()`、`continuation_context()`。
- `opspilot/investigation/loop.py`：`run()/resume()` 共用 `_drive()`；逻辑轮与物理请求分开计数；活跃时间跨 attempt；每轮前 `_manage_context()`（Holmes 阈值公式；压缩失败或仍超预算 fail-closed）；终态统一提交 `kind=conclusion` 步骤。
- `opspilot/investigation/runner.py`（新）：`Worker.resume` → `execute_pending` → `rebuild_transcript` → `loop.resume` → `publish`；已发布/控制拒绝/不兼容/malformed 各有明确结果，malformed 落库 `blocked`。
- `scripts/m1_compaction_smoke.py`（开发脚本）：真实 provider 折叠冒烟。

## 验证证据

| 项 | 命令 / 工件 | 结果 |
|---|---|---|
| 静态 + 单元 | `make check`（机器人第五轮修复后 HEAD） | `ruff` 全过、`mypy` 31 文件无问题、`1659 passed, 167 skipped, 2 xfailed`；第二轮修复时 1 条 `tests/test_m0_adapters.py::test_physical_timeout_retains_unknown_and_no_tools`（M0 适配器 10ms 物理超时竞态）在本机 main `b483a12` 上同样失败，与本改动无关，以 CI 为准 |
| 确定性恢复 | `tests/test_m1_investigation_context.py`（25 条）+ `test_m1_investigation_compaction.py` 新增被拒压缩行 resume 用例 | 超 4 逻辑轮、5 个断点重启、代际丢弃、撤权 stub、malformed fail-closed、活跃时间跨 attempt、截断回复/被拒计划在 resume 上与在线路径同判、follow-up 之后新提交的报告在 resume 上仍完成 |
| 确定性压缩 | `tests/test_m1_investigation_compaction.py`（13 条） | 阈值触发、字节级重建、压缩后重启、摘要失败 handoff、不足两槽 `CONTEXT_EXHAUSTED`、摘要仍超预算、计费、stub、校准、策略哈希 |
| 跨 Run 续接 | `tests/test_m1_investigation_continuation.py`（9 条） | 后继 Run 引用前 Run 证据并完成、越权证据不携带、二次交接保留继承证据、opaque target_refs 经 catalog 授权、prompt 升版后 transcript 不可重放仍可接续、旧代际未完成组已提交视图可携带、opaque ref 按前 Run 授权解析、current 策略 ref 不跨 Run、fail-closed |
| PG 集成 | `M1_DURABLE_POSTGRES=1`，DSN 改写到 55432：`test_m1_loop_resume_postgres.py`（18 条）+ 既有 4 个 M1 PG 套件 | `113 passed`（含既有 94 条无回归；新增迟到结果按 epoch 分键、畸形步骤行落库 blocked、跨版本畸形行报 blocked 各 1 条） |
| 真实 provider 冒烟 | `.venv/bin/python -m scripts.m1_compaction_smoke` | 2 次 HTTP 200、`deepseek-flash`、带 tools + thinking；`prompt 3422 / completion 2147` tokens；费用上界见 `docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json`；估计 vs 实测校准因子已记录 |

不是产品验收：11 个 `passes` 未改；未做真实 Run 全链路；worker 进程级 kill 只在既有 `test_worker_subprocess_kill_then_resume_from_business_rows` 覆盖 claim 后一点，其余断点用进程内异常 + 租约过期等价模拟。

## 独立实现审查（2026-09-21）

全新上下文子代理审查 `9f3506f..HEAD`，结论「修复后可合并」；2 个 P1（压缩吞掉报告槽、已提交报告崩溃后被改写为预算耗尽）、
4 个 P2（被拒计划被 recovery 执行、旧代际 conclusion 永久拒绝 publish、rebuild 不比对哈希、字节级测试不真）全部采纳修复，
每项配先红后绿用例；处置表见设计文档第 13 节。

## 机器人分诊（2026-09-22，`@codex review` 一次）

4 条 P1 全部采纳修复，一条发现一个提交，各配先红后绿用例，thread 已回复并 resolve：
`1075b04` runner 在 claim 后重读快照并校验 run_id；`5d25bcb` conclusion 键含控制代际、被取代的旧报告不再自动重新完成；
`f5c0328` 畸形字段的时间策略整条丢弃（F5 必填字段裁定不变）；`e0cde69` 超时后主动 shutdown HTTPS 连接。
修复后 `make check` `1643 passed, 164 skipped, 2 xfailed`；PG 全套 `110 passed`。

## 机器人分诊第二轮（2026-09-22，机器人对 `3e2a4ca` 自动复审）

1 P1 + 2 P2 全部采纳修复，一条发现一个提交，各配先红后绿用例，thread 已回复并 resolve：

| 发现 | 核实 | 处置 |
|---|---|---|
| P1 `loop.py:455` resume 用硬编码 `finish_reason="stop"` 解析已提交的尾轮文本，`length` 截断但恰好是合法 JSON 的回复会在重启后被接受 | 属实：行里持久化了 `finish_reason`，但 `Transcript` 未带出；同类还有被拒计划行（`rejected_plan`）在崩溃后会被当作新一轮 | `c345451`：`Transcript` 新增 `last_finish_reason / last_final / last_rejection`；resume 先按已提交裁决收尾（接受、末槽按解析原因失败、或按记录的拒绝原因失败），不再合成 finish reason |
| P2 `context.py:875` `continuation_context` 只从本 Run 工具结果行重建 bindings，二次交接丢失继承证据 | 属实 | `346ed5e`：先从投影后的前 Run context 以同一授权过滤播种继承 bindings，再合并新采集视图 |
| P2 `persistence.py:936` `late_result:step:<key>` 无 epoch，`ON CONFLICT DO NOTHING` 吞掉第二个被围栏尝试的物理回复 | 属实；tool / publish 的迟到键同类 | `5703f51`：三类迟到键追加 `:e{lease.epoch}`；同一尝试重放仍合并为一行 |

修复后 `make check` `1646 passed, 165 skipped, 2 xfailed`（另 1 条 M0 适配器超时竞态本机环境失败，见验证表）；PG 全套 `111 passed`。

## 机器人分诊第三轮（2026-09-22，机器人对 `7a55ec9` 自动复审）

2 P1 + 1 P2 全部采纳修复，一条发现一个提交，各配先红后绿用例（提交前以反向应用产品 diff 验证三条用例确实先红），thread 已回复并 resolve：

| 发现 | 核实 | 处置 |
|---|---|---|
| P1 `runner.py:74` 首次 `rebuild()` 在任何 handler 之外，畸形已提交步骤行触发 `INCONSISTENT_STATE` 时 worker 每次重试都崩溃，Run 到不了 `blocked` | 属实：`rebuild()` 解码 `_tool_plan` 失败即抛 `PersistenceError`，此时没有租约可供 `block()`；claim 后的第二次 `rebuild()` 同样未包 | `afc4a25`：经 `recovery_metadata()`（不解码步骤）取 run_id → `worker.claim` → 栅栏内 `block()`；claim 后的解码失败转 `ContextError` 走既有 block 路径；再次 resume 得 `control_denied` |
| P1 `loop.py:422` `superseded_conclusion` 是 Run 级布尔：follow-up 之后新代际提交了合法报告、崩溃于 conclusion 前，重启后被跳过，多花一次请求或误判 `BUDGET_EXHAUSTED` | 属实 | `ca79e4f`：该标志改为描述尾部候选——遇被取代 conclusion 行置位，其后任一轮重放即清除 |
| P2 `context.py:919` 继承 binding 只有 opaque `target_refs`、无 `target_id` 时，registry id 子集测试一律拒绝 | 属实：v4 schema 合法 binding 正是这种形状 | `7140228`：先经 `context_target_catalog()` 解析 ref 再做授权判断，与引用校验一致 |

修复后 `make check` `1649 passed, 166 skipped, 2 xfailed`；PG 全套 `112 passed`。机器人自动复审至此三轮（第一轮为显式 `@codex review`），发现数逐轮 4 → 3 → 3 且均为真实缺陷，继续按发现逐条处置；若再出现同量级发现，改派独立审查一次性覆盖 resume / continuation 路径而非继续逐轮推送。

## 机器人分诊第四轮与结构收敛（2026-09-22，机器人对 `253e1cf` 自动复审）

用户指示：机器人意见是建议不是强制，按实际取舍；并要求在反馈循环中守住一个原则——判断改动是否越来越偏离设计、小步补丁对整体性是否友好，再选择合适的改法。

| 发现 | 核实 | 处置 |
|---|---|---|
| P1 `reports.py:340` 缺 v4 required 字段的时间策略仍通过投影，配合调用方提供的 binding 可被引用 | 冻结 schema 确实要求 6 个字段；但投影合同是形状白名单而非 schema 校验（docstring 已记 F5 开放）；同一可信调用方同样能给字段齐全的假策略，存在性检查不缩小信任面；强制需改写 8 个测试文件的 M0 fixture | **拒绝并回复依据**，thread 已 resolve；F5（required 存在性在 intake 还是 loop 强制）登记到后续，待用户裁定 |
| P2 `client.py:257` `http.client.HTTPException`（`IncompleteRead` 等）不是 `OSError`，逃过 client 与 loop 的处理导致崩溃 | 属实 | `9eec1a3`：并入 `MODEL_UNAVAILABLE` 映射 |
| P2 `loop.py:937` `_remaining_timeout` 只按墙钟算本次 attempt，快速失败按超时上界记账后重试仍可拿到整段超时，账面超过 `active_seconds` | 属实 | `076c177`：attempt 消耗取 `max(墙钟, 已记账秒数)` |
| P2 `loop.py:987` conclusion 的 `model_seconds_used` 只含本次 attempt，与累计的 `model_requests_used` 不一致 | 属实 | `076c177`：`prior_model_seconds + seconds_used`，`LoopOutcome` 同步 |

**偏移判断**（推送前做的检查）：
- 第二、三轮在 resume 路径的三次修复是补丁叠补丁：`Transcript` 积累 4 个零散尾部字段（`last_finish_reason / last_final / last_rejection / superseded_conclusion`），`resume()` 里是一份与在线 `_round()` 平行的判定代码，第三轮 P1 正是第二轮补丁留下的洞。
- 第二、三轮在 `continuation_context` 的两次修复是在第二套实现里补洞：它自行扫描步骤行并重推授权，而 `rebuild_transcript` 已经计算「已采纳且仍授权的视图」。
- 机器人发现数 4 → 3 → 3 → 4 未收敛，触发上面记录的停机条件。
- 第四轮的三条 P2 是局部合同修正（HTTP 异常、预算、累计口径），不构成偏移。

**收敛**（`e3c1a53`，`refactor:`）：`Transcript.last_round: CommittedRound` 替代 4 个字段；`_round_verdict()` 成为在线与 resume 的唯一裁决函数；`continuation_context` 直接取 `rebuild_transcript(前 Run).delivered` 经 `view_targets_authorized()` 过滤，删掉行扫描与两处手写授权。净减 191 行、增 248 行（含 docstring）。既有 resume / continuation 用例全部保持通过：`make check` `1651 passed, 166 skipped, 2 xfailed`，PG 全套 `112 passed`。设计文档第 11 节补两行差异记录。

收敛后不再逐轮推送：先派一次全新上下文独立审查覆盖 runner / resume / continuation 整条路径（含 F5 拒绝的合理性），处置后一次性推送。

## 独立审查（收敛后，2026-09-22，全新上下文，范围 `3e2a4ca..e3c1a53`）

结论「列出的 P1/P2 修复后可合并」；审查方自行跑了 127 条单元 + 77 条 PG，并写了 3 个场景测试证实缺陷。全部发现对应证据处置如下：

| # | 级别 | 发现 | 处置 |
|---|---|---|---|
| 1 | P1 | 收敛把 `continuation_context` 建在 `rebuild_transcript` 上，继承了字节哈希与 reasoning 配对检查；prompt/context policy 升版后 `blocked(INCOMPATIBLE_STATE)` 的 Run（C3 §7 点名的接续对象）拿不到任何证据；旧代际未完成组已提交的视图也被整组丢弃 | 采纳 `56c292c`：新增 `committed_views()` 只读业务行（含部分完成组的已提交 ordinal），经同一个 `delivered_view` + `view_targets_authorized` 派生；审查方的两个场景改写为仓库用例（monkeypatch `render` 模拟升版；二工具组死于第二个结果后 follow-up） |
| 2 | P2 | 被拒压缩行（`accepted=false`）在重建时被跳过，resume 会再次压缩并复用同一个 `compact-1` 键，`commit_step` 返回旧行而内存上下文已切段，之后的行无法重建 | 采纳 `94a065c`：被拒行置 `last_round.rejection=COMPACTION_FAILED`（与在线裁决一致）；压缩键按尝试次数而非已接受版本计数 |
| 3 | P2 | runner：claim 自身因版本不符 block 时报成 `control_denied`；`block()` 被栅栏或存储拒绝时仍报 `blocked`；claim 之后的 `PersistenceError` 裸抛 | 采纳 `294a3f9`：只有栅栏写成功才报 `blocked`，否则报实际拒绝码；claim 后的持久层拒绝转为类型化结果 |
| 4 | P3 | 无 `reasoning_content` 的工具轮：在线先派发工具再配对失败，崩溃后重建则 `INCOMPATIBLE_STATE`（既有） | 记入后续：在线路径应在派发前拒绝该计划 |
| 5 | P3 | `attempt_spent` 未计本次 attempt 新增的工具时间 | 采纳 `666ae9c` |
| 6 | P3 | `view_targets_authorized` 对映射为 `None` 的 catalog 键比 `unsupported_citations` 更严 | 采纳：保持保守方向，docstring 记录理由（跨 Run 携带是新授权） |
| 7 | P3 | 接续说明取第一条 conclusion 行，被取代后描述的是最早结果 | 采纳（含于 `56c292c`）：取最新一条 |
| 8 | P3 | follow-up 取代未发布的 completed conclusion 且预算耗尽时，resume 发布 `budget_exhausted`，已接受报告只留在行里 | 记入后续：属追问通道的产品语义 |
| 9 | — | F5 拒绝的合理性 | 审查方认定拒绝成立（投影是形状白名单；binding 由同一可信方提供；`eligible_time_policies` 实际已要求 `id/mode` + window/max age）；指出「fixture 改动量」不是理由，开放的 F5 决策才是——已按此修正拒绝依据的表述 |

修复后：`make check` `1655 passed, 167 skipped, 2 xfailed`；PG 全套 `113 passed`；5 条新用例先红后绿（反向应用产品 diff 验证）。

## 机器人分诊第五轮（2026-09-22，机器人对 `86dfccb` 自动复审）与停机

3 P1 + 1 P2，逐条核实后全部采纳（均为本 PR 新增/改动代码中的具体缺陷，修法小且与合同一致），各配先红后绿用例：

| 发现 | 处置 |
|---|---|
| P1 `continuation_context` 用后继 Run 的授权解析 opaque ref，未映射的 canonical catalog 条目会被重绑到后继的唯一目标 | `ffeb168`：按前 Run 记录的 `scope_facts.target_ids` 解析，再按后继授权过滤 |
| P1 `current` 模式策略的 `time_scope_refs` 原样跨 Run，后继可用过期证据发布 current 事实 | 同上提交：携带证据视为历史，`current` 策略 ref 不跨 Run；后继需要 current 事实须重新观测（写入 `Continuation` docstring 与设计文档第 11 节） |
| P1 在线工具循环不续租，一次接近 360s 的模型请求后 420s 租约剩余不足以完成本轮工具，落为迟到历史并 `CONTROL_DENIED` | `7076073`：`StepCommitter.renew()`；durable store 在每次派发前同栅栏续租，拒绝即派发前停机（与 pending-tool 恢复路径一致） |
| P2 `except HTTPError` 内部的排空 `exc.read()` 抛 `IncompleteRead` 时，同级 `HTTPException` 分支看不到 | `984352b`：排空分支自行捕获 |

修复后 `make check` `1659 passed, 167 skipped, 2 xfailed`；PG 全套 `113 passed`；4 条新用例先红后绿。

第六轮（对 `0c1ae81`）：2 P2。`runner.py` claim 后 executor 工厂抛 `ToolContractError` 导致租约悬挂至过期——本次新写路径的明确缺陷，采纳 `ebfa3a0`（新增 `aborted` 结果并释放该租约，PG 用例先红后绿；`make check` `1659 passed, 168 skipped`，PG `114 passed`）。`messages.py` `assistant_message` 对约 500 层嵌套工具调用 `deepcopy` 触发 `RecursionError`——原有界 loop 旧代码、对抗性输入、不落行且已由崩溃恢复路径覆盖，拒绝并登记后续。

第七轮（对 `d921c5d`）：2 P1。携带 `target_catalog` 未物化前 Run 解析出的映射，后继可把未映射 canonical 条目重解析到自己的唯一目标——第五轮同一根因的残留，采纳 `dbe0217`（携带条目写入前 Run 解析的 `target_id`；用例先红后绿；`make check` `1659 passed, 168 skipped`，PG `114 passed`）。`tools/executor.py` 的 `dispatch_started_at` 取 `operation.started_at` 而非 `authorized_at`——该文件不属本 PR 改动范围（本 PR 仅 3 行且不含此处），差值为毫秒级控制/账本检查耗时，拒绝并登记后续。

**停机决定**：本 PR 改造部分已经过 5 轮机器人自动复审（4 → 3 → 3 → 4 → 4，未收敛）、1 次结构收敛、1 次全新上下文独立审查。按任务记录前文与项目经验规则，自此不再为机器人新一轮发现逐轮改码：新发现按类别核实后，属本 PR 新增代码的明确缺陷才修，其余以回复给出依据并登记后续；并向用户汇报由用户决定是否合并或继续。

## 未完成 / 后续

- 审查 P3 后续项：`MODEL_REJECTED` 按超时上界计活跃时间（过保守）；`DEADLINE_EXCEEDED` 后无可落库终态；
  runner 不校验 `Worker.versions` 是否含 `context_policy_revision`；dropped 组只在内存 `Transcript`；
  `executor_factory` 合同未写明须从工具 ledger 回填 `tool_seconds_used`。
- PR #29 描述已重写；三轮机器人分诊已逐项处置；等待最新提交 CI；合并仍走用户门。main 分支保护开启 required conversation resolution，未 resolve 的 thread 会使 `mergeStateStatus=BLOCKED`。
- 供应商余额差记账（2 次冒烟请求）。
- 独立审查 P3 #4：无 `reasoning_content` 的工具计划应在派发前拒绝（在线与重建同判）。
- 机器人第六轮 P2（拒绝）：`assistant_message` 先投影工具调用字段再复制，或把 `RecursionError` 映射为固定模型失败码，与 `client.complete()` 对超深响应体的处理对齐。
- 机器人第七轮 P1（拒绝，工具执行器范围）：视图 `dispatch_started_at` 改用 `operation.authorized_at`，随 M1-01 tool executor 任务处理。
- 独立审查 P3 #8：follow-up 取代未发布 completed conclusion 且无预算时的产品语义，随追问通道工作一起定。
- **F5 待用户裁定**：v4 `TimePolicy` required 字段（`id/revision/integration_id/interfaces/mode/reference_rule`）的存在性检查放在 intake 边界还是 loop 投影层；当前产品代码没有运行时 v4 schema 校验器，冻结包只在 M0 脚本/测试里按 schema 校验。裁定后在对应边界统一实现，并同步 8 个使用最小 policy 形状的测试 fixture。
- 跨 Run 自动接续、UI 展示 compaction/handoff、`opspilot_inputs` 追问通道接入 transcript 均不在本 PR。
