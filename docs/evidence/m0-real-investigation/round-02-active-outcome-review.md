# M0-02 主动调查独立结果评估

日期：2026-09-10。独立评估者未参与实现或调优，按本轮合同、SPEC证据与权限要求核查业务工件。只读既存文件与程序计算hash/计数；未读取.env、private-protocol、PG私有响应或推理内容，未调用模型、trace、查询或环境操作。共享宿主/worktree不构成OS隔离或盲测。

## 正常 Run：报告合同失败

`m002-normal-01`完成4个HTTP、16次工具查询，但**未产出可评分调查报告**。`result-business.json`的`final_business_content`与`response-6-business.json`最后choice一致，只有DSML调用otel_traces/otel_metrics的文本；没有事实、假设、反证、未知或建议，也没有完整evidence_id引用。`finish_reason=stop`和`status=investigation_returned`只证明返回了正文，不证明调查完成。应记录调查未完成/报告收束失败，不能记作“完成但结论不确定”，不能将工程正常观察替代模型结论。

业务安全响应显示最后一次完整HTTP200、reported `deepseek-flash`，32768额度下实际completion_tokens 2416；这不是已证实的length失败。仅凭业务输出不能确定DSML落入正文的内部根因。服务名称允许集沿用已独立审查的限定兼容决定，权重/精确模型版本仍未知。

## 已交付与证据核对

环境worktree `tmp/m0-environment/holmes-runs/m002-normal-01/`：4份delivered记录的全轮request ordinal为3、4、5、6，均response_received；累计业务views依次0、6、12、16。最后请求实际交付`m002-normal-01-e1`至`m002-normal-01-e16`；逐一重新计算canonical SHA256均等于delivered记录，16份manifest的raw/view文件SHA256全部匹配。没有把准备但未发送的证据计作已见。

主动查询包含服务发现、cart/frontend-proxy日志、checkout/payment/cart/frontend-proxy trace及metrics。需保留以下解释边界：

- `m002-normal-01-e2`、`m002-normal-01-e13`、`m002-normal-01-e16`是窗口结束时累计counter，不是5分钟increase；其中payment累计ERROR=5不能当该窗口错误增量。工程calls的5分钟ERROR增量为0并不与累计历史错误矛盾。
- `m002-normal-01-e7`、`m002-normal-01-e8`、`m002-normal-01-e14`的+Inf bucket可按其标签解释累计观测；`m002-normal-01-e15`按le分组的多个累计bucket不能相加作为calls。`m002-normal-01-e9`的累计transactions不能当该窗独立订单数。
- `m002-normal-01-e5`和`m002-normal-01-e6`为样本trace视图，spans不等于订单；保留parent信息也不能证明完整调用链或总体覆盖。
- 未提供baseline/SLO/HealthProfile，不能认证healthy/恢复、趋势改善或总体错误率。最终正文没有做出这些错误断言，但原因是根本没有报告，不能据此评为质量通过。

## 从原始工程文件独立核查正常窗

窗口1789007908..1789008208。`docs/evidence/m0-real-environment/m002-normal-01-observation.json`列出的7份`tmp/m0-environment/raw-evidence/m002-normal-01/`原始文件均存在，字节SHA256逐一匹配。直接解析raw确认：calls的ERROR增量全0；transactions增加8.748213906327457，checkout→PaymentService/Charge status0的+Inf增量9.998666844420743。浮点increase为Prometheus外推观测，不是精确订单量。

直接遍历两份raw trace：checkout和payment查询各9条trace；各含checkout 124 spans、payment 18 spans，所有采样spans的error=true计数为0。两集合trace_id相同，不是18个独立样本。frontend-proxy日志返回20/148 hits。由此仅支持“有实际流量，既存窗口指标与有限样本未观察到错误”，不能支持全量健康。工程事实有诊断价值，但模型未报告，正常Run结果仍失败。

## 审查锚点与后续

- result-business.json SHA256：`fe00815ba2f055bf7fe194694afe4f5435014b6936c589959822150d6b49d88d`
- delivered-business.json SHA256：`b4c6078450aab5db79cb86329f8cac8a404ec5ba0d47aef6a45e380858b40efc`

建议先保全并解释报告收束失败；本评估不授权重试或调参。真实故障窗/报告尚未在此审查；后续应另节接续独立工程事实和实际交付报告的逐claim核对。正常报告0/1，不能打开M0或产品实施门槛。
