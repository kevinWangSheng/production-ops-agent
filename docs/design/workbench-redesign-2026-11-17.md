> 状态说明（2026-11-17）：本稿是设计输入。以下几处已被用户决定或合同预审取代，以 [任务记录](../tasks/2026-11-17-workbench-redesign.md) 与 [合同](../tasks/2026-11-17-workbench-redesign-contract.md) 为准：详情页不用 Summary/Timeline 标签页，改为原型 v2 的结果在前单页加可展开的 Investigation log；回复在读到消息的那一步写进日志；Chat 面板要做（P7），不是「跳过」；D13 改为照 AWS（取消、失败、超时不能聊）；D25 改为每个调查一份反馈；D27 不做迁移；外观以原型 v2 为准、系统字体。

# OpsPilot 事故页重设计：对标 AWS DevOps Agent

日期 2026-11-17 · 状态：设计稿，未实现 · 取代 layout-B（web-ux-spec-2026-11-16）的交互模型

依据分三类，下文各处都标了出处：
- **已核查**：看过的代码，或 AWS 官方文档（2026-11-17 抓取）。
- **推断**：从已核查事实推出来的。
- **推荐**：本稿提出的方案。

## 0. 现状里已核查的病因

| 症状 | 代码事实 |
|---|---|
| 「你好」开出一个新 Run | `follow_up`/`correct` 都走 `incidents.control()`。每条消息推进 `control_generation`。如果 Run 已经出结论，就走续开（renew）：取消旧 Run、插入新 Run、`lifecycle='open'`、`conclusion=NULL`（`persistence/controls.py` 667-692） |
| 在恢复观察中发一条消息，观察就结束了 | 续开路径在同一事务里调用 `revoke_authorized_sessions`（controls.py 652-656）。页面上也直接写着 "Ends the current recovery watch"（截图 concluded-1440） |
| 回复是一份新报告的第一句 | 模型唯一的输出合同是 `ReportV2`（`investigation/reports.py` 168-196），里面没有回答人的字段 |
| 出现 INCOMPLETE_INVESTIGATION | 模型给出了合法报告，但 `assessment_status="incomplete"`（`loop.py` 573-577）。报告校验失败则记 `failed`。一句寒暄也被逼着交一份完整报告 |
| 看不到调查过程 | SSE 的 `step_committed` 只带 id、`finish_reason`、`planned_tools` 数量（`progress.py` 470-479）；`tool_committed` 只带 tool/status/evidence_id |
| 运行中发消息会打断当前请求 | follow_up/correct 会栅栏（fence）租约，Run 重新排队（controls.py 705-709、loop.py 646）。事件输入走 `append_input`，不栅栏，暂停时也能写入（controls.py 843-855） |

---

## 1. 值班人的操作路径

**列表页**
1. 打开 Incidents，默认停在 **Needs attention**（需要人处理）。每行一句话写清：状态、目标、已持续多久、Agent 当前在做什么（例如「Investigating · 6 checks」或「Root cause found · watching recovery 12/40」）。
2. 带「Linked」标记的行，表示同一目标上的告警已经并入主事故，点进去看主事故。
3. 手头有怀疑但没有告警时，点右上角 **Start investigation**：填目标、描述、可选的起始时间。

**详情页**
1. 先看 Summary 顶部：现在是什么状态、根因是否已确认、影响多大、谁在处理。
2. 想核对结论时，展开对应的 Finding，看证据片段，点「Open evidence」查原始查询和结果。
3. 想知道 Agent 怎么得出的结论，切到 Timeline，按时间顺序看它查了什么、每一步为什么这么查、哪一步失败了。
4. 有问题或有新信息时，点底部 **Chat with investigation**，直接打字。
   - 运行中：消息先进 Queued requests，Agent 在下一步之前读到它，然后在 Timeline 里回答。
   - 已完成：调查**原地继续**，Timeline 里出现「Resumed」，Agent 回答后视需要更新 Summary。
5. 处理动作都在页头：Take over、Mark remediated、Close、Reopen 等。Agent 给出的修复建议只能复制，没有执行按钮。
6. 调查结束后，在页头点 **Feedback** 评价：根因对不对、真实根因是什么。

---

## 2. 页面结构

### 2.1 列表页（桌面）
```
┌ OpsPilot  Incidents  Version changes  Suspension ─────── Read-only agent · demo ▾ ┐
│ Incidents                                              [ Start investigation ]   │
│ [Needs attention 3] [Investigating 2] [Watching recovery 1] [Resolved] [Closed] [All]│
│ 🔍 Filter by title, target, alert                                                │
│ ┌──────────────┬──────────────────────────────────┬──────────────┬────────┬─────┐│
│ │ Status       │ Incident                         │ Target       │ Opened │ Sev ││
│ ├──────────────┼──────────────────────────────────┼──────────────┼────────┼─────┤│
│ │ ● Needs you  │ Checkout p99 latency above 2s    │ checkout-prod│ 12m ago│ P2  ││
│ │              │ Agent stopped: deploy history not integrated · 2 linked       ││
│ │ ◐ Investig.  │ Checkout HTTP error ratio > 1%   │ checkout-prod│ 3m ago │ P1  ││
│ │              │ Checking pod restarts (step 6)                                ││
│ │ ◔ Watching   │ Cart 5xx spike                   │ cart-prod    │ 1h ago │ P3  ││
│ │              │ Root cause: OOMKilled · 12/40 healthy samples                 ││
│ └──────────────┴──────────────────────────────────┴──────────────┴────────┴─────┘│
└──────────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 列表页（约 400px）
表格改成卡片，标签页改成横向滚动的分段控件：
```
┌ ≡ OpsPilot        demo ┐
│ Incidents     [ + Start ]│
│ ‹Needs 3│Invest 2│Watch›│
│┌───────────────────────┐│
││● Needs you      P2 12m││
││Checkout p99 latency…  ││
││checkout-prod          ││
││Agent stopped: deploy… ││
│└───────────────────────┘│
```

### 2.3 详情页（桌面）
```
┌ Incidents / cd8f1359 ──────────────────────────────────────────────────────────┐
│ Checkout HTTP error ratio above 1%            [Take over][Mark remediated][Close][⋯]│
│ ◐ Investigating · P1 · checkout-prod · Alertmanager CheckoutErrorRatioHigh · 09:58 │
│ ─────────────────────────────────────────────────────────────────────────────── │
│  [ Summary ]  [ Timeline ]                                                      │
│ ┌──────────────────────────────────┐┌───────────────────────────────────────┐ │
│ │ Root cause  ✓ Confirmed           ││ Recovery                              │ │
│ │ payment-svc rollout v2.3 returns  ││ ◔ Watching · 12/40 healthy · since …  │ │
│ │ 500 on /charge (Deployment …)     ││ ▁▂▅█▇▅▂▁  error ratio                 │ │
│ │ Impact: 4.2% of checkout requests ││ Started automatically after RCA        │ │
│ ├──────────────────────────────────┤└───────────────────────────────────────┘ │
│ │ Current focus / Next steps        │┌───────────────────────────────────────┐ │
│ │ • Compare rollout time with …     ││ Suggested mitigation  · Advisory only  │ │
│ ├──────────────────────────────────┤│ Roll back payment-svc to v2.2          │ │
│ │ Findings (5)                      ││ Expected: error ratio < 1% in 5m       │ │
│ │ ▸ F1 Fact  Error ratio 4.2% …  E3 ││ OpsPilot will not run this. [Copy]     │ │
│ │ ▸ F2 Fact  Rollout at 09:41 …  E5 │└───────────────────────────────────────┘ │
│ │ ▸ H1 Hypothesis  Retry storm …    │┌ Could not see ────────────────────────┐ │
│ │ ▸ X1 Ruled out  Traffic surge …   ││ traces not integrated · deploy history │ │
│ └──────────────────────────────────┘└───────────────────────────────────────┘ │
│                                                                                 │
│ ───────────────────────────────── sticky bottom ─────────────────────────────── │
│ [ 💬 Chat with investigation ]                                                  │
└─────────────────────────────────────────────────────────────────────────────────┘
```

**Timeline 标签页加聊天框（桌面）。** 点 Chat 后自动切到 Timeline，输入框贴在底部：
```
│  [ Summary ]  [ Timeline ]                         Filter: [All ▾] [Expand all] │
│ 09:58  ⚡ Alert received · CheckoutErrorRatioHigh firing                        │
│ 09:58  ▶ Investigation started                                                  │
│ 09:58  ● Agent  "Checking checkout error ratio over the alert window first."    │
│          ├ metrics_range_query  rate(http_5xx…)[30m]   ✓ 1.2s   E1 · cited in F1│
│          └ k8s_get  deploy/payment-svc                 ✓ 0.4s   E2              │
│ 09:59  ● Agent  "Rollout at 09:41 lines up; checking pod logs."   thought 8s    │
│          └ logs_query  payment-svc error              ✗ timeout 30s  (no evidence)│
│ 10:01  ■ Report published · Root cause confirmed · 5 findings   [View diff]     │
│ 10:01  ◔ Recovery watch started (automatic)                                     │
│ 10:07  👤 demo  "Is it only checkout, or other services too?"                   │
│ 10:07  ↻ Investigation resumed                                                  │
│ 10:07  ● Agent  checks 2 services …                                             │
│ 10:08  💬 Agent → demo  "Only checkout: cart and frontend 5xx stay < 0.1% (E7, E8)."│
│          Report unchanged                                                       │
│ ── Queued requests (1) ─────────────────────────────────────────────────────── │
│  1. demo · 10:08 "Also check the payment DB connections"   waits for next step │
│ ┌────────────────────────────────────────────────────────────────────────────┐ │
│ │ Ask a question or add context…                                             │ │
│ └────────────────────────────────────────────────────────────────────────────┘ │
│  Send as demo · Enter to send, Shift+Enter for newline · 0/8192        [Send]   │
```

### 2.4 详情页（约 400px）
- 页头的处理按钮收进一个「Actions ▾」菜单。
- 两个标签页保留，都可以滚动。
- Chat 按钮固定在底部，加上 safe-area 间距。打开后输入框占满宽度，Timeline 里的工具行缩成一行。
```
┌ ‹ Incidents          ⋯ ┐
│Checkout HTTP error >1% │
│◐ Investigating · P1    │
│[Actions ▾]             │
│[Summary][Timeline]     │
│Root cause ✓ Confirmed  │
│payment-svc v2.3 …      │
│Recovery ◔ 12/40        │
│▸ F1 Fact …         E3  │
│▸ H1 Hypothesis …       │
│Suggested (advisory) …  │
│┌──────────────────────┐│
││💬 Chat with investig.││
│└──────────────────────┘│
```

### 2.5 页头按钮，按状态区分
- 主按钮最多两个，其余放进 ⋯ 菜单。
- 只要事故没有被合并，Chat 在**所有状态**下都可用。

| 状态 | 主按钮 | ⋯ 菜单 |
|---|---|---|
| Queued / Investigating | Pause, Take over | Cancel, Close, Change target, Merge |
| Paused（主体暂停或范围挂起） | Resume, Take over | Cancel, Close |
| Needs attention（交接 / 失败 / 超时 / 不完整） | Re-investigate, Take over | Mark remediated, Close, Change target, Merge |
| Root cause found（已结论，未观察） | Mark remediated, Take over | Re-investigate, Close, Change target, Merge |
| Watching recovery | Take over, Close | Pause watch（即 pause）, Change target |
| Human-owned | Mark remediated, Close | Resume automatic（resume） |
| Resolved | Feedback, Close | Reopen |
| Closed | Feedback, Reopen | — |
| Merged | 「Merged into X →」，Chat 关闭 | — |

---

## 3. 对话模型（推荐方案）

**核心变化：消息只是输入，不是控制动作。**

**新动作 `message`**
- 写入路径是 `append_input`，和告警事件同一条（已有、幂等、暂停时也能写）。
- `control_generation` 不推进，租约不栅栏，结论不清空，观察授权不撤销。
- `follow_up` 和 `correct` 合并成 `message`。旧端点保留一个版本作为别名，之后删掉。

**消息怎样被读到**
- 调查循环每轮开始时冻结输入水位（`begin_round` → `inputs_message`，loop.py 627-635，已有），所以运行中的消息自然在下一轮被读到，正在进行的模型请求不受影响。
- Queued requests 的定义：`input.seq > 当前 Run 已提交步骤的最大 input_watermark` 的人类消息。下一轮冻结水位后，消息离开队列，在 Timeline 里显示为「read at step N」。

**接力规则（一条，覆盖所有空闲状态）**
- 触发条件：事故有未读的人类消息，且没有活跃 Run（`queued` / `running` / `paused` 之外）。
- 动作：调度器开一个**续接 Run**。
  - Run 的 id 用 `uuid5(incident, max_unread_seq)` 生成，保证幂等。
  - 输入沿用 `_successor_input` 和 continuation（context.py 1640-1715），由它把旧报告和证据带过去。
  - 不推进代际，也不改 lifecycle。
- 页面上把它渲染成「Investigation resumed」，Run 编号只在悬停时显示。
- 最后一轮冻结之后才到的消息，在 Run 结束时触发同一条规则，不会丢。

**各状态下的行为**

| 状态 | 发消息的效果 |
|---|---|
| running / queued | 进队列，在下一轮读到，最终回复里回答 |
| paused / 挂起中 | 进队列，标注「Paused — read after resume」。不启动 Run（人工控制优先） |
| 已结论 / 交接 / 失败 / 超时 | 开续接 Run。lifecycle 不变（open 仍然是 open） |
| watching recovery | 开续接 Run，**观察继续**。回复或修订报告都不撤销观察授权，也不重新授权 |
| human-owned | 开续接 Run（人主动要求的，不算自动调查）。所有权不变，自动调查仍然关闭 |
| resolved / closed | 开续接 Run，但**只允许回复**（`report` 必须为 null），lifecycle 不变。想重新调查用 Reopen |
| merged | 输入框禁用，链接到主事故 |

**直接回复的合同（推荐）**
- 最终输出从 `ReportV2` 改成 `m0-response-v1`：
  ```json
  {"schema_version":"m0-response-v1",
   "replies":[{"to_input_seq":17,"text":"…","evidence_ids":["E7"]}],
   "report": <ReportV2> | null}
  ```
- `report` 何时必填：
  - 告警触发的首个 Run（没有可沿用的报告），以及 Re-investigate，都必须给 `report`。
  - 续接 Run 中，`report: null` 表示「报告不变」，Run 以新的执行结果 `answered` 结束，事故的 `conclusion` 保持原样。
- `replies` 怎么校验：
  - `evidence_ids` 只能引用本 Run 已交付或沿用的证据（复用现有引用检查）。
  - 不要求 fact 级 scope。如果文本里写了事实性断言，必须带 evidence_ids，否则 UI 标成「unsupported」。
  - 本 Run 读到的每条消息都应该有 reply；漏掉的在 Timeline 里标「not answered」。
- 降级规则（解决 INCOMPLETE 问题）：
  - 续接 Run 的 `replies` 合法、`report` 校验失败：保留回复，保留上一份报告，Timeline 里记一条「Report update rejected: <原因>」。**事故状态不降级。**
  - `replies` 和 `report` 都不合法：Run 交接，原因 `RESPONSE_INVALID`。Timeline 显示「Agent couldn't respond · [Retry]」，Retry 等同于再发一次同样的消息。
- 「你好」的成本：模型拿到「旧报告 + 沿用证据 + 消息」，一轮无工具调用就返回 `{"replies":[…],"report":null}`。一次模型调用，结论不动，观察不动。
- 中途叙述：提示词要求模型在每个工具轮先写一句**可见的** `content`（「我在查 X，因为 Y」）。这句话进 Timeline 的 Agent 条目。

**纠正与追问合并的理由**
- 用户分不清两者。
- 模型侧已经有「later human corrections take precedence」（context.py 1689）。
- 合并后由模型决定是否修订报告，修订会在 Timeline 显示为带 diff 的「Report updated」。

**代际、Run、恢复观察**
- 推进代际的只剩真正的控制动作：pause、resume、cancel、takeover、close、reopen、rebind、merge、register_remediation、Re-investigate（`new_run`）。
- Run 仍然是预算、租约、栅栏和审计的单位，只是对用户隐藏成「resumed」分段。
- 恢复观察只受控制动作影响。

**否决的备选**
- 同一个 Run 复活回 running：违反 ADR-0005 的终态语义，租约和预算不好算。
- 保留每条消息推进代际：就是现在的问题本身。
- 调查的回复走单独的轻量聊天模型或会话：回复拿不到调查上下文和证据，变成两套真相。（2026-11-18 P7 修订：否决的是「用聊天代替调查回复」。另做的右侧 Chat 面板只按事故记录回答、不启动也不改调查，指挥调查时把用户原句转进调查队列，回复仍由调查写进日志，见合同 E1–E7 与 P7 节。）
- 给 `ReportV2` 加可选的 `reply` 字段：寒暄仍然要交整份报告，INCOMPLETE 的问题还在。
- 保留 correct 单选框：没有用户价值，和现状一样。

---

## 4. Timeline 条目

| 条目 | 显示内容 | 数据来源 |
|---|---|---|
| Alert / 事件 | 告警名、firing 或 resolved、re-fired、linked | intake 记录、inputs（actor=event） |
| Investigation started / resumed / re-investigated | 触发原因（告警、某条消息、Re-investigate），预算上限 | runs 行和对应的 input 或 control |
| Agent 步骤 | 可见 `content` 一句、「thought Ns」（只用时长）、本轮读到的消息 | step 行 `assistant.content`、步骤时间、`input_watermark` |
| 工具调用（嵌在步骤下） | 工具名、参数摘要（查询表达式或资源）、✓/✗ 和错误码、耗时、证据链接、「cited in F2」 | step 的 `assistant.tool_calls[].arguments`，tool 行的 `status`/`query`/`evidence_id`，报告 claims |
| 上下文压缩 | 「Context compacted · evidence kept」 | step 的 `context.segment` 变化 |
| Report published / updated / rejected | 结论等级、根因、和上一版的 diff，或拒绝原因 | conclusion 步骤、`report_content`、校验原因 |
| 人类消息 | 作者、时间、全文、状态（queued → read at step N） | inputs（actor=web 用户） |
| Agent 回复 | 回复对象、正文、证据链接，「Report unchanged」或「updated」 | response 的 `replies` |
| 控制动作 | 「demo paused」「demo took over」等 | controls 审计行 |
| 恢复观察 | 开始（自动或登记）、每 N 个样本的进度、确认或结束原因 | observation sessions 和 samples |
| Run 结束 | 交接或失败用人话写原因，附 Retry 或 Re-investigate | 结论步骤的 execution/reasons |

**不显示 `reasoning_content`。**
- C3 §5 规定：「仅作为受限协议状态保存……不作为证据、复盘内容」。PRODUCT-CONSTRAINTS 的数据流合同也要求私有协议字段只回到同一 provider、同一 Run。
- 页面也算新的接收方，所以要显示必须由用户决定（见 D19）。
- 替代做法：可见 `content` 加时长。

**SSE 改动**（P3 预审 P3-14 取代原方案）
- SSE 只作「有变化」信号：worker 事件载荷不变；web 在收下消息后追加一种事件 `message_received {input_sequence}`（不含正文）。
- 页面收到任何事件后拉取 `GET /incidents/{id}/live`：同一登录、同一投影，服务端渲染页头、结果区、日志、Queued、输入框状态和恢复观察，就地替换。
- 不再给 `step_committed` 等加富字段（note、tools、duration_ms、agent_reply 等）：那会在 JS 里重复一遍投影。

---

## 5. Summary 标签页与报告字段

| 区块 | 字段 |
|---|---|
| 状态条 | lifecycle、mode/owner、当前 Run 状态、P 级（取告警 `severity` 标签，没有就不显示） |
| Root cause / 当前假设 | 有 `root_cause` 时显示 kind/ns/name 加证据，标「Confirmed」（`conclusion=supported`）。否则显示第一条 hypothesis，标「Hypothesis」 |
| Summary 段落 | `summary` |
| Current focus | 运行中显示最近一步的 `note` 和正在跑的工具；空闲时显示 `next_steps` |
| Findings | `claims` 分组：fact、hypothesis、counter_evidence、rejected_hypothesis，每条可展开看证据片段和链接 |
| Could not see | `gaps` 加 blind lines（已有 `_blind_lines`） |
| Key metrics | 现有 `charts.py`，展示被引用的指标证据 |
| Recovery | 观察会话、进度、样本 sparkline、授权来源（自动或登记） |
| Suggested mitigation | `claims[kind=recommendation]` 加 `next_steps`。标题固定写「Advisory only」，正文写「OpsPilot will not run this」，只有 Copy 按钮 |
| Investigation status | 结果分「completed」和「incomplete（原因）」两类，对应 PRODUCT-CONSTRAINTS 中「不完整」与「不确定」的区分 |

---

## 6. 和 AWS 的差异

| AWS 功能 | 处理 | 理由 |
|---|---|---|
| 运行中的消息排队，在步骤之间读取 | 采纳 | 现有水位机制直接支持 |
| 给已完成的调查发消息即恢复 | 采纳，并扩展到失败和交接状态 | 我们最常见的场景就是让不完整的调查接着做 |
| Summary / Timeline 双标签 | 采纳 | — |
| 子 Agent 嵌套 | 跳过 | 我们只有单个 loop，没有子 Agent；工具挂在步骤下面已经够用 |
| 从任意消息 Fork | 暂缓，以后可做（M） | 单团队场景价值低；Re-investigate 和 Change target 覆盖了主要需求 |
| 分诊：Linked / Unlink | 改造：显示关联和理由，只读 | merge 已经存在，但会关闭事故，Unlink 需要新的逆操作（M），先不做 |
| 分诊：Skipped / Unskip | 跳过 | 我们没有跳过机制，告警重复靠去重处理 |
| 结构化反馈（verdict、真实根因、steering、notes） | 采纳（S-M） | 进入 eval 和复盘草稿；不直接写知识库，知识仍要经过审核 |
| 内联修复提案与 Apply | 改造：只给建议，可复制，**没有 Apply** | 只读红线 |
| 优先级 | 改造：只读，取自告警标签 | 手动设置没有消费方 |
| 手动启动 | 采纳 | 已有 `/intake/ui`，列表页加入口 |
| 独立的 on-demand Chat | 部分采用（2026-11-18 P7） | 做事故页右侧的 Chat 面板：只按本事故记录回答、团队共享、指挥调查时转发用户原句；不做跨事故、不挂事故的独立 Chat（合同 P7-3） |
| 升级到技术支持 | 跳过 | 不适用 |

---

## 7. 后端改动与 PR 拆分

每个 PR 都直接以 `main` 为目标，按顺序合并，不叠 PR。

| PR | 内容 | 规模 | 取代的条文 |
|---|---|---|---|
| P1 `feat: response contract` | `m0-response-v1`：replies + 可空 report；新执行结果 `answered`（migration 加枚举值）；续接 Run 的降级规则；提示词要求可见 note。旧的 follow_up 路径也能直接受益 | M | reports.py 中「唯一输出为 ReportV2」的约定；ADR-0005 第 1 条补充：`answered` 不发布新结论 |
| P2 `feat: message input` | `message` 动作改走 append_input，不推进代际、不栅栏；调度器的未读接力规则；去掉消息路径上的 REOPEN_REQUIRED；resolved/closed 强制只回复；观察不撤销；follow_up/correct 作为别名 | L | C3 §10「结论发布后追问开新 Run…撤销观察…REOPEN_REQUIRED（F12 U1、r2）」；§4 中「追问」「纠正」两类输入改为一类；controls.py docstring 中的对应规则；#184 判定中「人工处理」的定义（见 D20） |
| P3 `feat: timeline projection + SSE` | timeline 读模型（全部 Run 一条日志）、`/live` 片段就地更新、`message_received` 事件 | M | 无：worker 事件载荷不变 |
| P4 `feat: incident pages` | 列表页和详情页重写（Summary、Timeline、Chat、Queued），视觉 tokens，400px 适配 | L | web-ux-spec-2026-11-16 全部交互；黑白灰基线；§4.8 的页面钉死测试 |
| P5 `feat: investigation feedback` | 反馈表、页面、导出给 eval 和复盘 | S-M | 无（新增） |
| P6 `feat: linked incidents display` | 关联关系与理由的只读展示 | S | 无 |

按 AGENTS.md，P1 和 P2 都触碰调查 loop，各需附一次有界真实 Run 的 trace 和 summary.json。P4 依赖 P1 到 P3 的数据，但可以先用现有数据渲染，等数据就绪后再接上。

---

## 8. 决定表

标 **⚠** 的是不可逆，或者触碰红线、用户已有裁决的项，需要用户确认。其余按推荐执行。

| # | 语义点 | 推荐 |
|---|---|---|
| D1 | 追问和纠正是否合并 | 合并为 `message`，删掉 UI 上的单选 |
| D2 | 消息是否推进 control_generation | 不推进 |
| D3 | 运行中的消息是否打断当前模型请求 | 不打断，下一轮按水位读入 |
| D4 | 空闲时有未读消息怎么办 | 调度器开续接 Run，id 为 `uuid5(incident, max_unread_seq)`，连续多条合并处理 |
| D5 | 暂停或挂起时的消息 | 进队列，恢复前不读，不开 Run |
| D6 | 续接 Run 的预算 | 和普通 Run 同样的防循环上限，不另设额度 |
| D7 | 只回复的结束 | 执行结果 `answered`；conclusion、lifecycle、观察都不变 |
| D8 | 回复合法、报告不合法 | 保留回复和旧报告，Timeline 记拒绝原因，事故不降级 |
| D9 | 回复也不合法 | 交接，原因 `RESPONSE_INVALID`，给 Retry |
| D10 | 恢复观察期间的消息或报告修订 | 观察继续，不撤销也不重新授权（每代最多自动授权一次，维持现规则）⚠ 触碰 C3 §10 中用户 10-15 的裁决 |
| D11 | resolved/closed 上的消息 | 允许，只回复，lifecycle 不变；要重新调查走 Reopen ⚠ 推翻 F12 r2 的 REOPEN_REQUIRED（用户已授权推翻，这里列出供知悉） |
| D12 | human-owned 上的消息 | 开续接 Run 回答，所有权不变，自动调查仍关闭 |
| D13 | 失败、交接、超时后的消息 | 开续接 Run（比 AWS 宽） |
| D14 | 已合并事故 | 禁用输入，链接到主事故 |
| D15 | 队列里的消息能否撤回 | v1 不支持（输入表只追加） |
| D16 | 作者与字数 | 登录身份「Send as」，不可代他人发送；上限沿用 8192（`INPUT_CONTENT_FIELD_MAX_CHARS`），不跟 AWS 的 32768 |
| D17 | 每条消息都要有回复吗 | 应有；缺失时标「not answered」，不重试 |
| D18 | reply 里的事实断言 | 必须引用本 Run 可见的证据，否则标 unsupported |
| D19 | 是否显示 `reasoning_content` | 不显示；显示可见 content 和时长 ⚠ 显示即新增数据接收方，按 PRODUCT-CONSTRAINTS 属于用户决定 |
| D20 | #184 自动观察中的「无人工处理」 | 消息不算人工处理，只有控制动作算 ⚠ 改变用户 10-12 裁决的判定 |
| D21 | Re-investigate | 即 `new_run`，推进代际，必须给新报告 |
| D22 | 修复建议 | 只读、可复制，无 Apply，也不生成可执行命令按钮 ⚠ 只读红线，不可放宽 |
| D23 | 页面上的 Run 编号 | 隐藏，只显示「resumed」，悬停显示 |
| D24 | 报告历史 | Summary 只显示最新一份合法报告；Timeline 的每条「Report updated」可以看 diff 和旧版 |
| D25 | 反馈的去向 | 一人一份，可更新；进 eval 和复盘草稿，不自动进知识库 |
| D26 | 旧 API（follow_up/correct） | 保留一个版本作为别名，再删除 |
| D27 | `answered` 枚举的 migration | 只增枚举值 ⚠ schema migration 回滚困难（Postgres 枚举值删不掉）；可改用 text 列加 check 约束来规避 |
| D28 | 进入 Timeline 的文本是否脱敏 | 和证据同一条脱敏路径；人类消息原样显示给已登录用户 |

---

## 9. 视觉方向（Cloudscape 风格，不用 AWS 品牌元素）

**颜色**
- 浅色：页面底 `#f4f4f4`，容器 `#ffffff`，边框 `#d1d5db`，正文 `#0f141a`，次要文字 `#5f6b7a`，链接和主按钮 `#006ce0`。
- 状态色：成功 `#00802f`，错误 `#db0000`，警告 `#855900`（底色 `#fffce0`），进行中 `#006ce0`，中性 `#5f6b7a`。
- 深色：底 `#0f141a`，容器 `#161d26`，边框 `#424650`，正文 `#dedee3`，链接 `#42b4ff`。

**排版**
- 系统无衬线字体栈，正文 14px，标题 18/24px，等宽字体用于查询和资源名。

**组件**
- 容器圆角 12px，带 1px 边框，没有阴影。
- 顶部导航是深色条（`#161d26`）。
- 状态用「图标 + 文字」标签，不只靠颜色。
- 表格行高 40px；Timeline 用左侧竖线加节点图标。
- Chat 输入框固定在底部（底部内边距加 `env(safe-area-inset-bottom)`）。

**规范**
- 间距以 4px 为基准（4/8/12/16/20/32）。
- 焦点环 2px `#006ce0`。
- 所有颜色对比度达到 AA。
