# #304 合同预审：最终报告被拒交接后 follow_up 被 INCOMPATIBLE_STATE 阻塞

预审者：全新上下文、只读，未参与实现。基线 `main` a7b296a（#309 之后）。没有改仓库文件，没有跑 make check、全量 pytest 或 Codex，没有连 kind。复现用的定向单测放在 scratchpad：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/923c6c5a-354f-4d80-b818-cd62113f3099/scratchpad/repro304/test_repro_304.py`，4 passed，0.43s，用 MemoryStepStore，不连 PG。

## 1. 根因（单测已验证；具体到 1b21375d 这一 Run 是强推断）

**live 路径发了什么。** 一个非最终轮的回复没有工具调用，报告校验又失败时，L1a/B3 置 `state.force_final = True`，并带上 `state.retry_feedback`（`opspilot/investigation/loop.py:787-798`）。下一轮是强制最终轮，发送的尾部消息由 `_final_messages` 生成（`loop.py:132-145`），顺序是：

1. `FINAL_REPORT_INSTRUCTION`
2. **`retry_feedback`**（`report_retry_feedback` 套固定模板，一定非空，`opspilot/investigation/reports.py:471-508`）
3. `run_coverage_message(delivered)`

该步记录的 `input_snapshot_hash` 是对这整串消息取的哈希（`loop.py:1246`）。

**重建路径校验了什么。** `_check_snapshot_hash`（`opspilot/investigation/context.py:1190-1229`）对 `context.final=true` 的行只补 `FINAL_REPORT_INSTRUCTION` 和 `run_coverage_message`（`context.py:1218-1227`），**漏了中间那条 retry_feedback**。所以只要 Run 里有一行是 L1a 强制出来的最终轮，任何对同一 Run 的重建都会在这一行抛 `INCOMPATIBLE_STATE`（`context.py:1229`，由 `context.py:960` 调用）。

**由来。** coverage 消息在 #58（1e7d4b8）进入哈希校验。retry_feedback 在 #59（a3efdfa）加进 `_final_messages`，但 `_check_snapshot_hash` 没跟着改。`_final_messages` 的 docstring 写着「Rebuild re-derives the same retry_feedback」，这只对 `resume` 成立（`loop.py:533-546` 在 resume 时重算 feedback），哈希校验那一侧没有重算。

**单测验证**（scratchpad 文件，四个用例均通过）：

- 回放「工具轮 → 非最终轮回复无效 → 强制最终轮回复也无效」：结果是 `failed (REPORT_INVALID)`，与 1b21375d 的交接原因相同。最终轮实际发送的尾部三条消息依次为 instruction、"The previous reply was not accepted…"、coverage。记录的哈希等于实际发送消息的哈希；`rebuild_transcript` 抛 `INCOMPATIBLE_STATE`。
- `advance_generation` 之后加 follow_up 输入，再重建：同样抛 `INCOMPATIBLE_STATE`，复现 issue 描述的现象。
- 对照组：靠预算耗尽进入的最终轮（没有 feedback），重建正常。
- **影响面比 issue 写的大**：强制最终轮回复了合格报告，但 worker 在 conclusion 行提交前崩溃，重建同样 `INCOMPATIBLE_STATE`。也就是说，本该发布的合格报告在重启后会被阻塞。C3 §7 断点表里「工具结果齐全 → 重建下一步上下文」这条恢复承诺，在 L1a 路径上失效了。

**1b21375d 为什么属于这一类（强推断，未直接查行）。** 该 Run 的防循环上限是 100 次模型请求，第 9 轮 `final=true`。按 `_drive`（`loop.py:641`）和 `_round`（`loop.py:698-701`），非 L1a 的最终轮只在剩余预算 ≤1 时出现，第 9 轮不可能满足；所以第 9 轮只能是 L1a 强制最终轮，第 8 轮必然是「无工具、未通过校验」的回复。去掉 event 也失败，也与此一致：`input_watermark=0`，输入消息本来就不参与。**未确认**：没有直接读 1b21375d 第 8 轮的行。原库 `/Users/shenghuikevin/dev/AI/poa-f2-c/tmp/pg-f2c-live` 仍在（已停）。可以复制数据目录后起临时 PG，读该行的 `context.final`、`assistant.tool_calls`、`content` 来确认（见 D-U2）。

**不受影响的路径。** 跨 Run 接续（`continuation_context` / `committed_views`，`context.py:1302-1316`）不走哈希校验，所以 cancel + new_run 能用，这与证据一致。

## 2. 修复方向

| 方向 | 做法 | 代价 | 恢复路径 / 已存数据 / 证据兼容 |
|---|---|---|---|
| **A 重建侧重算 feedback（推荐）** | 把 `_validated_report` 的「解析 + 引用检查 + feedback」抽成 `reports.py` 里的纯函数，live 与重建共用。`rebuild_transcript` 记住上一条模型行：同代际、非工具轮、非最终、没有被拒计划。遇到下一条 `final=true` 的行时，用上一条行的 content/finish_reason、当时的 `delivered`、`authorized_targets` 和证据上下文重算 feedback，传给 `_check_snapshot_hash`，按 instruction → feedback → coverage 的顺序插入 | 小到中：抽一个函数，改两个调用点。重建要依赖校验器在两次部署之间输出一致；校验逻辑变了而模板没变时会出现哈希不一致，结果仍是 fail-closed 的 `INCOMPATIBLE_STATE` | 已存的行（含 1b21375d 这类）**不迁移就能恢复**。发送字节不变，不 bump `context_policy_revision`。不改数据合同。与 `resume` 已有的「从已提交 content 重算 feedback」机制同源 |
| B 最终轮行记录 feedback 原文 | `_commit_step` 在 `context` 里写入 `retry_feedback`（原文或 sha 加原文），重建直接插入记录的文本 | 小。但这是步骤行 JSON 的数据合同新增（无需迁移），会多存一份模型可见文本，这类文本本来已经在 trace 和 prompt 里 | 只修新行；旧行仍然阻塞，除非叠加 A 作为回退。证据兼容 |
| C A + B 叠加 | 新行用记录的文本，旧行回退到重算 | 两套逻辑，测试面翻倍 | 全兼容，但对一个开发期缺陷来说偏重 |
| D 放宽校验 | 对旧代际行或 `final` 行跳过哈希校验 | 最小 | 削弱 C3 §7「无法安全重建时阻塞」的 fail-closed 保护，且修不好同代际崩溃那条路径（合格报告丢失）。**不推荐** |

推荐 A：改动面最小，不触碰数据合同，能修复已存数据，而且与 resume 现有的设计同源。

## 3. 九类核对

1. **时间**：已定。本修复不引入时间语义；跨尝试的活动时间预算延续不变（`loop.py:resume` → `_open_state`，C3 §13「预算不重置」）。
2. **计数与口径**：已定。follow_up 后 `model_requests` 从库里累计值继续（C3 §13；`test_budget_and_active_time_continue_across_attempts`）。重建本身不消耗请求。follow_up 后旧代际最终行的 `concluded=True`（`context.py:1004-1009`），resume 不会再强制最终轮，而是进入普通调查轮。
3. **状态转移**：已定。follow_up 推进 `control_generation` → 同一 Run 重新排队 → worker 领取 → `runner._continue` → `rebuild_transcript` → `resume`（`opspilot/investigation/runner.py:367-400`）。修复只改「重建能否通过校验」，不改转移。
4. **主体与权限**：已定，不变。worker 角色和凭据不动；重建用 `executor.scope.target_ids`（`runner.py:369`），与 live 时 `request.scope.target_ids` 同源。边界：live 与重建之间授权发生变化时，见 D-R3。
5. **数据合同**：方向 A 无数据合同变更、无迁移、无部署顺序约束。方向 B 会新增步骤行 `context` 字段（见 D-U1）。
6. **旧数据与已归档证据**：方向 A 让既有 L1a 行可以重建。`docs/evidence/f2-alert-correlation-live/` 中断言 2 记为 FAIL 的结论不改写（用户 10-22 已裁决）；1b21375d 已是 `cancelled`，不复活。
7. **并发与水位**：已定。尾部顺序是输入消息在前、最终轮消息在后（`loop.py:_with_trailing`，`context.py:1213-1227`）。修复须保持「输入 → instruction → feedback → coverage」。有压缩时：压缩行与最终行之间 feedback 仍然存在（live 时 `state.retry_feedback` 跨 `_manage_context` 保留），所以重建判断「上一条模型行」时要跨过 accepted 压缩行（见 D-R2）。
8. **外部调用**：重建不做外部调用。修复后 follow_up 会发出真实模型调用，受剩余预算、单次超时和防循环上限约束；失败后的状态沿用现有交接语义。
9. **验收投影与 passes**：`feature_list.json` 里没有 follow_up 相关步骤（grep 无匹配）。F2 的 `passes` 按 `docs/tasks/2026-10-17-f2.md:83` 的裁决由 PR-E 翻转，本修复不翻任何 `passes`。按 AGENTS.md「状态恢复」类变更走**用户门**合并。

## 4. 决定表

### 需用户裁决（合同、验收）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| D-U1 | 修复方向，以及是否新增步骤行数据合同 | A 重建侧重算：不改数据合同，修复旧行，依赖校验器确定性。B 记录 feedback 原文：新增 `context` 字段，旧行不修。C A+B。D 放宽校验：削弱 fail-closed | **A**（不改数据合同，所以只是缺陷修复；选 B/C 才是数据合同变更） | 恢复路径、步骤行 JSON | |
| D-U2 | #304 PR 的真实运行证据怎么满足 AGENTS.md「触碰恢复路径附有界真实 Run trace + summary.json」，同时尽量少占 kind | ① 复制 `poa-f2-c/tmp/pg-f2c-live` 数据目录起临时 PG，对 1b21375d 的真实行做只读重建：修复前 `INCOMPATIBLE_STATE`、修复后通过，并确认第 8 轮是 L1a 行。0 kind 占用，但不产生 LangSmith trace。② 在 ① 之外，再在 kind 上做一次有界真实 Run：开 Run，待其交接（或发布前人工暂停），再 follow_up，同一 Run 恢复并发出模型调用，附 trace 和 summary.json。L1a 不一定自然触发，确定性由 PG 集成测试兼 ① 覆盖；占用约 10 分钟，可以不注入故障。③ 不单独跑，并入 PR-E 的 kind 运行：kind 占用最少，但 #304 PR 合并时没有真实 trace，与 ADR-0006 不符 | **① + ②**，② 用最小场景（不注入故障、单个 Run、单次 follow_up）；PR-E 照原计划再验 C4-A 断言 2 | 验收证据、kind 占用 | |

### 可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| D-R1 | 共用函数放哪里 | `reports.py` 新增纯函数（输入：content、finish_reason、delivered、evidence_context projection、authorized_targets；输出：(report, reason, feedback)），`InvestigationLoop._validated_report` 改为调用它 | live 与重建只保留一份规则，避免 #59 那种一边改了另一边没跟上的漂移 |
| D-R2 | 重建何时插入 feedback | 当前 `final=true` 行之前最近一条**模型行**（跳过 accepted 压缩行和 conclusion 行）满足：同 `control_generation`、无工具调用、`final=false`、无 `rejected_plan`。重算结果不合格时才插入 | 与 live 的 L1a 触发条件和 `resume`（`loop.py:533-546`）一致。预算型最终轮在没有前置失败时不插入 |
| D-R3 | 授权在 live 与重建之间变化 | 用重建时的 `authorized_targets` 重算；结果不一致时照常 `INCOMPATIBLE_STATE`；已有 revoked view 时 `diverged=True`，跳过校验，现状不变 | fail-closed 符合 C3 §7；不为这种罕见情况设计新机制 |
| D-R4 | 是否 bump `context_policy_revision` 或 prompt revision | 不 bump | 发送的字节和模板都没变，只修校验一侧；bump 会无谓地阻塞在途 Run（C3 §6 revision 规则只针对模板字节） |
| D-R5 | 同时修 `_final_messages` 和 `_check_snapshot_hash` 的 docstring | 修 | 现在的 docstring 声称重建会重算，与代码不符，这正是漏改的诱因 |
| D-R6 | 测试落点 | 单测加在 `tests/test_m1_investigation_context.py` 附近；PG 集成测试加在 `tests/integration/test_m1_loop_resume_postgres.py` | 与既有 resume/rebuild 测试同处 |
| D-R7 | 保留 `poa-f2-c/tmp/pg-f2c-live` | 在 #304 的 D-U2① 做完之前，不 `worktree remove` poa-f2-c | `worktree remove` 会删掉被忽略的实验数据；这是 1b21375d 的唯一真实行来源 |

## 5. 独立验收测试作者应覆盖的外部断言（IncidentScenario → IncidentOutcome）

只通过公开入口驱动（intake/control/worker 领取），用脚本化模型，结论只看可观察结果：

1. **主场景（#304）**：模型依次给出工具轮 → 无工具的不合格报告 → 强制最终轮的不合格报告。期望 Run 交接 `REPORT_INVALID`，进入 `waiting_human`。人工 `follow_up` 后：同一 `run_id` 被重新领取；**不是** `blocked(INCOMPATIBLE_STATE)`；至少发出 1 次模型调用；该调用的输入里有 follow_up 文本；随后 Run 达到合格发布，或者以不同于 `INCOMPATIBLE_STATE` 的原因交接。
2. **附加事件变体（对齐 F2 C4-A 断言 2）**：在 1 的交接之后先写入一条 `kind='event'` 输入，再 follow_up。恢复后第一次模型调用的输入里同时有 event 文本和 follow_up 文本。
3. **崩溃变体**：强制最终轮回复合格报告，worker 在 conclusion 提交前终止。新 worker 领取后发布这份报告，不额外消耗模型请求，也不阻塞。
4. **预算累计**：follow_up 后的 `model_requests_used` 从交接前的累计值继续，不重置；总数不超过上限。
5. **fail-closed 保持（负向）**：篡改强制最终轮之前那条模型行的 content（等价于 feedback 不同），重建仍然 `blocked(INCOMPATIBLE_STATE)`，0 次模型调用。确认修复没有放宽校验。
6. **回归对照**：预算型最终轮（无前置失败）交接后 follow_up，照常恢复。
7. 断言只看状态、交接原因、模型调用次数与输入内容、发布结果；不断言内部函数调用顺序或思维链。
