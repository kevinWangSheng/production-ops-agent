# 2026-09-09上一轮真实调查验证结果（历史）

PR #15已于2026-09-10合并；后续新轮结果见[round-02-results.md](round-02-results.md)。本文件保留当时事实，不复用当时预算或进行中指令。

2026-09-09。本轮已从合成入口推进到固定真实软件环境与实际上游调查，但**故障调查报告尚未完成，首片产品实施门槛不打开**。20次模型请求已用满，服务已停止、数据和失败保留。未修改任何feature passes、未合并PR或部署产品。

## 实际跑通和失败

- **Flash最小链路完成**：新诊断明确Markdown围栏导致严格JSON失败；仅最终请求启用json_object后，2请求/10.792秒完成固定read_fixture→精确target/evidence_id→PG业务/诊断/outbox→LangSmith默认项目白名单上传/回读。原丢失正文的旧Flash失败不能倒推同因。[原始结果](flash-results.json)、[独立核查](flash-review.md)。
- **真实环境运行**：OTel Demo 2.0.2、25原服务+1只读代理，真实日志/指标/trace和部署身份记录可查询。仅整个专属Demo有三类来源，checkout/payment/frontend日志及payment RPC histogram等缺口明确保留。权限有实际wrong-target/写/非法查询拒绝；internal探针网络也拒直接后端/宿主/Internet，但实际Holmes在宿主运行，不能用探针认证它的OS隔离。[环境证据](../m0-real-environment/environment-results.md)。
- **正常调查**：3次主动尝试，前2无报告；normal-03为4模型/14工具、83.508秒首发至末响应跨度。独立核验checkout p99约74.25ms、5条去重trace/236 spans可见error0，支持“该窗采集证据未显示checkout/payment错误或明显异常”。模型healthy措辞超出未校准SLO/覆盖证据，span数不是唯一订单数，短引用尚需产品解析适配。[独立结果审查](investigation-outcome-review.md)。
- **故障事实成立，模型报告未完成**：7条独立trace显示payment Charge错误→checkout传播，日志出现POST /api/checkout500，支付成功增量0。两次主动调查分别3模型/14工具、3模型/12工具，第四待发包131799/158029bytes超过128KiB，均无最终报告；原normal-01内部异常具体原因仍unknown，不能把HTTP200误称provider500。
- **最后新Run接续仍未完成**：只继承fault-02的12份已保存业务视图，1请求、0新工具、90513bytes在上限内；66.066秒后finish_reason=length、8191输出tokens但正文为空。它不是原主动fault成功，也不能证明模型没有诊断能力。两次主动故障报告仍0/2，另接续1次incomplete。
- **还原与保全完成**：工程身份按原字节快照还原；独立后续5min指标错误增量0、交易/Charge成功恢复增长，8条checkout200日志与8条traceID一致。仅此工程观察，不是F6认证。停前归档19服务2792 distinct traces；26容器、Prometheus/OpenSearch卷、专属VM及原PG都保留，系统PG未动。[环境收尾](../m0-real-environment/closeout.json)、[PG收尾](postgres-cleanup.json)。

## 本轮具体复用与缺口

Holmes原ToolCallingLLM/ToolExecutor/共享prompt与固定只读源适配实际运行，不是全默认CLI工具或同条件候选评测。实证修复顶层thinking被LiteLLM拒绝、无shell时大trace被清空、重复日志metadata、PromQL422被误记502；完整raw与真正交付的model view分别保留。没有修改整套症状prompt或构造通用审批/恢复平台。[上游复用/配置/适配/扩展映射](../m0-real-environment/holmes-mapping.md)。

仍需解决：输入历史与输出预算/收束相互不匹配；动态证据/可见view与旧静态v2验收接缝；关注主体、依赖授权与真实Compose来源身份；逐ModelStep/ToolOperation持久重建、owner/Run及取消后新调用拒绝；实际调查进程与声明窗口/权限绑定。小型PG探针只证明最终业务快照/控制CAS和实际PG重启保留，不能替代完整步骤恢复。

## 版本、用量和费用

- Flash显式deepseek-v4-flash、thinking enabled/high；响应只证明reported alias，不证明不变后端权重。
- Flash运行CPython3.12.13 / HTTPX2 2.12.0 / LangSmith0.12.2，源码/lock/fixture digest见flash-results.json。环境为Colima专属aarch64、4 CPU/6 GiB/24 GiB盘；OTel commit63649d6d6a59de88fb421b88c3c3a6185b6d21ad及26镜像digest锁定。
- Holmes commit5e983c17f30e93099c7d775167266d4cd1d586c4，上游锁定LiteLLM1.89.0/OpenAI2.44.0/HTTPX0.28.1；实际配置、源hash与完整私有原始工件位置见[基线运行](../m0-real-environment/holmes-execution.md)。
- **20模型、1trace上传、59工具查询；输入170078、输出46839，总216917tokens。** [总账](final-usage.json)与[逐Run计时/用量](../m0-real-environment/holmes-per-run-usage.json)可核对。Holmes耗时按首模型请求→末响应计算，含中间工具，非完整进程wall；失败后的工具/启动不包含。
- 按实际空闲时段与已知缓存、Flash按miss保守估算约**0.3834861 CNY**；全miss空闲估算0.4658925，峰值全miss估算0.931785。价格依据[DeepSeek官方](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)。账户聚合余额减少0.39 CNY，不等于逐Run账单；可归属实际模型/trace账单仍unknown。新20+旧4 CNY未核账预留保留，**预留24 CNY不是实际花费24 CNY**。剩余trace次数不授权再发模型或自动上传业务。

## 实施决定与下一项任务

2026-09-09决定：SPEC首片门槛仍未满足。已取得真实环境、正常报告及故障可诊断事实，不能据此填passes=true；完整主动故障报告仍缺。下一项为[M0-02：故障报告收束与动态证据验收冻结](../../plans/first-vertical-investigation-2026-09-09.md)，条件满足后实施M1-01完整提交→查询→展示→追问/取消→持久保存流程。只补此首片前提，完整UI/发布/恢复/知识/72h soak在原后续阶段。

PR #15交付此修复与证据，远端CI与Code/Security审查结果在PR中维护；不自动合并。已处置远端image-lock覆盖P2，旧锁/配置hash改为只读校验输入，局部CLI红→绿及独立复验通过，原运行证据字节未改；新提交复审不借旧head结果冒称通过。工作区与原始数据保留位置见[当前任务](../../tasks/2026-09-09-m0-real-investigation.md)和[复现说明](../m0-real-environment/reproduce.md)。
