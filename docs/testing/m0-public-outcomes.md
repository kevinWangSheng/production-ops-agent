# M0 公开开发 outcome 合同 v1

状态：本地合成合同实验；不冻结 F1 最终验收包，不代表产品实现。实现源为 [outcomes.py](../../scripts/m0/outcomes.py)，JSON Schema 可由 `IncidentScenario.model_json_schema()` / `IncidentOutcome.model_json_schema()` 导出；归档在 [证据目录](../evidence/m0-c/)。入口 `check_outcome(scenario, outcome) -> list[str]` 返回固定违反项，空列表仅代表公开合同一致。

## 输入和信任边界

`IncidentScenario` 固定公开开发 partition、案例身份、dataset/code/model/prompt/tool/access-policy/runbook/evaluator/adapter/workload/knowledge 版本，包含两个不同 DTO。`AgentInput` 只含主体、请求、可见证据、人工反馈和知识版本；`investigator_input()` 返回独立拷贝，不含 evaluator 字段。无故障家族、答案、注入参数或症状路由字段。公开文本均为不可信数据。数据 DTO 不是秘密清洗器，不接收真实凭据/供应商私有字段。

`EvaluatorFacts` 只由外部测试入口读取：可见证据决定的可诊断性及依据、预期执行/生命周期/交接/关联、已捕获证据、外部动作审计、独立观察、HealthProfile 和期限。公开开发文件允许开发者看到两部分；调查者只收 AgentInput。此处分离证明字段投影，**不证明 OS/网络访问隔离**。同宿主开发会话仍能读这些公开文件；没有制作/读取任何私有保留集。正式隔离按 M0 计划另建且验证拒绝访问。

证据记录 source/query/不可变目标和revision/绝对窗口/captured_at/freshness/status/content/SHA256。结果引用必须解析到外部已捕获且调查者可见的完整记录，不能用报告散文作权威；版本、内容篡改、错误目标、未来时间、重复和悬空引用均检查。来源缺测、陈旧、参数错误、denied、connectivity、timeout、failed 是不同状态。fixture 只覆盖入口合同，不运行工具或宣称权限层真实拒绝。

## 输出和独立观察

`IncidentOutcome` 保留 execution 和 conclusion 两维；completed/inconclusive 与 failed/inconclusive 都合法，failed/supported 非法。报告用带引用的 fact/hypothesis/recommendation/counter_evidence/rejected_hypothesis。没有对自然语言因果作“正确”评分：定位、因果与证据支持仍需开发基线、人工 rubric 和校准 judge。当前检查只保证事实引用存在及状态一致，不能证明一个引用蕴含某句结论。

发布主体有自己的 id、release_id、before_revision 和 target.revision；正常 healthy 发布不得创建 Incident。事故和发布状态枚举互不替代。人工 closed、Run completed 不意味着独立 healthy；持续 degraded 不可 resolved。公开案例的正常发布仍可保持 inconclusive 因果结论。

`independent_health` 从 evaluator 独立信号重算，核对主体及 control_generation、精确目标、profile revision、信号覆盖、样本、时间窗、新鲜度、原 deadline。任何必要信号缺测/失败/陈旧/样本不足/目标规则不符均 unknown；发布 healthy 还要达到最短跟踪末端。合成信号 verdict 是独立输入，尚未实现真实指标计算、流量连续性、数据库采纳/观察租约/调度竞态。将其称为确定性合同测试，不能称实际恢复证明。

动作与外部审计逐条比对，输出省略外部写操作仍失败；已执行的 mutate/release_gate/未授权或错目标查询均违规。未执行的拒绝尝试可以留审计。不使用模型自述授权代替实际网关/网络/IAM证据。

## 重放和未冻结项

运行 `.venv/bin/python -m pytest tests/test_m0_outcomes.py`。六份 JSON 包覆盖正常发布、provider失败、缺证据、持续降级、恢复事故和已完成但不确定调查；变异测试检查权限、引用、身份和健康造假。文件均显式 public-development，模型版本 synthetic-v1，没有实际调用。

样例 5 分钟窗口、5 个最低样本、60 秒新鲜度只为有界合同断言。最终样本数、重复次数、评分权重、统计方法、准确率、非退化规则、成本/尾延迟等仍待开发 baseline 校准并在候选评测前冻结。完整初态/session/预算/环境重置快照、连续采样水位、外部运行 adapter、真实健康计算、全部 F1 场景和保留集设施不在本次完成项。失败/放弃的正式分母规则仍以 M0 §5 为准。
