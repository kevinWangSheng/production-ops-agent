# PR16：完整报告、来源与时效的统一严格入口候选

基线`da02563`。状态：**设计已独立批准，离线实现和联合终审已完成**；见[最终机制审查](round-02-pr16-v4-final-review.md)。以下方案过程保留，新版真实模型未执行。不调用模型/trace，不启动PG，不改变旧报告、预算或M1门槛。

## 已核查依据与失败复现

SPEC要求每观察保留source/query/target/version/window/freshness，区分facts/hypotheses/建议/反证；C3 §5要求完整候选报告经过引用/目标/当前权限校验，§7要求历史观察保持原时间、需要当前状态时重新观察。C3 §10的HealthProfile属于独立恢复，不把该平台扩入本修复。

只读实际normal03：原报告summary 1218字符、next_steps 4条，Outcome均没有字段；16/16 claims的target为None。离线用现有最高seam构造6种反例，当前均返回`[]`：未声明scope的实例事实；failed+错target的counter_evidence；同样的rejected_hypothesis；2099未来capture；2000历史窗数据却用“currently”措辞；最后cancel但completed。没有改实际工件。这些不表示历史查询本身无效，而是当前入口无法表达/检查用途与来源。

相邻路径统一纳入：initial和动态view的时间记录；summary/next_steps与原报告绑定；三类事实性claim；最后accepted control状态；完整报告与物理response绑定。避免逐条添加独立旁路。

## 版本与最小改动面

建议新增`m0-public-v4` / `m0-report-v2`作为**当前默认严格入口**。v2、v3、report-v1的schema及原报告保留；旧v3仅显式legacy读取/重放，输出明确legacy assurance和新规则unknown，不能继续作为当前默认通过结果。旧报告不填造模型target或时间声明，不改为v2报告。

实现复用现有Target、身份/投影、控制与通用记录检查，不复制完整平台：新增小的严格扩展DTO/checker（可在`outcomes_v4.py`），`holmes_bridge.py`增加版本分派和严格适配；现有schema留存，新增v4/v2 schema与定向tests。runtime由另一执行者仅修改`report_contract.py`和wrapper的送模元数据/时钟记录，冻结源码快照不改。PG/预算/Observer不改。根任务负责冻结包导航和PR文档。

## 1. 完整报告与输出绑定

- 新Outcome携带完整安全报告原文、SHA-256和完整解析对象，包含schema_version、assessment_status、conclusion、summary、claims、gaps、next_steps；不要继续只拷贝claims/gaps。
- trusted报告捕获记录绑定Run/step/physical request/control generation、完整response接收时间及安全报告原文/hash。完成条件继续恰好一条匹配committed delivery，并要求Outcome报告原文/hash与trusted捕获相等，解析对象与原文逐字段相等。
- 尽量以嵌套report为唯一报告内容，避免summary/claims等多份可独立修改副本；若兼容出口需要扁平字段，必须与嵌套对象逐项相等。
- 无模型报告的真实blocked/cancelled/waiting_human等incomplete出口仍保留，report可为空；任何完成声明不得缺报告。
- summary/next_steps保全到最高seam供完整独立质量/权限审查；不声称普通字符串已通过机器语义证明。报告建议不会转为执行权限。

## 2. 三类事实性claim统一来源合同

- 新view实际送模时附有`target_bindings: [{target_ref,target}]`或同一物理请求中明确绑定view ID/hash的业务catalog；target复用现有精确Target DTO，由可信纯映射产生，ref绑定Run和canonical Target hash。per-view refs、完整catalog和时间policy必须真的进入该请求business payload及hash，并计入原HTTP字节/token上限；不能只写归档，不能在14KiB已裁剪view之后无预算追加。超限沿用既有bounded失败，不能悄悄扩容。
- 新report-v2要求`fact/counter_evidence/rejected_hypothesis`显式提供非空`target_refs`、evidence_ids及`time_scope_ref`。模型只能选择实际交付的binding和固定时间policy目录；bridge不默认subject、不根据引用反向填目标。假设/建议仍保持其非事实属性，若提供引用也必须可解析。
- 对每个声明target_ref，至少一份**该claim引用且同物理请求实际交付**的view必须支持；不能只因对象在授权scope里就通过。多源佐证允许各view分别支持不同目标，不要求不合理的全交集。
- 三类事实性claim统一检查引用、目标、有效状态和适用时效。failed/no_data/stale不能变成服务事实；查询失败保留在gaps与可信动作审计。Integration未知实例不能提升为Compose实例事实。
- 结构化scope与自然语言内容是否一致仍接受完整独立质量审查；不能宣称消除所有自然语言假归属。

## 3. 有界时效合同，不把历史查询等同陈旧

新增一个可信版本化时间policy及证据/交付时间元数据，不建设HealthProfile。policy由控制/实验合同在请求前固定（revision/hash、适用source/target、用途、reference规则和阈值）；模型只能引用policy/time_scope_ref，不能选择TTL或更换reference。

固定的是reference**规则**，例如使用这一物理请求的可信完整response_received_at作为保守参考；实际reference值从对应dispatch/response事件得到，不预先把Run开始时间当后续新观察的参考。不存在该可信事件时reference为unknown，不用本地重放时钟或模型自述补齐。

区分以下事实：

- query/authorization window：准许查询的时间范围，不自动是数据覆盖区间。
- source observation time/coverage及其可信依据：由数据源/适配器记录；未知明确保留。Prom instant vector的评估时间不自动证明底层sample新鲜。
- operation_started_at与collection_completed_at：原observed_at实际位于HTTP之前，不能重命名成采集完成。
- dispatch_started_at与完整response_received_at：同physical request的可信客户端交付区间。当前无法知道服务端精确读到prompt的瞬间，不伪造该时刻。
- trusted evaluation/reference time：可重复校验用的明确锚点，不随今天重放而暗中改变。

两种用途明确分派：

1. **historical_window**：只在有可信事件时间/coverage证明时检查可见观察与指定历史窗相容，以及capture→delivery→response的记录顺序；query window本身不是coverage，缺源时间仍unknown。不以“距今天很久”判stale。原数据完整性/来源/缺测仍须核对。结构化historical引用不能支持current用途；自然语言却写“currently”等矛盾措辞由完整独立质量审查负责，不声称已机器理解文本。
2. **current**：按可信policy的reference及max-age等有据阈值核对**源观察时间/age证明**，不使用刚收到HTTP来冒充数据新鲜。缺source age、缺policy/reference、缺交付依据为unknown；未来时间或明确越界拒绝。阈值必须显式配置，不能发明统一数值SLA。

初始和动态证据共用时间判定，report只可把符合其可信用途的证据用于三类事实性claim。stale/unknown仍可持久保存、展示并产生明确handoff，但不能冒充合格事实/恢复结论。时间校验不替代PromQL数量语义、覆盖率或因果判断。

历史v1/v3缺target或真实时间元数据时，保留完整原报告并给出scope/freshness unknown及当前严格验收不通过；不补推测时间、不把旧raw按新视图重新签名。这个兼容结果不改变原历史质量FAIL或原结构检查记录。

## 4. 最后人控状态与完成权

严格入口对齐现有StepStore：最后accepted action为cancel时当前execution必须cancelled；最后correct必须waiting_human；后续显式new_run才建立新Run继续并允许其正常完成。只看最后有效转换，不能让历史cancel永久阻断后续new_run。继续保留连续generation、current Run、物理report request匹配及迟到拒绝。此为现有状态责任对齐，不新增状态平台。

## 设计通过后的最小验收集

完整报告summary/next_steps改动或删失检测；完整response原文/hash/解析对象交叉篡改；三类事实无scope/错scope/failed引用；授权但未交付target拒绝、多源分别佐证通过；current fresh/stale/unknown/future与historical合法旧窗/结构化用途不匹配；初始/动态相同规则；cancel/correct矛盾、new_run后完成、旧Run/代次迟到；v1/v3显式legacy完整保留而不通过strict。历史报告的“currently”等文本语义矛盾保留给独立全文质量审查。全部先离线，不以预算为零阻塞实现；新版真实模型/时钟接入仍标未运行。
