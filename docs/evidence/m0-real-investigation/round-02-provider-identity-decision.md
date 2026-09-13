# M0-02 服务端报告名称兼容证据与候选决定

当前候选，待独立核验。两个请求都直接发往官方HTTPS api.deepseek.com/v1/chat/completions，请求model=deepseek-v4-flash、thinking enabled/high，没有切换请求模型。首response身份校验失败，未保留返回值，未知不倒推。第二完整受限响应与安全业务投影记录response.model=deepseek-flash，stop、35721输入/15104输出。

2026-09-10T02:28:19.494432Z父用相同可信认证客户端对官方GET /models作一次只读metadata查询（非生成请求），HTTP200，列表仅deepseek-flash和deepseek-v4-pro，owned_by均deepseek；原安全记录round-02-provider-models.json与响应hash保留。当前官方API文档仍要求model=deepseek-v4-flash，https://api-docs.deepseek.com/ 及 https://api-docs.deepseek.com/quick_start/pricing/ ，查询时文档说明Flash-0731。

推断：官方服务接受文档指定的Flash请求名，返回自己当前metadata列出的Flash名称；这是请求别名与响应canonical名称差异的实证，不是固定后端权重或版本相同的证明。它不支持接受Pro或任意未知返回模型。

候选工程处理：继续强制出站deepseek-v4-flash，不改thinking/high；将本轮已核对response允许集固定为deepseek-v4-flash与deepseek-flash，并绑定metadata源/hash。记录requested_model和reported_model两字段，禁止把响应字符串重写成请求名；保留reported alias层级，不声称固定权重。其他模型继续拒绝。原两次failed状态与诊断丢失原样保留；不追改成通过。第二已有报告另由独立Agent质量评估，存在6项P2，不能靠身份兼容修复算质量通过。

独立审查需确认此映射仅解决官方请求与报告标识接缝，没有模型降级/切Pro/新增数据出口；如证据不足，保留blocked和具体未知，不盲发更多请求。费用只按本轮已确认usage及官方Flash峰值给保守估计，不冒称实际账单；首3.44064因用量丢失继续全unknown，旧24CNY不动。
