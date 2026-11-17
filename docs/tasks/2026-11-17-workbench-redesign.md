# 工作台重设计：对标 AWS DevOps Agent

- 状态：合同 r6 已定，P1 待开工
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

## 合同（r6）

完整决定表（选项、代价、代码出处）见 [合同决定表](2026-11-17-workbench-redesign-contract.md)。预审需用户裁决 0 项；以下全部按推荐采纳，lead 2026-11-17 裁决。

| 编号 | 裁决要点 |
|---|---|
| A1–A2 | 续接 Run 成为当前 Run，由 web 接力事务指过去；worker 候选去掉 `conclusion IS NULL` |
| A3–A4 | 不加 Run 状态、不迁移；只回复 = Run `completed` + 结论步骤 `execution="answered"`，新增带栅栏的 `settle_answered`；回复合法报告不合格按只回复结束并记拒绝原因；都不合格交接 `RESPONSE_INVALID`；取代设计 D27 |
| A5 | 重启补发布条件改为「事故当前结论不等于这一行」 |
| A6 | 已恢复/已关闭上的续接 Run 为 `reply_only`：不给工具、1 次请求、报告丢弃 |
| A7 | 自动恢复观察判定看「发布当前结论的那个 Run」，不看当前 Run |
| A8–A9 | 页面与列表共用一条九级状态判定；续接期间显示 Investigating，结果区继续显示上一份报告 |
| A10 | 接力前把遗留的未结束不活跃 Run 写成 cancelled |
| A11 | 续接 Run 活跃时 pause/resume/cancel 照常作用在它上面；cancel 只取消续接 Run，结论、lifecycle、观察保留（r4）；有结论时 close 同样只取消续接 Run、不写 cancelled，关闭后仍可只回复（r5） |
| B1–B7 | 新输入类型 `message`；Run 输入 `scope_facts["conversation"]={floor, mode}`，新消息带 `new: true`；旧类型渲染字节不变；未读 = 序号大于所有 Run 已提交水位最大值；B8 后继输入带上此前通过校验的回复正文（不带 assistant 原文和 reasoning_content），对话跨 Run 连贯（r6） |
| C1–C3 | 工具轮回复写在可见 content 的 JSON `{note, replies}` 里，解析不出不算错；校验结果另存步骤行顶层字段，`assistant` 字节不动；回复只查证据编号存在，不认识的去掉并标 unsupported |
| C4–C5 | 回复说明作为独立尾随模板，只在有新消息的轮次发送；**不进 `prompt_revision`，只在步骤行记哈希**（做法③），P1 PR 给 C3 §5 加一句增补并在简报单列 |
| C6–C9 | 最终输出 `m0-response-v1` 包装，仍接受裸报告；最后一轮才读到的消息由最终输出回答；not answered 读取时计算；P1 不改纪律提示词 |
| D1–D12 | 状态表：调查中/暂停排队；有结论或观察中开续接；已恢复/已关闭只回复；**已取消、失败、超时禁用输入框，用 Re-investigate**（照 AWS，取代 D13）；**做完但自评不完整（D7a）可以接着聊**（AWS「completed」）；人工接管只记不查；无 Run、已合并拒绝；Re-investigate 在服务层连做取消 + 开新 Run |
| R1–R5 | 接力条件里的 D7a 判定往回找本事故任一 Run 的通过校验报告，不只看当前 Run（r2）；收下消息按 D 组状态表判定，与接力判定分开（r3）；接力在 web 进程循环和发消息请求里共用一个函数执行，角色 `opspilot_web`，不改授权；Run 编号 `uuid5("{intake_key}:relay:{最大未读序号}")`；不写控制审计、不撤销观察、不改 lifecycle、不清结论 |
| E1–E7 | Chat 面板：两张新表（P7 迁移）；模型调用放 worker（密钥只在 worker）；只读本事故记录，不读 `reasoning_content`，人的话走 `project_input_content` 脱敏；`steer` 只转发用户原句；团队共享、随事故保留；P4 只做面板外壳 |
| U1–U3 | 首个 Run 无报告时显示「已收集的证据」和「最近一步」；外观以原型 v2 为准、系统字体、不加载外部资源；回复不用展开就能看到且在 Messages 筛选下可见 |
| O1–O4 | 复盘草稿过期保持现状；`follow_up`/`correct` HTTP 别名保留一版，存储层分支和 `m1_live_runner` 两个开关在 P2 删；反馈每个调查一份（照 AWS，取代 D25）；F7 第 5 步审计重建补输入表来源 |
| T | 整个重设计最多 8 个场景测试（`tests/acceptance/test_workbench_conversation.py`）+ 3 条浏览器路径，一个共用驱动；不测清单与旧测试删除清单见决定表第五节 |

修订：r1 = 预审全部推荐（2026-11-17）；r2 = R1 的 D7a 判定往回找（PR #694 机器人发现，同日）；r3 = 收下与接力分成两个判定（同上）；r4 = A11 续接 Run 活跃时人工控制可用（同上）；r5 = A11 补 close 路径（同上）；r6 = B8 续接 Run 带上之前的回复（同上）。

## PR 顺序

P1 回复合同 → P2 消息输入与接力 → P3 日志投影与 SSE → P4 页面（可与 P1 并行，先用现有数据）→ P7 Chat 面板 → P5 反馈 → P6 关联展示。每个 PR 直接对 `main`，不叠 PR。P1、P2 各附一次有界真实 Run 的 trace 与 `summary.json`。

## 执行进展与证据

- 2026-11-17：合同预审完成（全新上下文 Fable，只读）。全量探针：推荐做法 6 个受影响文件 0 失败；改版本号做法 12 失败（含 3 条 F3 第 5 步真实 Run 证据）。PG 门控验收未跑，留给 P1/P2 CI。

## 下一步与交接

- 开 P1 worktree；验收测试作者与实现者分离。
- 旧 layout-B 分支 `feature/workbench-detail-b`（锁定的 worktree，3 个未推送提交）是否删除，待问用户。
