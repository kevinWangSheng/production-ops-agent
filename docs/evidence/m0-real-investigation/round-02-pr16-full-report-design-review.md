# PR16 完整报告与验收合同独立设计审查

审查基线：`da02563968341181515fd24668f0d425f797ce3f`，工作区 `production-ops-agent-m0-01`。独立 Agent 新上下文审查，未参与实现。2026-09-10 UTC。

## 范围与依据

读取 AGENTS、SPEC 全文、ROADMAP、v3 冻结包、C3 调查循环/证据/恢复观察相关合同、`outcomes_v3.py`、`holmes_bridge.py` 与两份直接测试。通过 GitHub API 读取 PR16 原发现 3976067577、3976067579、3976067585。只执行离线合成 fixture 反例；没有读取 `.env`、provider 私有协议、密钥，也未启动环境、调用模型或发送业务证据。本记录不是自然语言报告质量复审结果，不打开 SPEC 实施门槛。

## 已确认发现

1. **P1：完整报告没有跨过外部验收接缝。** `ModelReport` 有 `summary` / `next_steps`，`IncidentOutcome` 无此字段，bridge 仅复制 claims/gaps 等字段。下游无法经唯一公开 Outcome 审查摘要中的错误或建议的越界含义。必须保留最终响应的完整安全业务报告，并绑定原物理请求及原内容 hash；只增加检查 claim 的规则不足以解决。
2. **P1：所有历史真实报告的事实 target 均缺失。** `ModelReport.claims` 使用无 target 的旧 `Claim`；bridge 复制后 `ScopedClaim.target=None`；检查器因此跳过身份绑定。不能依据调查 subject、唯一引用、工程已知故障或 registry 替模型填写结论目标。目标缺失应在新版评价中显式 unknown/fail，旧结构通过仍只是旧 evaluator 的历史结果。
3. **P2：时间契约缺少适用策略与可信参考时间。** 现检查仅有查询窗不能超过 scope、window.end 不能晚于 captured_at，没有 delivery/reference 时间或 freshness policy；`status=ok` 不建立新鲜性。历史调查合法，不能把 window.end 到 captured_at 的大间隔一概当错误。
4. **P2，相邻实质漏洞：反证与排除假设绕过来源有效性。** `counter_evidence`、`rejected_hypothesis` 要求 evidence_ids，但只有 `fact` 检查 failed/stale 状态及 target。于是模型可以用另一个服务的失败查询排除核心原因，检查返回成功。三种证据断言应共用身份、来源状态、适用时间校验；失败查询本身作为操作事实应由明确的操作审计类型表达，不借此支持服务事实。

## 离线反例与边界

命令：`.venv/bin/python` 通过 `runpy.run_path('tests/test_m0_outcomes_v3.py')` 复用 `packet()` 和 `check()`，分别独立修改：

|反例|修改|当前返回|
|---|---|---|
|missing_target_wrong_service|target 缺失，正文写 checkout，引用仅 payment 的 artifact|`[]`|
|counter_failed_wrong_target|conclusion=partial；反证 target=checkout，artifact target=payment 且 status=failed|`[]`|
|rejected_failed_wrong_target|同上，kind=rejected_hypothesis|`[]`|
|capture_in_2036|captured_at 改到 2036，2026 查询窗与其他记录不变|`[]`|
|cancel_current_generation|最新 control action 从 correct 改为 cancel，保留 completed/current generation|`[]`|

前四项直接支持上述发现。第五项说明 public checker 只验证控制序号和记录一致性，不验证取消后的采纳是否合法；已知 PG 采纳屏障承担真实控制责任时不得把此函数成功宣传为完整人控证明。最小设计需明确可信采纳状态/权威来源，或拒绝最新取消状态下的 completed candidate；不能在本轮顺带实现 Observer 平台。此项已向环境 owner 提醒，待其结合当前 PG 合同确认负责的接缝。

这些反例检查确定性结构能力；即使补齐 target，正文仍可能说另一目标、错数值或无据因果，必须由独立完整报告审查判断。

## 最小兼容设计建议

- 保留旧 `IncidentScenario/Outcome.v3`、`ModelReport.v1` schema 与原 fixture/原报告/原投影版本。新严格接缝采用新 schema/evaluator 版本；旧读取器继续解释旧数据。历史重新评价另产有版本、有原 hash 的新结果，不回写旧 PASS，不篡改原报告去适配 schema。
- 新 Outcome 携带完整安全 report（含 summary、claims、gaps、next_steps），以及精确原业务正文或可验证的持久引用/hash；绑定 run/step/physical request。确定性字段与原报告须相等，不能只验证 report 输入交付而遗漏输出绑定。没有模型报告的可信预算/取消 handoff 应明确区分，不能伪造一份报告。
- 新模型报告对 fact/counter_evidence/rejected_hypothesis 要求显式 target；允许确有证据的 integration unknown identity，不能自动升级成 Compose 实例。模型可从已交付的可信 identity 选取目标，目标解析不得改写原文。历史 v1 target 缺失保留缺失并报告无法验证。
- freshness 作为可信运行事实而非模型自述：固定 policy revision、用途（历史窗口调查/当前状态）、evaluation reference、实际捕获与交付时间、适用时间窗及上限来源。抓取延迟、证据交付年龄、源数据时间覆盖各自有语义，不混为一个 age。
- 对历史调查，按请求固定的过去窗口和源时间覆盖评估；不能因为今天复审旧报告而把当时新鲜证据自动判陈旧。对当前状态，按当时可信 reference 与适用 freshness 限制判断。缺少 policy/reference 的历史数据返回 unknown，不补造时间。stale/failed 可原样保存为 gap/unknown，不能支撑合格服务事实或健康结论。
- 先冻结新合同及阈值语义，再实施；新合同的历史回放只验证桥接/来源和可见性，不能提升已失败的自然语言报告质量、生成新的模型样本或宣称候选通过。

## 实施后必需复验

覆盖完整报告保真及输出篡改、错 request、target 缺失/错服务/未知实例、反证/排除假设使用失败证据、历史窗合法且时间不可重标、未来 capture/delivery、不适用或缺失 policy/reference、当前数据陈旧、无模型报告合法 handoff。需运行相关旧回归，单独记录旧 schema 读取兼容与新评价失败/unknown；再次独立审查最终代码和最终合同后才能关闭本设计审查。

当前结论：三项原发现及一项相邻证据漏洞成立，修复尚未实施或验证；本记录不宣布设计或实现通过。

### 控制接缝补核

随后独立读取 `StepStore.control`（225–245）及 `control_snapshot`（726 起）：accepted cancel 将业务状态置 cancelled，correct 置 waiting_human；快照仅导出 accepted 控制事件。因而上述第五个反例可按现有合同作为 P1 状态一致性缺口修补，不需要新平台：取最后 accepted 动作，cancel/correct 之后不能采纳完成报告；最后 new_run 接续后允许新的有效完成，历史 cancel 不应永久阻断。需要明确这是对已存在可信业务状态的一致性检查，不能替代 PG 原子发起/提交屏障。原 fixture 用 correct 作为完成前最新事件也应在新契约中纠正为合法接续，不能为保留 fixture PASS 放松该规则。

## 具体方案独立复核

复核对象：[严格接缝方案](round-02-pr16-strict-seam-design.md)，在同一独立审查上下文中阅读具体候选，并向作者要求以下明确化：

1. `target_bindings`/per-view refs/可信 time-policy 目录必须进入实际物理请求的 business payload，纳入 hash 及既有字节/token预算，超限按既有规则有界失败。只在 bridge 归档后生成字段不算模型看过目标；裁剪视图后外挂目录也不能绕预算。
2. 三种事实性 claim 显式 `time_scope_ref`，引用已实际交付且请求前固定的可信 policy。历史窗口事实也需要源事件时间或可信 coverage；query window/Prom instant evaluation timestamp 不能替代底层 sample 新鲜性。
3. “currently”等自然语言与结构化 historical 用途的矛盾由完整独立报告审查处理。确定性用例只能证明结构化范围/时间引用合法，不得将其命名为自然语言含义已经验证。

上述明确化纳入最终方案后，**此有界设计通过，可进入离线实现**：public-v4/report-v2 与显式 legacy 分派、完整安全报告及输出 hash/request 绑定、三类证据断言的共同目标/状态/时间校验、当前与历史用途区分、末次 accepted 人控一致性均符合已批准合同。没有批准任何新的数值阈值；policy/source time 缺失时须保守 unknown，不授予 current factual adequacy。

特别核对：旧 `observed_at` 位于 HTTP 前，只能保持原语义；新 `collection_completed_at` 应在实际收集后记录；`dispatch_started_at` 至 `response_received_at` 是交付/响应的客户端界限，不伪造精确模型读取时刻。离线重放使用固定原 reference，不能用复审当前时刻改写旧证据时效。

通过范围仅为设计，不代表实现已验证、真实新版模型接入可用、旧自然语言报告质量改善或 M0/M1 门槛开放。最终代码及新 schema 仍需定向回归与独立复验。

最终稿复核：作者已将上述三项明确化写入方案 §2–3 与最小验收集；本审查者已重新逐段读取确认。**有界设计审查通过，可按该最终稿执行离线实现。** 尚无实现通过结论；没有新增费用、环境操作、阈值或功能验收授权。

## 活动实现预审（不作为最终版本通过）

初读 `outcomes_v4.py` 后复用 `tests/test_m0_outcomes_v4.py::strict_packet/checked/resync_report/resync_context` 独立构造三项反例，均在活动版本返回 `[]`，已交实现者处置：

- 实际报告 completed/inconclusive 且 claims=[]，dispatch_started_at、response_received_at、capture.response_received_at、evaluation_at 全缺失：所需报告时钟只在事实循环检查，空报告绕过。应独立验证所有实际报告的响应时钟与顺序，不能拿空 claims 获得严格通过。
- hypothesis 提供不存在的 target_refs/time_scope_ref：可选引用仍须可解析，不能因为其不是事实就接受悬空结构化引用；无需强求假设具备事实依据。
- 当前用途、max_source_age=300 秒，可信 source event range 为 00:01:30–00:01:40，但 artifact/scope 窗为 00:00–00:01，仍通过。需明确源事件时间与获准查询窗的相容性；快照来源按其接口语义处理，不应把 query window 伪造成 coverage。

候选验收包 v4 与旧 v3 已逐行比较：正常/故障各2 Run、300秒窗、至多16 HTTP、原每Run/工具/token/字节/时间上限、P1/P2 和非退化规则、72小时后续要求均保留。新增严格规则没有降低原候选门槛；最终冻结仍待稳定实现版本复验。
