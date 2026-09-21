# 调查 loop 长程执行边界改造方案（草案，待用户决定）

日期：2026-09-21。状态：**设计草案，已按 2026-09-21 独立审查修订**（审查结论「需修改后实施」，修订项见第 12 节），未实施、未改任何冻结值或验收步骤。
对象：[PR #29](https://github.com/kevinWangSheng/production-ops-agent/pull/29)
`feature/m1-01-investigation-loop`（HEAD `9f3506f`，领先 `origin/main` 52 个提交，`CLEAN`）。
上游对标证据见 [调研总览](../research/upstream-agent-loop-benchmark-2026-09-21.md)；
本文只记录设计判断与实施合同，不重复源码摘录。

## 1. 结论

1. **运行时：继续手写 loop，不引入 LangGraph，也不引入 OpenAI Agents SDK。**
   维持 [ADR-0004](../adr/0004-langgraph-orchestration.md) 的「推迟」结论，理由见第 3 节。
2. **持久 transcript 就是 `opspilot_steps` 业务行，不新建 messages 表。**
   缺的是「从行重建模型可见上下文」的纯函数和「把 loop 挂到 `Worker.resume` 之后继续跑」的组合层，
   不是新的存储。
3. **上下文压缩按 HolmesGPT 的两段式结构实现（2026-09-21 用户决定「按参考的来」）**：
   (a) 单工具结果超过单工具 token 上限时，模型可见消息替换为指针 stub（evidence_id + 状态 + 预览），完整 view 仍在 `tool_results`；
   (b) 每轮模型调用前，若 `估计 tokens + MAX_OUTPUT_TOKENS > 预算 × 阈值比例`，用一次模型请求把历史摘要为一条 user 消息，
   保留 system + 摘要 + 最后一条 user；摘要仍超预算则 `CONTEXT_EXHAUSTED`（Holmes `CompactionInsufficientError`）。
   与 Holmes 的差异只有合同强制的三点：摘要消息**前置一段确定性来源摘要**（逐 view 的 evidence_id/target/window/status，PC「压缩必须保留证据来源」）；
   摘要请求计入本 Run 物理模型请求预算并按同一 reserve/settle 记账；摘要请求失败时不回退到原始历史继续（Holmes 行为），
   而是保留旧上下文并显式 handoff（C3 §5「压缩失败时保留旧上下文并交接」）。压缩结果作为新的 context 表示随下一步 ModelStep 持久化，原始行不动。
4. **上下文预算用「保守估计 + provider 回报 `usage.prompt_tokens` 校准 + fail-closed」测量**，
   不引入 tokenizer 依赖；HTTP 字节上限保留为传输层第二道闸。
5. **4 次模型请求保持为 M1-01 冻结上限**，但从 loop 的硬编码变成 `RunLimits` 参数：
   loop 接受任意上限（测试可 >4），产品组合层在 accept 时强制 `limits <= M1 冻结值`。
   提高冻结值是新的用户决策，本方案不做。
6. **跨 Run 续接只做「交接摘要 + 续接上下文构造器」**，新 Run 的创建仍是人工 / Controller 动作
   （现有 `new_run` 合同要求先 cancel），不自动串 Run。理由见第 4 节。
7. **PR 策略建议：合并 #29 当前有界切片，长程改造作为 main 上的新 PR。** 备选是在 #29 上重做。
   两者实现内容相同，区别只在审查范围；待用户决定（第 9 节）。

## 2. 已核查事实（静态源码，非运行证明）

| 项 | 当前状态 | 出处 |
|---|---|---|
| loop 执行模型 | 内存 `messages` 列表；`run()` 一次性从 `InvestigationRequest` 组装；无 resume 入口 | `opspilot/investigation/loop.py` `InvestigationLoop.run/_round` |
| 模型请求上限 | `MAX_MODEL_REQUESTS_PER_RUN = 4`；`InvestigationRequest.model_requests` 校验 `<= 4`（`loop.py:209-214`），`_call_model` 另有两处直接比较该常量（`loop.py:537`、`:559`），物理计数 `_physical_requests` 是内存字段、每次 `run()` 置 0（`:257`）；最后一次预留给最终报告 | `limits.py`、`loop.py` |
| 步骤键 | `logical_key = "round-{ordinal}"`，无 `context_segment`；domain 已有 `step_id(run_id, context_segment, logical_round)` 与 `ModelStep.input_snapshot_hash` 但 loop 未用 | `loop.py`、`opspilot/domain/runs.py` |
| 预算 | 每个物理请求 `reserve_budget` → `settle_budget(spent/unknown)`；`DurableStepStore` 按 lease epoch 命名空间预留；工具次数/秒数在 `opspilot_tool_charges`（#20） | `store.py`、`persistence.py` |
| wall time | `RUN_WALL_SECONDS` 按**本次 attempt** 的 `clock.monotonic()` 起点计算，重启即重置 | `loop.py` `_remaining_timeout` |
| 持久化输入 | `opspilot_runs` 没有 question / scope / evidence_context / variant / 工具面；新 worker 无法从 PG 重建 system/user 消息 | `persistence.py` `install()` |
| 恢复（main 已合并 #30） | `Worker.resume` → 版本门 → `claim` → `rebuild` → `RecoverySession.execute_pending` 只重放 pending tools；不重建 messages、不继续模型循环、不发布 | `opspilot/worker.py`、`opspilot/recovery.py` |
| `rebuild()` | 一致快照读 incident/run/steps；校验每个非 `late_result` 步骤的 tool plan/结果形状（fail-closed `INCONSISTENT_STATE`）；pending 按 incident 代际过滤 | `persistence.py` `rebuild` |
| 上下文压缩 | 产品无。M0 有确定性压缩器 `scripts/m0/compressor.py`（整组折叠、摘要只含 `folded_groups/tool_call_ids/evidence_ids/tool_content_sha256`，**不含** target/window/status；丢弃折叠组的 reasoning_content），并有一次真实 provider 折叠证据：14 条消息折叠 4 组后 DeepSeek HTTP 200、`deepseek-flash`、`finish_reason=stop`，但 transcript 为合成 id 且未记录请求是否带 `tools`/thinking | `docs/evidence/m0-real-investigation/round-07-compressor-real-run.json`、`round-07-wp2-results.md:7-8`；ROADMAP「压缩器接入」为 M0 遗留项 |
| DeepSeek 约束 | 带 `tools` 的请求必须回传**此前各轮**在 transcript 中的 `reasoning_content`，否则 400；C3 §5：压缩/恢复后续传不兼容时阻塞交接，不猜测删除协议字段 | [DeepSeek 参考 §1.3](deepseek-flash-prompt-tool-reference.md) |
| 真实 Run 观测 | 1 次工具轮后 prompt 1036 → 1189 tokens；M002 校准集 prompt 603–106,736 tokens | `docs/evidence/m1-01-investigation-loop/ledger.json`、v4 包 |

## 3. 运行时判断：为什么维持手写

LangGraph 在持久化维度提供的是：super-step 边界 checkpoint、节点级 pending writes、
从节点开头 replay、`thread_id` 寻址。逐项对照 main 已有能力：

| LangGraph 机制 | OpsPilot 已有等价物 | 差异 |
|---|---|---|
| checkpoint（super-step 后） | `commit_step`（模型响应提交） | 已有，且按业务键 `(run_id, logical_key)` 幂等 |
| pending writes（节点级） | `commit_tool` 按 ordinal 幂等；`rebuild()["pending_tools"]` | 已有，且带 incident 代际过滤 |
| `get_state` / replay | `rebuild()` + `RecoverySession.execute_pending` | LangGraph 的 checkpoint 确实持久化含 messages 的 graph state 并按 `thread_id` 恢复；**不可用的原因是合同**：C3 §7「每次执行尝试创建新的 Graph 执行身份，不跨尝试恢复旧的 latest checkpoint 或 pending task」、ADR-0003「checkpoint 不能授权从旧 epoch 继续」。因此跨尝试的消息重建无论如何都必须从业务行做 |
| thread_id | `incident_id / run_id / epoch / control_generation` | LangGraph 没有 lease/epoch/人工代际栅栏；[状态协议研究](../research/state-protocol-design-input-2026-09-07.md) 已指出需要 epoch 命名空间 + CAS 提升 accepted checkpoint 才能防旧 worker，即**第二套状态层** |
| 上下文压缩 / token 预算 | 无 | LangGraph 也没有，属应用职责 |

[ADR-0003](../adr/0003-business-state-recovery-authority.md) 规定 PG 业务行是唯一跨进程恢复依据、
graph checkpoint 只是 attempt 局部缓存。引入 LangGraph 意味着 checkpoint 表永远不能作权威，
所有重建逻辑仍要自己写一遍，再加一层对账。净效果是成本没有收益。
OpenAI Agents SDK 的 Session / RunState / compaction session 是最清晰的**概念参考**
（历史存储、暂停状态、压缩分层），但采用它要经其 ChatCompletions 适配传 DeepSeek 的
`reasoning_content`（C3 §5 记录的适配缺口正是这类），且同样带来第二套历史存储。

**反转条件**：若后续需要多节点并行 fan-out 且每节点独立持久，重新比较；届时另立合同，不在本 PR 顺带引入。
（节点内人工 interrupt 不构成反转条件：`opspilot/domain/runs.py:57` 已规定人工回复以新执行尝试恢复，不在原地续跑，与 LangGraph in-place resume 语义相反。）

## 4. 对任务书假设的修正

| 任务书假设 | 本方案处理 | 理由 |
|---|---|---|
| 压缩「可采用手写或 LangGraph 运行时」 | 原稿推荐纯确定性折叠；**用户 2026-09-21 决定按 Holmes 参考实现 LLM 摘要 compaction**，确定性来源摘要作为强制附加，见第 1 节第 3 条 | Holmes 的 LLM 摘要把整段历史（含 reasoning_content）送去总结，摘要成为产品记忆，与「不把隐藏思维链作为产品记忆」和 PC「压缩必须保留证据来源」冲突；确定性折叠零模型费用、可重算、可先红后绿测试。**移植时新增**（M0 压缩器没有）：折叠摘要按 view 逐条保留 evidence_id/operation_id/tool/source/target_id/window/observed_at/freshness/status/truncated/adopted，作为 P2 验收断言（PC「压缩必须保留证据来源」） |
| 「上下文预算必须可测量，不能只依赖 HTTP 字节」→ 隐含 tokenizer | 保守估计（bytes/4）× 本 Run 校准因子（`usage.prompt_tokens / 估计`，单调只增），每步把估计与实测都落库 | DeepSeek tokenizer 是新依赖且随模型换代漂移；Holmes/OpenSRE 在非 OpenAI 模型上同样是估计；真实测量只有 provider 的 `usage`，而每一步的 usage 已经持久化 |
| 「支持在同一 Incident 下创建或接续新的 Run」 | 只做 handoff 结论 + `continuation_context()` 构造器；创建新 Run 保持人工 / Controller | C3 §13「过期后使用新 Run 接续，不静默延长旧执行权」、`new_run` 现有合同要求先 cancel；自动串 Run 等于把预算耗尽变成无界工作，PC 禁止 |
| 「为调查 Run 建立清晰的持久执行状态」 | 不新建表；`opspilot_runs` 增 `input` 快照与 `active_seconds_used`，`opspilot_steps` 增 `context` 列 | 步骤行已经是有序 transcript；再建 messages 表会出现两份权威 |
| 验收「进程在 4 个位置退出」 | 全部保留，并增加「最终报告已提交、publish 未确认」（C3 §7 第 5 行） | 现有合同的断点比任务书多一个 |
| 「不允许同时引入两套拥有相同调查循环的框架」 | 同意；本方案零框架 | — |

## 5. 目标架构

```
Worker.resume(incident)                      # 已有：版本门 → claim → rebuild
  └─ RecoverySession.execute_pending()       # 已有：只重放 pending tools
  └─ InvestigationRunner.continue(session)   # 新：组合层
       ├─ snapshot = store.rebuild(incident)
       ├─ input    = snapshot.run.input       # 新列：question/scope/evidence_context/variant/limits/tool_face
       ├─ transcript = context.rebuild_transcript(snapshot, input, versions)   # 新：纯函数
       │     raw   : 有序 [system,user,(evidence)] + 每个 live 步骤的 [assistant,*tool]
       │     visible: 按最后一个 live 步骤记录的 compaction 链折叠后的表示
       ├─ if 最后步骤是已校验最终报告且 conclusion 为空 → publish（不再调模型）
       └─ InvestigationLoop.resume(transcript, limits_remaining)   # loop 拆成 start()/resume() 共用 _drive()
             每轮：context.prepare(visible) → 估计/校准/折叠或 CONTEXT_EXHAUSTED
                   → commit_step(logical_key=f"{segment}:round-{n}", response, context=...)
                   → 工具执行、commit_tool、view 过大则模型可见 stub
                   → 最终报告 / handoff 结论 → commit_step + publish
```

模块：

- `opspilot/investigation/context.py`（新）：`Transcript`、`rebuild_transcript()`、
  `estimate_tokens()`、`Calibration`、`fold()`（移植 `scripts/m0/compressor.py`，产品代码不 import scripts）、
  `view_stub()`。全部纯函数，可在无 PG 下测试。
- `opspilot/investigation/limits.py`：新增 `RunLimits` dataclass 与 `M1_FROZEN_LIMITS`；
  现有常量保留并作为冻结值来源。
- `opspilot/investigation/loop.py`：`run()` 拆为 `start(request)` / `resume(transcript)`；
  `LoopState` 持有 raw/visible/delivered/evidence_ids/rounds/physical/segment/active_seconds。
- `opspilot/investigation/runner.py`（新）：`InvestigationRunner`，唯一同时认识 `Worker`、
  `InvestigationLoop`、`ReadOnlyToolExecutor` 的模块；最终报告与 handoff 结论的 `publish` 在这里。
- `opspilot/persistence.py`：加列（见第 6 节），`commit_step` 增可选 `context` 参数，
  `settle_budget` 增可选 `seconds`，`accept/new_run` 增可选 `input`；`rebuild()` 原样返回新列。
  全部向后兼容：旧行 `context IS NULL` 视为 `segment="ctx0"`、无折叠。

## 6. 持久状态模型

| 表 / 列 | 新增 | 语义 |
|---|---|---|
| `opspilot_runs.input jsonb` | 是 | 输入快照：`question`、`scope`（window/target_ids/deadline）、`evidence_context`（已投影，含 `time_policies`，供 resume 重建 `DeliveredView.time_scope_refs`）、`variant_id`、`bound_target_id`、`limits`、**`tool_schemas` 数组本体**与 `tool_face_sha256`。C3 §5 要求「实际内容或固定持久引用」，只存 sha 两者都不是；resume 直接用持久化数组，registry 重生成结果只用于比对并记录差异（L3b 实例层，不阻断） |
| `opspilot_runs.active_seconds_used double precision` + `opspilot_budget_reservations.reserved_seconds / seconds` | 是 | 模型请求活跃时间**先预留后结算**（仿 `charge_tool`）：reserve 时按本次请求的 timeout 上界占用，settle 时核减为实测；请求中崩溃则上界永久占用（C3 §13「租约状态不明时保守计量」）。工具秒数已在 `tool_seconds_used`，其旧 epoch 未结算预留同样永久占用——这是既有行为，意味着**每次请求中崩溃都会缩小剩余预算**，长程 Run 须接受 |
| `opspilot_steps.context jsonb` | 是 | `{segment, round, input_snapshot_hash, estimated_prompt_tokens, calibration, compaction: null \| {revision, policy, folded_step_ids, digest_sha256, before, after}}` |
| `opspilot_steps.logical_key` | 格式变 | `"{segment}:round-{n}"`，`n` 全 Run 单调；`segment` 为 `ctx{k}`，k = 折叠次数。domain `step_id()` 的 `(run_id, context_segment, logical_round)` 三元组由 logical_key 可重算；`opspilot_steps.step_id` 主键仍是 uuid4、`operation_id` 仍基于该 uuid，**不**改为哈希键 |
| `versions["context_policy_revision"]` | 是 | 折叠策略与估计器版本的内容哈希；策略变更让在途 Run `blocked(INCOMPATIBLE_STATE)`（C3 §5 revision 规则） |

**兼容性口径**：`versions` 比对是整 dict 相等（`persistence.py:435`、`worker.py:261`），新增键会让**所有部署前在途的旧 Run 在 claim 时 `blocked(INCOMPATIBLE_STATE)`**，这是 C3 §5 要求的正确行为；「旧行 `context IS NULL` 视为 `ctx0`」只服务于已完成 Run 的读取与 UI 回放，不存在「旧在途 Run 续跑」的兼容分支，实施时不要写。

原始行不可变：折叠只写 `context.compaction`，`response` / `tool_results` 不改。
模型可见的 view stub 也只存在于出站消息，`tool_results` 保留完整 view。

## 7. 恢复矩阵

| 断点（C3 §7 + 任务书） | 重启后行为 | 幂等/预算 |
|---|---|---|
| 模型响应提交前退出 | 旧预留留在 `reserved/unknown`；新 epoch 重新预留 `{segment}:round-n#a1`，同一 `logical_key` 重发 | 预算不重置。跨 attempt 的幂等**不是**靠 `commit_step` 的 existing 分支：新 attempt 的 `claim` 与旧 attempt 的 `commit_step` 都先锁 incident 行，串行化后要么旧 commit 先落地（claim 后的 `rebuild` 已看到 round-n，新 attempt 从 n+1 继续），要么 claim 先落地（旧 commit 被栅栏写成 `late_result`）。规则：**rebuild 看到 round-n 已 live 提交 → 从 n+1 继续** |
| 响应已提交、工具执行前退出 | `execute_pending` 重放全部 pending ordinal | operation_id 稳定；工具 ledger 再计一次（C3 §7 不承诺 exactly-once，已记录） |
| 工具已执行、结果提交前退出 | 同上，外部查询重复一次 | 同上 |
| 工具结果已提交、下一轮模型调用前退出 | `rebuild_transcript` 得到完整组，下一轮 `round-{n+1}` | 已完成工具**不**重查（`_still_pending` + rebuild 校验） |
| 最终报告已提交、publish 未确认 | runner 重新校验报告引用后直接 `publish`，零模型请求 | `publish` 已幂等 |
| 人工 cancel，或在途 attempt 期间 `control_generation` 变化 | `_assert_current` → `CONTROL_DENIED`；旧代际 late_result 行永不进入 transcript | 已有 |
| 同 Run 的 `follow_up` / `correct` / `pause→resume` 后重新 claim | `control()` 只把同一 Run 置回 queued 并递增代际，Run 仍可 claim；旧代际步骤的 pending tools 被 rebuild 丢弃且新租约不能补提交（PG 测试 `:846-880` 已断言）。`rebuild_transcript` 因此会遇到**旧代际的不完整组**。规则：旧代际中结果齐全的组照常进入 raw transcript；不完整组**整组**不进模型可见上下文，并记入 `context.compaction.dropped_incomplete`（可审计，不静默）；随后按 `input_watermark` 在该边界纳入新输入（C3 §5「先重建已提交步骤，再在明确边界纳入新输入」）。main 尚无 follow-up 文本通道，本 PR 只留 `inputs` 接缝 | 新增 |
| 授权收紧后（scope 变化） | `rebuild_transcript` 按当前 `scope.target_ids` 过滤 tool view：目标不在授权内的 view 不进模型输入，`delivered` 同步剔除（C3 §7「撤权后旧受限证据不得再进入新模型输入」） | 新增 |
| 最终报告请求已返回但校验失败（`REPORT_INVALID` / `OUTPUT_LENGTH`）或 `CONTEXT_EXHAUSTED`，随后进程退出 | 目前 persistence 没有 failed/handoff 的写路径，重启后只剩「预留已满 → `BUDGET_EXHAUSTED`」，真实原因丢失。规则：runner 提交一条**非模型 handoff 步骤**（`logical_key="handoff:{reason}"`，`response={"kind":"handoff","assistant":{"role":"assistant","content":null},"handoff":{...}}`，无 tool plan，`rebuild_transcript` 跳过 `kind=handoff` 行）并以该步骤 `publish` 结论；`publish` 的 `response == conclusion` 严格相等由 runner 用同一对象保证 | 新增；`RecoverySession.publish` 遇 `FINAL_STEP_REQUIRED` 放弃租约的既有取舍在此路径下需复审 |
| `publish` 已成功、旧 attempt 未收到确认 | Run 已 `completed`、conclusion 非空，`Worker.resume` 返回 `CONTROL_DENIED`；runner 须先读 `recovery_metadata`/conclusion 把它识别为「已完成」，不报失败 | 新增 |
| 其他 Run 的行 | `rebuild_transcript` 断言每行 `run_id == lease.run_id`，否则 `INCONSISTENT_STATE` | 新增防御 |
| 折叠记录引用了不存在/不属本 Run 的 step_id，或策略版本不符 | `INCOMPATIBLE_STATE` → blocked handoff，不猜测重算 | fail-closed |
| 折叠后仍超预算 | `CONTEXT_EXHAUSTED` handoff（Holmes `CompactionInsufficientError` 的等价物），不静默截断 | — |

## 8. 预算与轮次语义

| 维度 | 计数位置 | 上限来源 | 重启 |
|---|---|---|---|
| 逻辑轮 | `opspilot_steps` live 行数 | 由模型请求上限约束 | 从行数续 |
| 物理模型请求（含 provider 重试） | `opspilot_budget_reservations` 三列求和 | `RunLimits.model_requests ≤ 4` | 不重置（已有） |
| 工具次数 / 秒数 | `opspilot_tool_charges` | `≤ 20 / 240s`（#20） | 不重置（已有） |
| 活跃时间 | `active_seconds_used` + 本 attempt 增量 | `≤ 1800s` | 不重置（新） |
| 绝对期限 | `opspilot_runs.deadline` | 授权 | 已有 |
| 上下文 | 每步 `context.estimated_prompt_tokens` 与 `response.usage.prompt_tokens` | `131072 − 16384 − 余量`（v4 包第 16 行与 `limits.py:20` 为 16,384；同文件第 10 行的「含32768输出」是旧候选值），触发折叠阈值 = 预算 × 比例 | 校准因子随 transcript 重算 |

已承认的估计风险：bytes/4 对 uuid/时间戳密集的 tool view 低估约 2 倍，首轮无校准（盲区）；校准因子单调只增，之后只会更保守；512KiB/4 恰等于 131,072，未校准时字节闸与 token 闸几乎重合，校准后 token 闸才先触发，因此字节闸不是独立保护。供应商实际窗口为 1M（参考 §1.7），`CONTEXT_EXHAUSTED` 是本地 fail-closed，不会先撞供应商 400。

停止条件与上游一致：最终文本且无 tool_calls → 候选结论校验；上限触顶 → 预留的无工具最终请求
（Holmes `tools=None if i==max_steps`、OpenSRE safety handoff 的等价物）；
取消 / 期限 / 上下文耗尽 → 显式 handoff。可选项（不在验收内）：OpenSRE 的停滞检测——
同一 `(tool, canonical params)` 重复 ≥2 次时返回引用既有 evidence_id 的拒绝 view 而不派发。

## 9. 需要用户决定的事项

1. ~~PR 策略~~：2026-09-21 用户决定 A。实施分支 `feature/m1-01-loop-long-horizon`（worktree `../production-ops-agent-loop-long-horizon`），起点 #29 头 `9f3506f`，#29 合并后 rebase 到 main 再开 PR。原选项：A）合并 #29 现有有界切片，本方案作为新 PR（推荐：#29 已 52 提交 / 49 轮审查且 `CLEAN`，
   新 PR 审查范围干净，符合 #39 不做 stacked PR 与机器人审查停机规则）；
   B）在 #29 分支上继续重做，PR 描述重写。实现内容两者相同。
2. ~~压缩方式~~：2026-09-21 用户决定按上游参考（HolmesGPT）实现，见第 1 节第 3 条。
3. **`reasoning_content` 与折叠的真实 provider 冒烟改为 P2 前置条件**（1 次请求，常设授权内）：
   round-07 证据只证明「折叠后的合成 transcript 被接受」，未证明「请求带 `tools`+thinking、剩余组带真实 reasoning_content」时不 400。
   推断依据是 API 无状态（参考 §1.1）、折叠删除的是整组消息而非字段；这是推断，不是已证。

## 10. 分阶段交付与验证

| 阶段 | 内容 | 验证 |
|---|---|---|
| P1 持久状态与恢复 | 新列、`RunLimits`（同时替换 `loop.py:257/537/559` 三处内存计数与常量比较，物理计数改读三列求和）、步骤键、`rebuild_transcript`、loop `start/resume`、`InvestigationRunner`、handoff 步骤、publish 续接 | 确定性 fake：≥6 逻辑轮；PG 集成：5 个断点各 1 条 subprocess kill 用例（子进程内需一个**可控 fake ModelClient**，现有用例只在 claim 后 kill）、预算/活跃时间不重置、代际隔离、malformed fail-closed、同 Run follow_up/correct 后续跑且旧代际不完整组不进上下文、终态原因在重启后保留、`publish` 已成功后 resume 识别为完成、重复查询被计两次、旧 attempt 迟到 `commit_step` 落为 late_result 且不进 transcript、resume 后 `delivered`/引用校验与原 attempt 一致 |
| P2 上下文管理 | 估计器（**含 tools 数组与 response_format**，M0 版只算 messages）+ 校准、view stub、折叠 + `context.compaction` 持久化（含逐 view 来源字段）、`CONTEXT_EXHAUSTED`、`context_policy_revision` | **前置**：1 次带 `tools`+thinking 的真实折叠冒烟。确定性：膨胀触发折叠、折叠摘要含全部来源字段、折叠后 evidence 引用可解析、折叠记录被 rebuild 精确复现（segment k 跨 attempt 一致）、策略变更 blocked |
| P3 交接 | handoff 结论 DTO、`continuation_context()`、任务记录 / ADR-0004 复核注记 / PR 描述 | 确定性：新 Run 上下文只含已采纳证据且 run_id 重绑；独立审查 |

每阶段 `make check` + `M1_DURABLE_POSTGRES=1` 定向 PG；先红后绿；不改 11 个 `passes`、
不改 v4 冻结值、不调用真实模型（P2 冒烟除外，另行记录）。

## 11. 实施与草案的差异（2026-09-21，P1–P3 完成后）

| 草案 | 实施 | 原因 |
|---|---|---|
| `opspilot_steps.context` 新列 | 写在步骤 `response` 载荷的 `context` 键内（`segment/round/input_snapshot_hash/estimated_prompt_tokens/calibration`） | 不改 `rebuild()`/`publish()` 的列契约，`SELECT *` 与 `response == conclusion` 比较原样成立 |
| `opspilot_runs.active_seconds_used` 列 | `opspilot_budget_reservations.reserved_seconds / seconds` 两列，`run_usage()` 按「已结算取实测、未结算取上界」求和 | 与预留/结算同一条记录，不需要第二个累加器 |
| 非模型 handoff 步骤 `handoff:{reason}` | 统一为 `kind=conclusion` 终态步骤（`conclusion:{segment}:round-{n}`），完成与 handoff 都经它 `publish` | 一条写路径覆盖 C3 §7 第 5 行与「预算耗尽不是完成」两种终态；载荷不含私有协议字段，可直接导出 |
| 压缩记录随下一步 ModelStep 持久化 | 压缩本身是一条 `kind=compaction` 步骤（`{segment}:compact-{k}`，含 `folded_step_ids/digest/accepted`），重建按顺序回放 | 摘要请求是真实模型响应，C3 要求提交；`accepted=false` 的行保留旧上下文 |
| `versions["context_policy_revision"]` | `investigation_versions()` = `prompt_revision` + `context_policy_revision`，调用方合并 `tool_schema_revision` | 同草案 |
| 折叠摘要「keep 最新 1 组」 | 按 Holmes 折叠前缀之后的全部消息 | 用户决定按参考实现 |
| P2 前置真实冒烟 | 已执行：`scripts/m1_compaction_smoke.py`，2 次请求均 200，见 `docs/evidence/m1-01-loop-long-horizon/compaction-smoke.json` | 第 9 节第 3 项关闭；估计器首轮校准因子见该文件 |
| runner 对 blocked 的落库 | 新增 `DurableStore.block(lease)`（同栅栏），`claim()` 不静默恢复 blocked | 草案第 7 节 fail-closed 行需要一条写路径 |
| 本 worktree PG lab | 55431 被 `m1-human-control` worktree 实例占用，本任务在 `tmp/m1-lh/postgres` 起 55432 实例，测试经 scratchpad 脚本改写 `DSN` 运行 | 项目约定不共用他人实例；未改仓库脚本 |

## 12. 独立审查记录（2026-09-21）

全新上下文子代理只读审查，结论「需修改后实施」，七项取舍均判定成立。采纳并已写入本文的修订：
第 2 节两处事实修正（round-07 折叠证据、`loop.py` 三处硬编码）；第 3 节 LangGraph 论证改为合同禁止；
第 4/5 节折叠来源字段列为移植新增项、真实冒烟升为 P2 前置；第 6 节 tools 数组持久化、活跃时间先预留后结算、
`versions` 新键的兼容口径、step_id 措辞收窄；第 7 节修正「提交前退出」幂等描述并新增 follow_up/correct、
撤权过滤、非报告终态、publish 已成功四行；第 8 节承认估计器风险；第 10 节补 9 类用例与子进程 fake 模型客户端。
审查列出的三大翻车点（segment/k 跨 attempt 确定性、旧代际悬空组、终态无写路径）分别对应第 6/7 节新增规则，
实施时各需一条先红后绿用例。

## 13. 来源

HolmesGPT `773fddf`：`tool_calling_llm.py` L1147–1210（`max_steps`、末轮无 tools、每轮前 compaction）、
`tool_context_window_limiter.py`（单工具 spill）、`compaction.py` / `input_context_window_limiter.py`
（阈值公式、`CompactionInsufficientError`）、[context-management 文档](https://holmesgpt.dev/0.24.3/reference/context-management/)。
OpenSRE `4303874`：`react_loop.py` L180–330（`max_iterations`、停滞、cancel、safety handoff）、
`context_budget.py`（整对淘汰、pinned、overflow 后收缩重试）、`lifecycle.py` L167–315（session resume 内容）。
OpenAI Agents SDK `ad93f54`：`running_agents.md`（`max_turns`、RunState）、`sessions/index.md`
（compaction session、失败回滚）、`results.md`（Session 写入失败的 fail-closed 恢复）。
LangGraph 官方 persistence / durable-execution 文档（super-step checkpoint、pending writes、durability 模式）。
本仓库：C3 §5/§7/§13、ADR-0002/0003/0004、v4 冻结包、`scripts/m0/compressor.py`、`scripts/m0/step_store.py` `rebuild()`。
