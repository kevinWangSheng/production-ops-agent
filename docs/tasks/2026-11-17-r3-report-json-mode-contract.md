# #686 PR-R 合同预审 r1：每轮都开 JSON 模式（U7），去壳取消

- 预审者：全新上下文、未参与实现，只读。行号指 `origin/main` `aa86233d`（工作台 P1 #698 已合并），worktree `/Users/shenghuikevin/dev/AI/poa-686-prereview`。
- 已定裁决不重提：U7（每轮都发 `response_format`，接受工具轮没有进度说明，空白内容算失败）、去壳取消（U6 作废）、U2=a、U3=a、D6、U5=a。
- 答案隔离：没有打开 ground_truth / assertion / status / few_shot，没有看 `~/.agent/handoffs`。
- 结论先说：**需用户裁决 1 项（新）**，可逆默认 10 项；钉旧规则的测试 2 处，验收测试 0 处；探针 1521 项里 2 失败。

## 0. 现状（已核对）

| 事实 | 出处 |
|---|---|
| 调查轮的请求只在终答轮开 JSON 模式：`json_mode=final` | `opspilot/investigation/loop.py:809` |
| `response_format` 只在 `json_mode` 为真时进请求体；`tools` 与它互不排斥，代码层面本来就能同时发 | `loop.py:222-226` |
| 压缩请求固定 `json_mode=False`，它的指令写明 "Plain text only, no JSON" | `loop.py:1269`、`opspilot/investigation/context.py:191` |
| 空白内容**已经**是失败：`parse_report` 对 `None`、空串、纯空白返回 `EMPTY_REPORT`；重试提示对 `EMPTY_REPORT` 不带明细 | `opspilot/investigation/reports.py:219-220`、`:648-662` |
| 工具轮（带合格工具调用）不解析内容为报告：`tool_round=True` 直接进下一轮；内容只在「本轮发过回复说明」时交给 `tool_round_visible`，纯空白返回「无说明、无回复」 | `loop.py:641-642`、`:864-879`、`reports.py:1443-1444` |
| 重建/恢复只比对消息哈希 `input_snapshot_hash = messages_hash(messages)`，不含 `response_format`、`tools`、`max_tokens` | `loop.py:1421`、`context.py:1488-1516` |
| 步骤行另记整个请求体的 `request_sha256`（含 `response_format`），产品代码没有任何地方回读比对 | `loop.py:1316-1318`、`:1411-1413`；`grep request_sha256 opspilot/` 只有写入 |
| `versions` 只有 `prompt_revision`（模板字节哈希）、`context_policy_revision`、`tool_schema_revision`；`ModelProfile.adapter_revision` 是 DTO 字段，循环不用 | `loop.py:103-173`、`context.py:291-293`、`opspilot/domain/runs.py:81-89` |
| 上下文估计器只算 messages 和 tools，不算 `response_format`（设计文档说「含 response_format」，代码没有）；体积闸按真实请求字节算，含它 | `context.py:296-316`、`loop.py:1537`、`docs/design/investigation-loop-long-horizon-2026-09-21.md:183` |
| 评测录制把整个 `ModelCall`（含 `json_mode`）存进录制文件，校验时用存下来的 call 复算请求体；重放不比对请求体 | `opspilot/evaluation/recording.py:538-543`、`:1035-1058`、`opspilot/evaluation/replay.py:249-286` |
| trace 的模型 span 记录 `opspilot.model.json_mode` | `opspilot/tracing.py:472` |
| C3 正文没有写「工具轮不发 response_format」；它只在三处链接 DeepSeek 参考 | `docs/design/technical-proposal-2026-09-07.md:165`、`:306`、`:514` |
| 旧规则的原文只在 DeepSeek 参考里，且该文件自称「参考资料，不是验收标准，不改变任何已批准合同」 | `docs/design/deepseek-flash-prompt-tool-reference.md:3`、`:120`、`:124`、`:308` |

## 1. 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| G1 | F3 第 5 步的真实 Run 证据（竞争原因场景，run-1/run-2）是在「只有终答轮开 JSON 模式」时跑的。那条验收的说明写着「模型可见面变了，证据通常作废，除非有登记过的等价证明」（2026-10-26 裁决）。现在每轮都开 JSON 模式：提示词、工具、视图一个字节都没变，验收测试照样通过，但模型在工具轮不再写说明文字，行为确实变了。这份证据还算数吗 | a 算数，不重跑：机械检查比的是提示词/工具/投影三个版本，都没变；在 PR-R 的任务记录和这次有界真实 Run 的 run.md 里写明「请求参数自本次起每轮带 response_format」。b 按「行为面变了」处理，在租机上重跑 F3 第 5 步两次真实 Run 并搬迁证据（要租机，约半天）。c 给请求参数加一个版本键进 `versions`：部署时在跑的 Run 会被挡成 blocked，F3 验收的等价证明只容一个变化键且已被占用，等于也要重跑 | **a**。2026-10-26 的裁决针对的是模型**看到**的字节；`response_format` 不是模型可见文字，先例上输出上限从旧值改到 65536 也没有作废 F3 证据（**未确认**：没有逐条核对那次改动与 F3 证据的先后）。PR-R 本身按 AGENTS 要附一次有界真实 Run，足以证明新请求形状下主路径可用 | F3 `passes`、证据说明 | |

## 2. 可逆默认（Agent 自定，各写一行理由）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 |
|---|---|---|---|---|
| R1 | 「每轮」包不包括压缩请求 | a 不包括，压缩照旧不开；b 也开 | **a**。压缩指令要求「纯文本、不要 JSON」（`context.py:191`），摘要也不是 JSON；开了以后按供应商文档会被替换成空白，直接 `COMPACTION_FAILED`。压缩请求本来就不带工具。U7 说的是调查轮（工具轮+终答轮）。知识草稿的调用本来就开着（`opspilot/knowledge/worker.py:327`），不动 | `loop.py:809` 一处 |
| R2 | 怎么改 | a `loop.py:809` 改成恒为真，`ModelCall.json_mode` 字段保留；b 删掉字段 | **a**。评测录制文件里存着带 `json_mode` 的 `ModelCall`（`recording.py:1035`），删字段会让旧录制解不开；压缩和脚本还要传 False | 一行 |
| R3 | 要不要给请求形状加版本 | a 不加，`prompt_revision` 等三个版本都不变；b 加版本键（见 G1 的 c） | **a**。C3 §5 规定进 `versions` 的是模板字节和工具合同（`technical-proposal:176-205`），请求参数不在其列；重建只比消息哈希，新旧代码写的步骤行互相都能重建。区分新旧 Run 的办法：步骤行 `request_sha256`、trace 的 `opspilot.model.json_mode` | 无 |
| R4 | 部署时在跑的 Run | — | 不受挡：版本没变，被新代码领取后按消息哈希重建通过，后面的轮次带 `response_format` 发出。代价是缓存断一次（§5）。回滚同理安全 | 部署说明写进 PR 正文 |
| R5 | 停止作答的回合返回空白，判什么 | a 沿用现有代码，不新增原因码；b 新增专门的原因码（要用户裁决） | **a**，现有路径已经满足 U7，实测见 §4：没有消息的 Run，非终答轮空白 → 一次强制终答重试（提示里写 `EMPTY_REPORT`，无明细）→ 再空白 `failed(EMPTY_REPORT)`；预留的最后一轮空白 → `failed(EMPTY_REPORT)`。续接 Run（`report_optional`/`reply_only`）不重试：之前的步骤没有有效回复 → `failed(RESPONSE_INVALID)`；之前工具轮已有有效回复 → `answered`。实现上不用改代码，只补测试 | 无代码改动 |
| R6 | 工具轮内容是空白 | — | 不算失败，照常执行工具。空白原样存进 `assistant` 消息并在后续请求里回放（重放哈希要求字节不变，不能清掉）。实测 232 个空格的工具轮后正常完成 | 无 |
| R7 | 工具轮内容恰好是一个 JSON 对象（`{"note","replies"}`，或整个 `m0-response-v1` 包装）还解析吗；回复说明模板改不改 | a 不动：`tool_round_visible` 照旧，带 `note`/`replies` 键就取回复；模板一个字节不改；b 删掉工具轮回复形式并改模板 | **a**。P1 合同 C1 的形式本身就是一个 JSON 对象，和 JSON 模式不冲突，反而只有这种形式还能出现；模板原文「you may write your visible content as one json object」仍然成立（`reports.py:1359-1362`）。改模板字节会让发过它的在跑 Run 重建失败（哈希记在步骤行，`context.py:1497-1502`）。实测：工具轮里的包装或 note 形式回复仍被记下，之后空白收尾得到 `answered`。用户已接受工具轮基本不会有说明 | 无 |
| R8 | 工具轮生成一长串空白直到撞上输出上限（供应商文档提到的 JSON 模式风险；实测 15 个工具轮最多 232 个空格，没撞上限） | a 沿用现有判定：带工具调用且 `finish_reason=length` → `failed(OUTPUT_LENGTH)`，不重试（`loop.py:842-847`）；b 加重试，属于新行为 | **a**，并在冒烟和首个小批里统计工具轮 `completion_tokens` 和内容空白长度；出现一次再开 issue | 观察项 |
| R9 | 文档改哪几处（都只记录已裁决内容，走文档类预授权，不是用户门文件） | — | ① `docs/design/deepseek-flash-prompt-tool-reference.md:120` 表格行改为「官方未文档化；本项目 2026-11-17 实测 24 次同时发送 0 报错」，`:124` 改为「每个调查轮同时发 `tools` 与 `response_format`；压缩请求不发」，`:308` 清单项同步；`:109-112` 补一句「内容不是 JSON 时返回空白、`finish_reason=stop`，按 `EMPTY_REPORT` 处理」。② `docs/tasks/2026-11-17-workbench-redesign-contract.md:100`（C1 现状栏「工具轮不是 JSON 模式」）加一句带日期的注：#686 起每轮都开，C1 的裁决不变；`:108`（C9）加注：工具轮可见内容实际为空白，日志只显示工具名和耗时（C9 已写的兜底）。③ `docs/tasks/2026-09-28-m1-01-loop-limits.md:23` 可选加注。C3、SPEC、PRD、ADR、PRODUCT-CONSTRAINTS、feature_list 不用改（全文检索无旧规则） | 文档 |
| R10 | 估计器要不要补算 `response_format` | a 不补；b 补 | **a**。约 40 字节，由每 Run 的校准系数吸收；补了会改 `estimated_prompt_tokens` 的记录值，没有收益 | 无 |

## 3. r0 / 增量表各行复核

| 行 | 现状 | 说明 |
|---|---|---|
| D1、D2/D2'、D3/D3'、D4/D4'、D5/D5'（`output_unwrapped` 键）、U1、U4、U6、N1、N2、N3 | **作废** | 都是去壳的边界、位置、记录或后果。#456 的围栏反馈验收和 #453/#456 单测一项都不用改（探针：全部通过）；带围栏的回复照旧拒收，提示字节与 main 一致 |
| U5 | 已裁决 a，**但不再必需** | `tests/test_m1_alignment_c_contract.py:455` 之所以要改是因为去壳后围栏样例不再是坏 JSON；去壳取消后该测试原样通过（探针）。改不改都不影响合同，由 lead 记为「无需执行」或照裁决换样例，断言不变 |
| U2 | 有效 | `ClaimV2.evidence_ids` 默认空列表，fact 类缺键由 `FACT_SCOPE_REQUIRED` 拒收。包装里的 `report` 成员走同一条校验 |
| U3 | 有效 | `next_steps` 默认空列表，`gaps` 不放宽 |
| D6/D6' | 有效 | 每个出错引用给唯一最近的合法 id，`missing_target_refs` 列允许值；数据只来自 `state.delivered` 和投影后上下文。只在没有消息的 Run 里发出（其余模式不重试）。续接 Run 的 `delivered` 是否含继承视图：**未确认** |
| D7 | 有效 | 不做逐条 claim 合并报告 |
| D8/D8' | 有效 | L2、`REPORT_RETRY_TEMPLATE`、`RUN_COVERAGE_TEMPLATE`、`FINAL_REPORT_INSTRUCTION`、`REPLY_INSTRUCTION_TEMPLATE` 字节全不动，`prompt_revision` 不变。代价照旧：停在强制终答、上一轮回复属于 U2/U3 新接受或 D6 措辞变了的在跑 Run，重建时哈希对不上，以 `INCOMPATIBLE_STATE` 关闭（C3 §7 设计内路径） |
| D9 | 有效，范围缩小 | 实现完成后由非实现者对 F3 第 5 步 run-1/run-2 的 DB 提取跑 `rebuild_transcript`。只有 U2/U3/D6 会影响强制终答行的反馈字节；JSON 模式本身不影响。两次 Run 的 ledger 本机仍没有，**未确认** |
| N4 | 有效 | 回复缺 `evidence_ids` 不放宽 |

## 4. 探针

**环境**：`sysctl vm.swapusage` 运行前 used 12821 MB / free 490.69 MB，中途 free 498.69 MB，低于 500 MB，所以**没有跑整个 `tests/acceptance`**，只跑三批定向文件。`git archive aa86233d` 解到 worktree 的 `tmp/probe-686-r1/`（已忽略），在副本里改、在副本里跑（确认 `opspilot.__file__` 指向副本），跑完已删。worktree `git status --porcelain` 为空。

**草稿补丁**：`loop.py:809` `json_mode=final` → `json_mode=True`，仅此一行。空白内容失败不用补丁，现有代码已是。

**结果：1521 项，2 failed、1469 passed、50 skipped。**

- 批 1（286 项，2 failed / 284 passed）：`tests/test_m1_investigation_loop.py`、`test_m1_investigation_compaction.py`、`test_m1_view_explicit_contract.py`、`test_m1_loop_limits_contract.py`、`test_m1_investigation_client.py`、`test_tracing.py`、`test_m1_investigation_context.py`、`test_m1_investigation_continuation.py`。
- 批 2（326 项，318 passed / 8 skipped）：`tests/acceptance/test_456_forced_final_fence_feedback.py`、`tests/test_453_report_retry_feedback.py`、`tests/test_456_forced_final_feedback.py`、`tests/acceptance/test_m1_01_acceptance.py`、`test_workbench_conversation.py`、`test_304_forced_final_rebuild.py`、`tests/test_304_forced_final_rebuild_impl.py`、`test_m1_conclusion_publishable.py`、`test_m1_report_contract_citations.py`、`test_m1_alignment_c_contract.py`、`test_m1_upstream_alignment_b_contract.py`、`tests/acceptance/test_f3_step5_real_runs.py`、`test_f3_step5_rubric_gates.py`、`tests/test_lab_evidence.py`、`tests/acceptance/test_646_report_root_cause.py`、`tests/unit/test_646_report_root_cause_impl.py`、`tests/acceptance/test_m1_live_flash_replay.py`、`tests/test_f13_call_diagnostics_contract.py`。8 项跳过全是 `test_workbench_conversation.py`，原文 `explicit PG opt-in required: M1_DURABLE_POSTGRES=1`。
- 批 3（909 项，867 passed / 42 skipped）：`tests/test_a7_main_path_impl.py`、`test_a7_main_path.py`、`test_tool_expansion_a7_contract.py`、`test_instruction_discipline.py`、`test_f3_surface_equivalence.py`、`test_f3_competing_scenario_impl.py`、`test_f3_pr1_acceptance.py`、`test_f3_pr2_coverage_impl.py`、`tests/acceptance/test_309_loop_dedupe_parallel.py`、`test_eval_c_recording.py`、`test_eval_b2_replay.py`、`test_eval_d_structural.py`、`test_eval_c_product_seams.py`、`test_eval_c_live_driver.py`、`test_f6_recovery.py`、`tests/test_m1_04_alert_context_impl.py`、`test_386_logs_impl.py`、`test_385_upstream_issues_impl.py`、`test_m1_takeover_evidence_script.py`、`tests/acceptance/test_386_structured_logs.py`。输出尾部可见的跳过都是 `test_f6_recovery.py` 的 PG 门控；42 项跳过原因没有逐条核对。

失败清单原样：

```
FAILED tests/test_m1_investigation_loop.py::test_exploration_retry_does_not_consume_the_final_slot
FAILED tests/test_m1_investigation_loop.py::test_last_request_is_reserved_for_the_report_and_sends_no_tools
2 failed, 284 passed in 4.19s
```

- 两项都断言 `model.calls[0].json_mode is False`（`tests/test_m1_investigation_loop.py:1080`、`:1761`），直接钉着旧规则；按 U7 把这两行翻成 True，其余断言（终答轮不带工具、`FINAL_REPORT_INSTRUCTION` 位置）不动。
- 不变的钉值：压缩请求 `json_mode is False`（`tests/test_m1_investigation_compaction.py:143`）按 R1 继续成立；`:454`、`test_m1_view_explicit_contract.py:154`、`test_m1_investigation_loop.py:1081/1763/1794`（终答轮为 True）继续成立。
- 证据型验收全部通过：F3 第 5 步真实 Run（只比三个版本）、#304 重建、#456 围栏反馈与 `prompt_revision` 不变、A7、评测录制/重放（旧录制自带 `json_mode=False` 的 call）、`test_instruction_discipline.py`。
- 共享路径实测（脚本 `/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/0acde4fb-8ea6-4e87-840e-a0bac99a2504/scratchpad/r3/probe686_r1_blank.py`，`assemble` + 脚本化模型，草稿补丁下；空白取 `""`、3 个空格、232 个空格、`None` 四种，结果相同）：

```
非终答轮空白 → 再空白        report_required  failed (EMPTY_REPORT)      请求 2 次，json [True, True]，tools [有, 无]
预留终答轮空白               report_required  failed (EMPTY_REPORT)
空白收尾                     report_optional  failed (RESPONSE_INVALID)  请求 1 次，不重试
空白收尾                     reply_only       failed (RESPONSE_INVALID)  请求 1 次
工具轮内容 232 空格 → 报告   report_required  completed
工具轮内容是包装 → 空白收尾  report_optional  answered
工具轮内容是 note 形式 → 空白收尾  report_optional  answered
工具轮内容空白 → 空白收尾    report_optional  failed (RESPONSE_INVALID)
重试提示原文：The previous reply was not accepted as the final report (EMPTY_REPORT). This is the forced final retry: …
```

**没有跑的**：整个 `tests/acceptance` 全量（内存）、其余未列出的 `tests/` 文件、全部 PG 门控项（含 P1 的 8 个场景验收和 `test_f6_recovery.py`）、任何 live 类。按读代码，PG 门控的循环路径和内存版相同，预期不受影响，**未确认**。

## 5. 费用、延迟、缓存

- 实测（`json-mode-probe.md` §3–4）：停止作答 JSON 开 41–75 s、关 49–63 s，看不出差别；工具轮 1.5–31 s。
- 缓存：同一组消息第一次带 `response_format` 发出只命中约 12k token（对照 168k/168k）；原样重发两次命中 31k/31k、131k/131k。推断 `response_format` 是缓存身份的一部分：每轮都开时前缀一致，缓存照常累积；只有切换才断。所以新 Run 无损失，部署时在跑的 Run 断一次（推断，**未确认**）。
- 终答轮现在的缓存命中中位数只有 2.3%（去掉了工具）；每轮都开以后终答轮会不会改善，**未确认**。
- 上下文预算：`response_format` 不进估计器（R10）；工具轮的空白内容进后续请求，实测每轮最多 232 个空格，可忽略。体积闸按真实字节算，已包含。

## 6. 必答类别

1. 时间：不涉及。已定：超时、截止时间不变（`loop.py:796` `_remaining_timeout`）。
2. 计数与口径：模型请求数、重试次数规则不变。run.md 的「首答重试」口径不变，数值预期下降（格式类失败减少），与 1116/1117 可直接比；进度说明条数会降到接近 0（已接受）。
3. 状态转移：不新增状态或原因码（R5、R8）。空白内容的转移见 §4。
4. 主体与权限：不涉及，请求仍由 worker 用同一把模型凭据发出，无新数据流出。
5. 数据合同：无迁移、无新键。步骤行 `request_sha256` 的值变化（无人回读）。部署顺序无要求（R4）。
6. 旧数据与已归档证据：旧 ledger 按消息哈希重建不受影响；评测录制自带旧 call；F3 证据见 G1、D9。停在强制终答的在跑 Run 受 U2/U3/D6 影响，见 D8。
7. 并发与水位：不涉及。
8. 外部调用：同一模型、同一端点，多一个请求参数；24 次同时带 `tools` 与 `response_format` 0 报错。失败后的状态见 R5、R8。供应商要求提示词里有 "json" 和示例：系统提示里的报告合同两者都有（`reports.py:69-113`），每轮都在。
9. 验收投影与 `passes`：`IncidentOutcome` 投影不变；没有验收测试变红；`passes` 不翻。F3 证据的效力是 G1。PR-R 触碰调查 loop，按 AGENTS 要附一次有界真实 Run 的 trace 链接和 `summary.json`。

## 7. 合同与验收测试应断言什么

1. 多轮 Run 里每个调查轮的请求都带 `response_format={"type":"json_object"}`：工具轮同时带 `tools`，终答轮与强制终答轮不带 `tools`；压缩请求不带。
2. 空白（`None`、空串、纯空白）收尾：§4 表里的五种结局，原因码逐一对上；重试提示含 `EMPTY_REPORT`。
3. 工具轮内容是空白时：工具照常执行，Run 能完成；重建后该步骤的消息哈希对得上。
4. 旧代码写的步骤行（工具轮没开 JSON 模式）在新代码下重建通过，Run 能继续。
5. `prompt_revision`、`context_policy_revision`、`tool_schema_revision` 不变。
6. 带围栏或前置文字的回复照旧拒收（去壳已取消）：沿用 #456 现有验收，不新增。
7. U2/U3/D6 的断言沿用 r0 §4 第 5、6 条。

不该测：`ModelCall` 字段名以外的内部实现、D6 新措辞的完整字节。

## 8. 未确认

- 整个 `tests/acceptance` 全量、PG 门控、live 类没有跑。
- F3 第 5 步 run-1/run-2 的 ledger 不在本机，D9 的重建核对没做。
- 输出上限改动没有使 F3 证据作废这一先例，没有逐条核对时间线。
- 续接 Run 的 `delivered` 是否含继承视图（D6 数据源）。
- 缓存身份包含 `response_format` 是推断；终答轮缓存是否改善没测。
- 工具轮空白撞输出上限的概率（15 个样本 0 次）；`report_required` 带消息、长 Run、调完工具后最后一轮给包装的真实表现（`json-mode-probe.md` §5）。
- 批 3 的 42 项跳过原因没有逐条核对。
