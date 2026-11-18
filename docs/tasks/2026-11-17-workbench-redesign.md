# 工作台重设计：对标 AWS DevOps Agent

- 状态：合同 r7 已定，P1 已合并（#698），P2 实施中
- 更新日期：2026-11-17
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

## 合同（r7）

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
| U1–U3 | 首个 Run 无报告时显示「已收集的证据」和「最近一步」；外观以原型 v2 为准、系统字体、不加载外部资源；回复不用展开就能看到且在 Messages 筛选下可见 |
| O1–O4 | 复盘草稿过期保持现状；`follow_up`/`correct` HTTP 别名保留一版，存储层分支和 `m1_live_runner` 两个开关在 P2 删；反馈每个调查一份（照 AWS，取代 D25）；F7 第 5 步审计重建补输入表来源 |
| T | 整个重设计最多 8 个场景测试（`tests/acceptance/test_workbench_conversation.py`）+ 3 条浏览器路径，一个共用驱动；不测清单与旧测试删除清单见决定表第五节 |

修订：r1 = 预审全部推荐（2026-11-17）；r2 = R1 的 D7a 判定往回找（PR #694 机器人发现，同日）；r3 = 收下与接力分成两个判定（同上）；r4 = A11 续接 Run 活跃时人工控制可用（同上）；r5 = A11 补 close 路径（同上）；r6 = B8 续接 Run 带上之前的回复（同上）；r7 = A3 补「无回复 + 不完整报告」走不完整交接（P1 独立审查）；r8 = C4 `reply_only` 最终轮不发最终报告指令（P2 真实 Run 1 中模型按最终报告指令返回裸报告，判 `RESPONSE_INVALID`）。

## PR 顺序

P1 回复合同 → P2 消息输入与接力 → P3 日志投影与 SSE → P4 页面（可与 P1 并行，先用现有数据）→ P7 Chat 面板 → P5 反馈 → P6 关联展示。每个 PR 直接对 `main`，不叠 PR。P1、P2 各附一次有界真实 Run 的 trace 与 `summary.json`。

## 执行进展与证据

- 2026-11-17：合同预审完成（全新上下文 Fable，只读）。全量探针：推荐做法 6 个受影响文件 0 失败；改版本号做法 12 失败（含 3 条 F3 第 5 步真实 Run 证据）。PG 门控验收未跑，留给 P1/P2 CI。

- P1 回复合同（`feature/workbench-p1-response-contract`，worktree `../poa-wb-p1`，对齐 r7）：实现 Opus；独立测试 Codex gpt-6.1-sol（`tests/acceptance/test_workbench_conversation.py` 5 个函数 8 例 + `tests/workbench_driver.py`，在 main 上 7 红 1 绿，原因均为功能缺失）；独立审查 Codex gpt-6-astra 三轮：首审 3×P2（1 项裁为合同澄清 r7，2 项修复），复验 1×P2（未收尾的 answered 步骤 + 超时清扫，Run 行优先）已修，lead 自核 delta。全新 PG：场景 + m1-01 验收 41 通过；实现者跑 `tests/acceptance` 带 PG 全过。真实 Run 证据 `docs/evidence/workbench-p1-response-live/`：Run 1 `RESPONSE_INVALID`（空回复，保留），Run 2 续接回答「你好」结局 `answered`、结论与代际不变；费用上限约 0.183 CNY（3 次尝试）。未覆盖：已发布结论上的 D3 真实 Run、`reply_only` 真实 Run（fixture 给不出完整首份报告）。

- P2 消息输入与接力（`feature/workbench-p2-message-input`，worktree `../poa-wb-p2`，对齐 r7）：实现 Opus；独立测试 Codex（`test/workbench-p2-scenarios` 995e2e91，5 个函数 15 例，断言未改）。行为：`message` 输入（不推进代际、不写控制审计）、web 进程接力开同代际续接 Run（请求内一次 + 后台循环兜底，`uuid5(":relay:{最大未读序号}")`）、A2/A7/A10/A11、后继输入写 `conversation`（只在事故有消息时）与此前通过校验的回复（B8）、Re-investigate、`follow_up`/`correct` 改为别名、F7 第 5 步审计读 `message` 输入（lead 裁决改两处断言来源）。文档：C3 §10 与 §4 一句、PRODUCT-CONSTRAINTS 第 22 行 D20 注释、ADR-0005 补注。不改授权、不迁移（已用运行时角色实测接力写入）。合同驱动的测试改动（lead 2026-11-17 裁决，断言强度不变）：F7 第 5 步人工纠正改读 `message` 输入（O4）；F7 第 1 步动作集加 `message`、`reinvestigate`（B1/O2/D11）；F13 草稿断言 `message`，辅助函数改为 reopen 后 Re-investigate（D6/D11）；F2 第 4 步风暴、`f2_b2_impl`、M1-01 交接停放、运行时角色测试把再排队从 `follow_up` 改为 `resume`（D7b）；F3 第 5 步 `scenario_shape_problems` 加 `expected_generation`，产品路径断言 0（A1/B3）。冻结的 F3 真实 Run 证据早于 P2，仍按代际 1 校验；以后再跑 F3 真实 Run 会记 0，本次不重跑。另有两个发布 fixture 补写结论步骤（A7）、两个文件里只用于再排队的无内容 `follow_up` 换成 `resume`。删除的用例按文件、用例名和取代它的合同条款列在提交信息里（40 个文件，含整文件删除 `test_m1_timeout_followup_postgres.py`、`test_f12_available_actions.py`、`test_245_rebind_release_impl_postgres.py`）。验证：场景文件 24 例全过；改动与删除涉及的文件逐个定向跑过；全量 PG 门控交给 CI。真实 Run：命令已交给用户运行。

## 下一步与交接

- 开 P1 worktree；验收测试作者与实现者分离。
- 旧 layout-B 分支 `feature/workbench-detail-b`（锁定的 worktree，3 个未推送提交）是否删除，待问用户。
