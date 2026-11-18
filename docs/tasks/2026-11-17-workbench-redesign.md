# 工作台重设计：对标 AWS DevOps Agent

- 状态：合同 r10 已定，P1–P4 已合并，P7 实施中
- 更新日期：2026-11-18
- 依据：[设计稿](../design/workbench-redesign-2026-11-17.md)、[合同决定表](2026-11-17-workbench-redesign-contract.md)、PRODUCT-CONSTRAINTS、C3 §5/§9/§10、ADR-0005；取代 [layout-B 规格](../design/web-ux-spec-2026-11-16.md) 的交互模型
- 工作区：本记录 `chore/workbench-redesign-record`；各 PR 的分支与 worktree 开工时补在下面「执行进展」

## 目标与范围

把事故页做成「结果在前的单页 + 可展开的调查日志 + 右侧常驻 Chat 面板」，交互照搬 AWS DevOps Agent：

- 调查线：给调查发的消息是**输入，不是控制动作**。不推进代际、不打断当前请求、不撤销恢复观察；调查在读到消息的那一步回复，回复写进日志。已出结论的调查收到消息后原地继续（续接 Run），页面显示 Investigating。
- Chat 面板：按事故记录回答，不启动也不触碰调查；用户的话属于「指挥调查」时，原句署用户名转进调查队列。
- 页面：原型 v2（结论 → 关键证据 → 假设 → 排除/未知 → 看不到的 / 下一步 → 调查日志 → 恢复观察 → 原始记录），每条证据和每个日志步骤都能展开看完整查询、来源、目标、时间窗、新鲜度、耗时和原始结果。
- 不变：只读红线（修复建议只能复制，没有 Apply）、人工控制优先、业务记录是恢复权威。

范围外：子 Agent、Fork、Unlink、Skip、升级到技术支持（见设计稿 §6）。

## 用户已定（2026-11-17）

- 照搬 AWS 已有文档的行为，不再问用户；只问 AWS 没覆盖或碰红线的。
- 放弃：layout-B 交互、黑白灰基线、§4.8 页面钉值测试、C3 §10「结论后追问开新 Run / 撤销观察 / REOPEN_REQUIRED」、每条消息推进代际。
- D10 观察期间的消息或报告修订不停观察；D19 不显示 `reasoning_content`；D20 消息（Chat 面板或调查输入框）不算 #184 的「人工处理」，只有控制动作算（用户明确「不算」）。
- 页面以原型 v2 为准；v1 标签页式原型被否。

## 合同（r10）

完整决定表（选项、代价、代码出处）见 [合同决定表](2026-11-17-workbench-redesign-contract.md)。预审需用户裁决 0 项；以下全部按推荐采纳，lead 2026-11-17 裁决。

| 编号 | 裁决要点 |
|---|---|
| A1–A2 | 续接 Run 成为当前 Run，由 web 接力事务指过去；worker 候选去掉 `conclusion IS NULL` |
| A3–A4 | 不加 Run 状态、不迁移；只回复 = Run `completed` + 结论步骤 `execution="answered"`，新增带栅栏的 `settle_answered`；回复合法报告不合格按只回复结束并记拒绝原因；都不合格交接 `RESPONSE_INVALID`；没有回复但报告合法只是自评不完整，走现有不完整交接（r7）；取代设计 D27 |
| A5 | 重启补发布条件改为「事故当前结论不等于这一行」 |
| A6 | 已恢复/已关闭上的续接 Run 为 `reply_only`：不给工具、1 次请求、报告丢弃 |
| A7 | 自动恢复观察判定看「发布当前结论的那个 Run」，不看当前 Run |
| A8–A9 | 页面与列表共用一条九级状态判定；续接期间显示 Investigating，结果区继续显示上一份报告 |
| A10 | 接力前把遗留的未结束不活跃 Run 写成 cancelled |
| A11 | 用户 2026-11-17 批准授权，用于 A11 r4「取消续接 Run 保留观察」：迁移 0025 给 `opspilot_web` 加 `opspilot_observation_sessions.UPDATE(subject_control_generation)`，取消时同一事务重绑会话代际并释放采样租约。续接 Run 活跃时 `rebind_target` 维持现状（state 写成 cancelled），不在 A11 范围。续接 Run 活跃时 pause/resume/cancel 照常作用在它上面；cancel 只取消续接 Run，结论、lifecycle、观察保留（r4）；有结论时 close 同样只取消续接 Run、不写 cancelled，关闭后仍可只回复（r5） |
| B1–B7 | 新输入类型 `message`；Run 输入 `scope_facts["conversation"]={floor, mode}`，新消息带 `new: true`；旧类型渲染字节不变；未读 = 序号大于所有 Run 已提交水位最大值；B8 后继输入带上此前通过校验的回复正文（不带 assistant 原文和 reasoning_content），对话跨 Run 连贯（r6） |
| C1–C3 | 工具轮回复写在可见 content 的 JSON `{note, replies}` 里，解析不出不算错；校验结果另存步骤行顶层字段，`assistant` 字节不动；回复只查证据编号存在，不认识的去掉并标 unsupported |
| C4–C5 | （r8，P2 真实 Run）`reply_only`：最终轮不发最终报告指令，A6 不给报告。回复说明作为独立尾随模板，只在有新消息的轮次发送；**不进 `prompt_revision`，只在步骤行记哈希**（做法③），P1 PR 给 C3 §5 加一句增补并在简报单列 |
| C6–C9 | 最终输出 `m0-response-v1` 包装，仍接受裸报告；最后一轮才读到的消息由最终输出回答；not answered 读取时计算；P1 不改纪律提示词 |
| D1–D12 | 状态表：调查中/暂停排队；有结论或观察中开续接；已恢复/已关闭只回复；**已取消、失败、超时禁用输入框，用 Re-investigate**（照 AWS，取代 D13）；**做完但自评不完整（D7a）可以接着聊**（AWS「completed」）；人工接管只记不查；无 Run、已合并拒绝；Re-investigate 在服务层连做取消 + 开新 Run |
| R1–R5 | （P2 审查，人工控制优先）人工取消或关闭之后，只有该决定之后收下的消息才触发接力，开出的 Run 仍读 floor 以上全部未读消息。接力条件里的 D7a 判定往回找本事故任一 Run 的通过校验报告，不只看当前 Run（r2）；收下消息按 D 组状态表判定，与接力判定分开（r3）；接力在 web 进程循环和发消息请求里共用一个函数执行，角色 `opspilot_web`，不改授权；Run 编号 `uuid5("{intake_key}:relay:{最大未读序号}")`；不写控制审计、不撤销观察、不改 lifecycle、不清结论 |
| E1–E7 | Chat 面板：两张新表（P7 迁移）；模型调用放 worker（密钥只在 worker）；只读本事故记录，不读 `reasoning_content`，人的话走 `project_input_content` 脱敏；`steer` 只转发用户原句；团队共享、随事故保留；P4 只做面板外壳 |
| P7-1–P7-18 | （r10）迁移 `0026_chat_panel`，一问一答一行、会话必须挂事故；上下文由 web 在提问时从页面投影按白名单构建并存进发言行；worker 线程 `opspilot-chat` 领取作答（无工具、json 模式、不存推理、不开 trace），池下限 capacity+4；web 转发 `steer` 的原句（`chat_forward`，幂等 `chat:{turn_id}`），worker 只写 `steer`；每人 10 分钟 20 问；History 本事故最近 50 个会话。**U-P7：用户 2026-11-18 批准方案 A**（两张新表的列级授权，降级删表并只报计数）。明细见决定表「P7」节 |
| U1–U3 | 首个 Run 无报告时显示「已收集的证据」和「最近一步」；外观以原型 v2 为准、系统字体、不加载外部资源；回复不用展开就能看到且在 Messages 筛选下可见 |
| O1–O4 | 复盘草稿过期保持现状；`follow_up`/`correct` HTTP 别名保留一版，存储层分支和 `m1_live_runner` 两个开关在 P2 删；反馈每个调查一份（照 AWS，取代 D25）；F7 第 5 步审计重建补输入表来源 |
| P3-1–P3-22 | （r9）一条日志覆盖全部 Run、全事故统一步骤计数、读取时计算 not answered 与 Retry、「Agent couldn't respond」、SSE 只作变化信号由 `/live` 片段就地更新、`message_received` 事件；不迁移、不改授权。明细见决定表「P3」节 |
| T | 整个重设计最多 11 个场景测试（r9）（`tests/acceptance/test_workbench_conversation.py`）+ 3 条浏览器路径，一个共用驱动；不测清单与旧测试删除清单见决定表第五节 |

修订：r1 = 预审全部推荐（2026-11-17）；r2 = R1 的 D7a 判定往回找（PR #694 机器人发现，同日）；r3 = 收下与接力分成两个判定（同上）；r4 = A11 续接 Run 活跃时人工控制可用（同上）；r5 = A11 补 close 路径（同上）；r6 = B8 续接 Run 带上之前的回复（同上）；r7 = A3 补「无回复 + 不完整报告」走不完整交接（P1 独立审查）；r8 = C4 `reply_only` 最终轮不发最终报告指令（P2 真实 Run 1 中模型按最终报告指令返回裸报告，判 `RESPONSE_INVALID`）；r9 = P3 预审表、E1 迁移号改 0026、场景函数上限改 11（lead 2026-11-18）；r10 = P7 预审表、U-P7 授权批准、E1/E3/E5 按 P7-1/2/3/4/9 修订（同日）。

## PR 顺序

P1 回复合同 → P2 消息输入与接力 → P3 日志投影与 SSE → P4 页面（可与 P1 并行，先用现有数据）→ P7 Chat 面板 → P5 反馈 → P6 关联展示。每个 PR 直接对 `main`，不叠 PR。P1、P2 各附一次有界真实 Run 的 trace 与 `summary.json`。

## 执行进展与证据

- P3 日志投影与实时更新（`feature/workbench-p3-timeline`，worktree `../poa-wb-p3`，对齐 r9）：实现 Opus；P3-20 的场景与浏览器断言由独立测试作者另加。行为：日志按 Run 画出事故全部 Run（起点按来源命名，步骤全事故连续编号，早期 Run 原始结果改走证据页）；消息状态 Queued → Read at step N → Answered / Not answered，not answered 可 Retry（复制原文、幂等 `retry:{seq}`）；`RESPONSE_INVALID` 显示「Agent couldn't respond」；页面收到任何 SSE 事件后拉 `/incidents/{id}/live` 就地更新，新条目追加并在读者不在末尾时显示「N new ↓」；发消息追加 `message_received` 事件。截图存 `~/.agent/diagrams/opspilot-workbench-p3-shots/`。
- P4 页面（`feature/workbench-p4-incident-pages`，worktree `../poa-wb-p4`）：实现 Opus；独立浏览器测试作者另起（路径 a、b，`tests/browser/test_workbench_paths.py`，断言未改，页面按定位器对齐）；独立审查两轮（首审 MERGEABLE、2 项 P2 已修：D7a 往回找、A8 与列表 SQL 分组一致性测试；复审 FIX-FIRST：D8 人工接管措辞优先、D12 未读消息「Not read」、D7a 报告带其 Run 的证据，均已修，每项一句断言，逐项去掉修复验证会红）。行为：列表与详情按原型 v2 重写；A8 一条状态函数（`opspilot/web/status.py`）页头与列表共用，列表分组 SQL 同步；结果在前（结论全文、关键证据、假设、排除/未知、看不到的、建议只可复制），调查日志每步每个工具调用可展开看查询、来源、目标、时间窗、新鲜度、耗时与脱敏结果，筛选「全部/问题/消息」与 Expand all；Chat 面板只做外壳（E7）；P2 合入后输入框发 `message`（按 P2 的 available_actions 决定可用，措辞按 D 表），D11 单一 Re-investigate，排队请求与回复读真实数据，续接 Run 后报告与证据取发布它的 Run（A9）。证据：定向页面文件 + 两条浏览器路径 + 场景文件在全新 PG 上 251 通过；ruff、mypy 干净；全量交 CI。截图（列表与 10 种详情状态、P2 三态、1440 与 400、深色）存仓库外 `~/.agent/diagrams/opspilot-workbench-p4-shots/`。删除（合同 §5 P4 清单）：整文件 7 个（`test_m1_web_workbench.py`、`test_572_incident_list_page.py`、`test_684_list_contract.py`、`test_684_incident_list_query.py`、`test_m1_list_run_state_postgres.py`、`test_f2_tool_timeout_page.py`、`test_m1_incident_control_contract.py`），部分用例（`test_m1_02_recovery_page.py` 3 个、`test_m1_report_charts_contract.py` 17 个、`test_m1_report_charts_robustness.py` 1 个、`test_m1_184_projection_impl.py` 3 个），另有约 12 个文件只删读旧页面文字的断言（逐个列在提交里）；`tests/m1_web_support.py` 未搬（98 个文件引用）。未做：P3 日志投影（多 Run 时间线、逐步 SSE）、O1–O4。
- 2026-11-17：合同预审完成（全新上下文 Fable，只读）。全量探针：推荐做法 6 个受影响文件 0 失败；改版本号做法 12 失败（含 3 条 F3 第 5 步真实 Run 证据）。PG 门控验收未跑，留给 P1/P2 CI。

- P1 回复合同（`feature/workbench-p1-response-contract`，worktree `../poa-wb-p1`，对齐 r7）：实现 Opus；独立测试 Codex gpt-6.1-sol（`tests/acceptance/test_workbench_conversation.py` 5 个函数 8 例 + `tests/workbench_driver.py`，在 main 上 7 红 1 绿，原因均为功能缺失）；独立审查 Codex gpt-6-astra 三轮：首审 3×P2（1 项裁为合同澄清 r7，2 项修复），复验 1×P2（未收尾的 answered 步骤 + 超时清扫，Run 行优先）已修，lead 自核 delta。全新 PG：场景 + m1-01 验收 41 通过；实现者跑 `tests/acceptance` 带 PG 全过。真实 Run 证据 `docs/evidence/workbench-p1-response-live/`：Run 1 `RESPONSE_INVALID`（空回复，保留），Run 2 续接回答「你好」结局 `answered`、结论与代际不变；费用上限约 0.183 CNY（3 次尝试）。未覆盖：已发布结论上的 D3 真实 Run、`reply_only` 真实 Run（fixture 给不出完整首份报告）。

- P2 消息输入与接力（`feature/workbench-p2-message-input`，worktree `../poa-wb-p2`，对齐 r7）：实现 Opus；独立测试 Codex（`test/workbench-p2-scenarios` 995e2e91，5 个函数 15 例，断言未改）。行为：`message` 输入（不推进代际、不写控制审计）、web 进程接力开同代际续接 Run（请求内一次 + 后台循环兜底，`uuid5(":relay:{最大未读序号}")`）、A2/A7/A10/A11、后继输入写 `conversation`（只在事故有消息时）与此前通过校验的回复（B8）、Re-investigate、`follow_up`/`correct` 改为别名、F7 第 5 步审计读 `message` 输入（lead 裁决改两处断言来源）。文档：C3 §10 与 §4 一句、PRODUCT-CONSTRAINTS 第 22 行 D20 注释、ADR-0005 补注。不改授权、不迁移（已用运行时角色实测接力写入）。合同驱动的测试改动（lead 2026-11-17 裁决，断言强度不变）：F7 第 5 步人工纠正改读 `message` 输入（O4）；F7 第 1 步动作集加 `message`、`reinvestigate`（B1/O2/D11）；F13 草稿断言 `message`，辅助函数改为 reopen 后 Re-investigate（D6/D11）；F2 第 4 步风暴、`f2_b2_impl`、M1-01 交接停放、运行时角色测试把再排队从 `follow_up` 改为 `resume`（D7b）；F3 第 5 步 `scenario_shape_problems` 加 `expected_generation`，产品路径断言 0（A1/B3）。冻结的 F3 真实 Run 证据早于 P2，仍按代际 1 校验；以后再跑 F3 真实 Run 会记 0，本次不重跑。另有两个发布 fixture 补写结论步骤（A7）、两个文件里只用于再排队的无内容 `follow_up` 换成 `resume`。删除的用例按文件、用例名和取代它的合同条款列在提交信息里（40 个文件，含整文件删除 `test_m1_timeout_followup_postgres.py`、`test_f12_available_actions.py`、`test_245_rebind_release_impl_postgres.py`）。验证：场景文件 24 例全过；改动与删除涉及的文件逐个定向跑过；全量 PG 门控交给 CI。真实 Run 1（用户运行，[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/98c56dd8-ac1f-4247-8db1-8be275d09273/r/00000000-0000-0000-ecf4-4d2a1591ee95?poll=true)）：首个 Run 发布（D3），「你好」续接 Run `answered`，结论、代际、lifecycle 不变；关闭后的 `reply_only` 续接 Run 交接 `RESPONSE_INVALID`——请求里同时带了最终报告指令和回复说明，二者矛盾，模型按最终报告指令返回裸报告；结论保留、lifecycle 仍为 closed。已在 90bf51bb 修复（合同 r8）。Run 1 的摘要与原始 ledger 只存仓库外 `~/.agent/handoffs/attachments/workbench-p2-ledgers/`，仓库只收重跑的 `summary.json`。真实 Run 重跑（[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/98c56dd8-ac1f-4247-8db1-8be275d09273/r/00000000-0000-0000-217a-8fd9119d7aad?poll=true)，证据 `docs/evidence/workbench-p2-message-live/`）：首个 Run 发布；消息后 `report_optional` 续接 Run 回复并发布修订报告（结论由续接 Run 的结论步骤发布，代际 0、lifecycle open）；关闭后 `reply_only` 续接 Run 0 个工具调用、`answered`，结论与 closed 不变。5 次 HTTP，费用上限 0.081 CNY。

- P7 Chat 面板（`feature/workbench-p7-chat`，worktree `../poa-wb-p7`，对齐 r10）：实现 Opus；场景函数与浏览器路径 (c) 由独立测试作者在另一分支编写。行为：右侧面板可提问、看回答（纯文本、引用证据可点）、History 切换会话、New chat；回答只来自页面上的记录，问答不改调查的输入、Run 与步骤；`steer` 时面板显示转发结果并链接到日志里那条「from Chat」消息。改的钉值：迁移头（P7-18 所列 11 处）、授权指纹、表数 44→46、0023/0021 降级测试把 0026 两表计入、池下限 +4（`test_427_outbox_impl`、`test_427_pool_compose`、`test_f2_b1_impl`、`test_f2_b1_capacity_priority_postgres`、`test_393_compose_static_impl`）。
- P7 真实模型检查（2026-11-18，用户跑 `~/.agent/p7run.sh`，PR #711 头 `b896be6b`）：DeepSeek 2 次调用均成功；提问得到回答且 `steer=false`、引用记录证据，调查的输入、Run、步骤不变；指挥调查的问题得到 `steer=true`，转发成输入 1，署提问人、`chat_forward`、正文等于原问题；模型文字不进输入，发言行不含推理。按 E4 不导出 trace。证据：[summary.json](../evidence/workbench-p7-chat-live/summary.json)。

## 下一步与交接

- 开 P1 worktree；验收测试作者与实现者分离。
- 旧 layout-B 分支 `feature/workbench-detail-b`（锁定的 worktree，3 个未推送提交）是否删除，待问用户。
