# M0-02 fault-01 独立可诊断前提与报告审查

审查者为全新上下文独立 Agent，未参与实现或注入。只读核对 AGENTS、SPEC、C3 §5/11–13、本轮 round-02-contract.md、工程观察原始业务证据及实际模型交付记录。未访问凭据、provider 私有协议/推理、工程注入器配置，未发起模型、后端查询或环境操作。共享宿主并非 OS 隔离或盲测；本记录不认证健康、恢复或 M1 入口。

## 发送前可诊断前提

窗口 1789010952..1789011252（2026-09-10 03:29:12..03:34:12 UTC）。环境 worktree `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment` 下 `docs/evidence/m0-real-environment/m002-fault-01-observation.json` 的七个 raw 均重新计算 SHA256 与 bytes：calls 2743、transactions 101、checkout-rpc 1180、latency 142、traces-checkout 519743、traces-payment 519744、logs-frontend-proxy 25515，7/7 匹配。原件在 `tmp/m0-environment/raw-evidence/m002-fault-01/`。

工程观测的 calls/transactions/checkout-rpc 明确使用 `increase(...[5m])` 在窗口终点求值；延迟为 `histogram_quantile(.95, sum by(le,service_name)(rate(...[5m])))`。checkout 错误 span 增量约25.106，payment 错误 span 增量约12.553；checkout 调 payment Charge 的 code2 增量约11.298、code0 为0；payment transaction 增量0。有实际流量和窗口错误，增量外推小数不能当精确订单数，状态 UNSET 不能自动等价业务成功，42.652ms 的估计延迟不能在无 SLO/对照下证明健康或恶化。

两份服务 trace 查询返回相同11个 distinct trace，不能合计成22请求。核对至少两条完整 raw 调用传播：

- `2af2dcb24220d2a6f5b41087d4b0b8fb`：checkout PlaceOrder `d14a47c2c11fee49` → checkout PaymentService/Charge `45940d3c0a084de4` → payment Charge server `854f80ea8695a4cb`，均有显式 CHILD_OF；payment ERROR，经 checkout code2/PlaceOrder code13 上传 frontend，proxy ingress `68215b1df12c08bd` HTTP500。
- `aab74389f233d9ec29ba34d8848efef5`：checkout PlaceOrder `30db70f9a12bd0ca` → checkout Charge `19f99076efc77010` → payment Charge `7f05be4662689d30`，同样显式父子引用及错误传播，proxy ingress `59720c19b591fc2a` HTTP500。

raw payment 异常文字为 `Payment request failed. Invalid token. app.loyalty.level=gold`，checkout/前端异常传播同文字；这只是业务错误证据，不据此推测注入 flag。当前 `holmes_baseline.py:115` 的 trace 投影省略 exception/status_description，只保留指定状态标签、operation、source identity 和 parent references，并按错误优先/时长排序，20项和14000 bytes双限。因此模型不能被要求报告 raw 中未交付的错误文字或完整调用链；省略字段不等于原始遥测缺失。

日志 raw 后端 total180、returned20，最新优先，只有一条已返回 POST /api/checkout500：trace `124bb098579844844530eaab40293ff1`、span `b62178e953ecaffa`，在11条trace集合中。其余可见200不能推及全部180。日志 v3 还可能因14000 bytes缩减，最终只以实际 displayed_logs/model_visible_hit_count 判定模型可见数量。proxy日志没有容器身份标签时不能根据名称宣称 exact container。

初步结论：允许模型通过授权工具取得的范围足以支持有限目标——定位 payment 依赖的失败 Charge 调用及对 checkout HTTP500 的影响。可诊断前提成立；不要求解释内部注入机制，不以工程事实替代模型报告。实际报告通过与否待真正最后 HTTP delivery、registered views/raw 逐项审查。

## 实际报告审查

实际结果 `tmp/m0-environment/holmes-runs/m002-fault-01/result-business.json` 为 investigation_returned、4 HTTP、19 工具、最后 `m002-fault-01-http-17` HTTP200/stop，2309 completion tokens；报告 JSON 具有17项 claims，assessment_status=completed、conclusion=supported。已核对安全 response-17-business 的正文与 result 报告，未读取私有协议。

最后 delivery state=response_received、19个 registered views；business_messages canonical SHA256 为 `a0fa020e2ce69037423003e7c050b9581875b5205839da3837e29a1c3b1614ab`，重新计算匹配。19/19 view 交付 canonical hash、19/19 raw canonical hash、38/38 raw/view 文件 hash 均与 manifest 匹配，所有 claim evidence_ids 都是已交付完整 ID。非仅检查准备快照。实际请求 hash 为 `0e7c65aad490f602f99b3662101472786d65f55191cc3b2c33923b5859284ca6`（依赖可信运行器记录，未打开原始私有请求）。

实际 e8/e9 各349 raw spans、335 omitted、**14 sampled spans**，并非20。source identity table保留 payment exact container ID、image、config hash，尽管其 span 没有进入14项；报告建议中的 payment 身份与 e9/registry 一致。14项中 trace490fdb46cfa2ce55c61bbe602016604e 的 load-generator→proxy→frontend→checkout 链通过显式 parent references 支持，最深已显示的是 checkout PlaceOrder；checkout→payment 的进一步定位来自明确 client metric 的 rpc_service/rpc_method/status，报告将其放 hypothesis 是合理的。未声称已显示完整 payment span 是正确边界。

### 逐 claim 核对（序号按 final_report.claims，1起）

| 项 | 结果与证据 |
|---|---|
| 1 | 核心事实成立：e6 的 checkout500时间/trace一致，实际19显示、20后端返回，18条其余显示日志为200；“19 of20 displayed”措辞不精确，counter_evidence14正确明确19显示。 |
| 2 | 成立：e8 的 trace490fdb…所述链可经显示的 CHILD_OF逐段连通；省略中间 frontend internal span 的简写不导致虚构边。 |
| 3 | 成立但限定已返回指标系列：e11/e15 checkout Charge code2增量11.2977801954、code0为0，其余列出的调用code0；没有把累计桶相加当次数。 |
| 4 | 成立：e12/e18 PlaceOrder code13增量11.2977801954、code0为0。表述“all counted”保留了计数范围。 |
| 5 | 成立于view：e16 payment ERROR/UNSET各12.5530891060；e9显示样本无payment span/operation标签。不能外推raw缺少这些标签。 |
| 6 | 成立：e5是USD77/CAD6累计，e13两者5m increase为0，报告明确区分累计与窗口，没有将历史值当本窗交易。 |
| 7 | 数值成立于返回的span metrics；“confined”只能指这些已观测服务系列，不能认证全系统无其他错误。 |
| 8 | **P2关联及覆盖错误**：e7 cart backend94、显示20，仅支持这20条Information GetCart/AddItem。e6中trace124bb…是checkout500；cart HTTP200属于其他trace（例如d3c3ce810c7950967aebca8b116788b3、96edb9bfb32fea52305d10961ba3b0e4）。e7有同124bb的GetCart日志，却没有HTTP200结果，不能拼成同一失败trace的cart200。 |
| 9 | 查询零命中成立：e10 payment日志 total0；“own error messages not observable”应限定日志源/已交付view，raw trace exception仍有错误文字。 |
| 10 | 支持的有限因果假设：payment Charge失败与checkout PlaceOrder及HTTP500一致；由不同信号联合推断，未展示payment完整span。 |
| 11 | 合理假设：payment span错误增量+transaction0支持到达后未记交易的解释，但非内部机制证明。 |
| 12 | 成立：e17为空，明确没有“observed”更深调用，而非证明不存在任何下游依赖。 |
| 13 | **P2计数错误**：声称20/349，实际14/349，335 omitted；遗漏受20项及14000bytes双限，报告只给error-first/时长偏差，没有准确说明字节限导致再删6项。 |
| 14 | 成立：proxy180后端命中/19显示，不外推其他日志状态。 |
| 15 | **同P2关联/覆盖错误**：cart被排除的方向符合指标，但所举同error trace HTTP200证据不成立，20/94日志也不足以说全窗口日志无错误；应改为可见证据不支持cart是主故障点，并引用e11明确的GetCart成功指标。 |
| 16 | 身份成立：payment容器8d8e65…、image33f327…、config a95bfc…均来自e9 identity table的exact container.id映射；仅人工建议，未执行操作。 |
| 17 | 条件式建议，未声称已查明flag；CEL/flagd routing/retry无这些证据支撑，属不必要具体化，宜收窄为获取payment错误message。 |

### 未知、建议及总体判定

gaps重复20/349错误，及“payment error messages absent”将日志零命中/视图省略混为总体遥测缺失。独立raw核对已知e8/e9保留payment `grpc.error_message` 和异常事件，而当前view省略；不能要求模型陈述未见文字，但可以要求说“当前投影视图未提供”，而非据此判定必须新增采集。建议优先从已捕获raw通过授权片段接口取得信息，再考虑应用日志补充。最后 next_steps中的 `cluster m0-otel-20260909` 实为 integration_id，不能作为已验证cluster身份；人工查看额外数据是建议，不是模型获准扩大访问范围。

**分层结果：报告收束成功、有限故障依赖定位成功；完整报告质量未通过。** 保留3组P2（cart错误关联/日志覆盖，trace实际显示计数，原始证据与投影缺口混淆）。完整引用/交付真实性、累计与窗口区分、主要定位及payment源身份通过。未出现模型执行修复、批准发布或认证健康；因果置信度为supported/hypothesis的定性表达，仍需按产品冻结评价合同校准。

这是本轮一个开发故障样本，未构成泛化/盲测统计，也不回改旧轮失败；M0退出/M1门槛由父记录依据全部独立证据决定。本审查仅写此文件，未提交或修改实现。
