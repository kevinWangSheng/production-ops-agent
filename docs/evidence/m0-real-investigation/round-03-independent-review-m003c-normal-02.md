# 独立审查:m003c-normal-02
审查者:独立 Agent(全新上下文),0 模型请求,只读。

## 核对范围

读取的文件(均在 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-runs/m003c-normal-02/`):
- `result-business.json`(final_report、finish_reason=stop、model_http_requests=3、quality_assessment=pending_independent_evidence_check、boundary_errors=[])
- `m003c-normal-02-e1..e16-tool-model-view.json`(全部 16 条模型可见视图,逐条读取)
- `m003c-normal-02-e1..e16-manifest.json`(全部 16 条)
- `m003c-normal-02-e*-raw.json`(仅用于 hash 重算,未据此对模型结论加分)
- `time-policies.json`、`question-original.txt`、`delivered-business.json`(仅用于核对 target_catalog)
- 工程观察:`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/docs/evidence/m0-real-environment/m003-normal-observation.json`
- 判据:`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-01/docs/testing/first-investigation-v4-2026-09-10.md` 第 61–75 行"确定性与报告判据"。

未读取:任何 `.env`、`private-protocol/`、含 injector/answer/flag 的文件、`round-03-*.md`。

Hash 重算结果:
- 文件级 sha256(`raw_file_sha256` / `view_file_sha256`):e1–e16 共 16 对,全部一致。
- 规范化 sha256(manifest 声明算法 `sha256(json.dumps ensure_ascii=False sort_keys=True UTF-8)`,字段 `raw_sha256` / `view_sha256`):对 e2、e7、e8、e15、e16 重算,5 对全部一致。
- 附带观察:e1 的 raw 与 view 完全相同(同 hash),e7/e15/e16 的 raw 约 1–1.5 MB 而 view 约 18 KB,说明 view 是强投影,模型确实只能看到投影内容。

## 逐 claim 核对表

| # | kind | 报告陈述(摘) | 引用证据 | view 中实际 | 判定 |
|---|------|--------------|----------|-------------|------|
| 1 | fact | checkout span calls 窗口 increase 114.81,唯一 status_code 序列为 STATUS_CODE_UNSET;集成级分解无 checkout ERROR 序列,ERROR 序列仅 load-generator/frontend/payment/frontend-proxy 且各为 0 | e2,e3,e10,e11 | e2:`{checkout, STATUS_CODE_UNSET} = 114.80626`,仅此一条。e3:按 rpc 标签分组仅返回 `{service_name=checkout}=114.806`(无 rpc 标签),不与陈述冲突。e10:20 条序列,ERROR 标签仅 load-generator/frontend/payment/frontend-proxy 四条且值 "0";checkout 仅 UNSET。e11:同上四条 ERROR=0 | OK |
| 2 | fact | 服务端 `_count` PlaceOrder rpc_grpc_status_code="0" increase 8.74;7 条客户端 bucket 序列均 code "0"(GetCart/EmptyCart/Charge/GetQuote/ShipOrder/GetProduct/Convert);非零 code 的客户端/服务端查询返回空 | e4,e5,e12,e13,e14 | e14:`_count` PlaceOrder code 0 = 8.73515 → 8.74 正确。e4:恰 7 条序列,方法名与 rpc_service 全部对应,均 code "0"。e5:`_bucket` PlaceOrder code 0 = 101.08(报告未把它当次数,正确)。e12、e13:`result: []`,且 actual_sources.services 为空 | OK |
| 3 | fact | 下游 payment:10 traces / 20 payment spans,error_spans 0,Charge 的 rpc.grpc.status_code=0;app_payment_transactions_total increase 8.73,返回集内无失败标签序列 | e6,e16 | e16:backend 10 traces / 496 spans;backend_counts_by_service.payment = {span_count 20, error 0};可见 14 个 payment span,Charge 全部 `rpc.grpc.status_code: 0`。e6:仅 `{service_name=payment}=8.7348`,无 status_code 标签 | OK |
| 4 | fact | checkout traces 无可见失败:error_spans 0(132 checkout spans);10 个 PlaceOrder 可见 span 列出 start_us;子 HTTP POST 200;error_detail_coverage raw_has_listed_error_details=false、0 可见字段 | e7 | e7:backend_counts_by_service.checkout = {132, error 0}。可见 14 span:10 个 PlaceOrder 的 start_us 与报告列出的 10 个值逐一相同,均 `rpc.grpc.status_code: 0`;2 个 HTTP POST `http.status_code: 200`;2 个 prepareOrder… 无 status_tags。error_detail_coverage 各字段与陈述一致 | OK |
| 5 | fact | frontend-proxy 日志 3 条 POST /api/checkout 全 200,无上游失败字段;trace ID 45ce…/189ab…/3fc16… 也是 checkout/ingress traces 返回的 ID;显示的 ingress 与 router frontend egress span 全 200 | e8,e7,e15 | e8:19 条显示,POST /api/checkout 恰 3 条(06:49:01.648、06:49:00.921、06:48:38.882),均 200,响应标志 "-"。3 个 trace ID 均在 e7 backend_trace_ids 与 sampled_spans 中,也在 e15 actual_visible_trace_ids 中(e15 view 自带标注,非模型跨 view 推断)。e15 可见 14 span 全部 `http.status_code: '200'` | OK |
| 6 | fact | cart 日志 20 条显示全部 Information(number 9),GetCartAsync/AddItemAsync/EmptyCartAsync,无 error/exception | e9 | e9:model_visible_hit_count 20,20 条 severity 全为 {Information, 9},body 仅这三种调用 | OK |
| 7 | hypothesis | 可见遥测与所有 PlaceOrder 成功一致(约 8.7 服务端 PlaceOrder、114.8 checkout span calls,"all status OK");仅是对已观察记录的陈述 | e2,e4,e5,e7,e8,e14,e15,e16 | 数值正确。但 span-metrics 的 status_code 是 `STATUS_CODE_UNSET`,不是 OK;"all status OK" 把 UNSET 与 gRPC code 0 混称为 OK。summary 与 claim 1 已正确写 UNSET,故不改变含义 | P3 |
| 8 | hypothesis | 若有失败只能在未显示记录中:checkout trace 496 中省略 482(可见 14)、frontend-proxy 318 中省略 304、无 checkout 日志源;是覆盖论证不是观察 | e7,e8,e15 | e7:496/482/14 一致。e15:318/304/14 一致。question 的 available_log_sources 仅 cart、frontend-proxy | OK |
| 9 | recommendation | 仅建议(未执行):接入 checkout 日志源并提高限额复查 482/496、304/318、133/152 | e7,e8,e15 | 482/496、304/318 见上;e8 backend_total_hits 152、可见 19,152−19=133 一致。为调查建议,非修复动作 | OK |

补充核对:
- 全部 9 条 claim 的 `time_scope_ref` = `m003c-normal-window`,与 `time-policies.json` 唯一 id 相同。
- 全部 12 个不同 `target_refs` 均存在于 `delivered-business.json` 的 `evidence_context/target_catalog`(如 54ec7… = checkout 容器 a57660d1e0e3,2e820… = cart 容器,eae22… = payment 容器,a0546… = 仅观察到 checkout 的集成级目标),没有编造的 target。
- 报告未把 `limits.source_query_limit=20` / `display_max_spans=20` 写成可见数;可见数均按 `actual_visible_*` / `model_visible_hit_count` 写。
- 累计 vs increase:所有引用的 PromQL 均为 `increase(...[5m])`,报告未引用任何原始累计值;`_bucket` 之和(101.08、131.03 等)未被当作请求数。
- 缺失 series:报告明确写 "unknown rather than zero",与 view 的 missing_series_semantics 一致;未把不存在的 checkout ERROR 序列写成显式 0。
- 省略 vs 缺失:482/304/133 均写为 "omitted / unshown",gap 2 明确 "not a population failure rate";未把投影省略说成遥测缺失。

## summary 与 gaps 核对

summary 中的数字:114.81(e2)、8.74(e14)、8.73 未出现于 summary 但出现于 claim 3(e6)、132 checkout spans(e7)、10 个 PlaceOrder span(e7)、3 条 POST /api/checkout 全 200(e8)、七条客户端序列(e4)、ERROR 序列四个服务各 0(e10/e11):全部可在对应 view 找到。
summary 的因果表述限于"每个可见信号显示成功""no window evidence of checkout failures in visible telemetry, not zero failures",与 claims 7/8 一致,没有在 summary 里放大到 claims 之外。
summary 明确 "No SLO or baseline … no health/recovery certification is made",与 question 要求一致。

gaps 五条逐一核对:
1. 无 checkout 日志源:与 question `available_log_sources=[cart, frontend-proxy]` 一致。
2. trace 样本偏置:10/496/14/482 与 20/318/7/14 与 e7、e15 一致;选择策略引文与 view 的 selection_policy 一致。
3. 日志覆盖:19/152、06:48:38.88–06:49:18.52(e8 首末时间戳 06:49:18.515698 与 06:48:38.882475);20/77、06:47:28.98–06:49:01.67(e9 首末 06:49:01.6670547 与 06:47:28.9811435)一致。
4. checkout 无 ERROR 序列 → unknown 而非 0:与 metric_semantics 一致。
5. 无 SLO/基线:与 question 一致。

gaps 没有声称看到看不到的东西;结构化 conclusion 为 `partial`,final_report.assessment_status 为 `completed`,与"完成但不确定"的区分相符。

## 与工程观察对照

| 项目 | 工程观察(m003-normal-observation.json) | 报告 | 一致性 |
|---|---|---|---|
| calls by status | 与 e10 20 条序列逐值相同(checkout UNSET 114.806;ERROR 四条为 0) | 同 | 一致 |
| app_payment_transactions_total increase | 8.7348 | 8.73 | 一致 |
| checkout client `_count` per method | 7 个方法均 code 0(8.735 / 13.73 / 22.46) | 报告用 `_bucket` 序列(e4)只说标签为 0,未引次数 | 方向一致,无冲突 |
| traces-checkout observed_spans | checkout 132 / error 0;全部 12 服务 error_spans=0;10 个 trace_id | e7 同集合,132/0 | 一致 |
| traces-payment | payment 20 / error 0;同 10 个 trace_id | e16 同 | 一致 |
| logs frontend-proxy total_hits | 152 | 152 | 一致 |

工程观察窗口内 error_spans 全为 0、无 ERROR 增量,报告"未观察到失败但不认证零失败"的方向与之一致;没有漏报的可见失败(e8 中唯一非 200 是一条 `GET /api/data/ 308` 重定向,与 checkout 无关,报告未提及也不构成漏报)。

## 发现清单

1. P3,`result-business.json` → `final_report.claims[6].text`(hypothesis):写 "all status OK"。实际 e2/e10 中 checkout span-metrics 的 status_code 为 `STATUS_CODE_UNSET`(非 OK),gRPC code 0 才是 OK。summary 与 claim 1 已正确写 UNSET,该表述不改变结论。
2. 观察(非缺陷):e7 可见 10 个 PlaceOrder span,而 e14 `_count` increase 为 8.74;报告 claim 7 写 "about 8.7"。increase 为外推值,view 的 window_semantics 已说明,报告未把两者等同,不计缺陷。
3. 观察(非缺陷):`result-business.json` 顶层 `assessment_status` 为 null,而 `final_report.assessment_status` 为 "completed";`quality_assessment` 为 "pending_independent_evidence_check"。这是外层状态字段,不影响报告内容核对。

未发现 P1、P2。

## 判定:PASS

无未处置 P1/P2;仅 1 条 P3 表达问题。报告的核心结论("可见遥测中无 checkout 失败证据,但不能断言零失败")由实际交付的 16 条 view 支持,未认证健康/恢复,未建议或执行修复,模型请求 3 次未超上限。

## 未确认

- 规范化 hash 只重算了 e2、e7、e8、e15、e16 五条(文件级 hash 已覆盖全部 16 条);其余 11 条的规范化 hash 未重算。
- 未核对 `delivered-business.json` 中送模输入原文与 `question-original.txt` 的逐字对应(超出本次"报告 vs view"范围)。
- 顶层 `assessment_status=null` 与 `final_report.assessment_status=completed` 的关系由宿主状态机决定,本次未能从只读文件确认哪一个是权威。
- 工程观察中 traces/logs 的 raw 文件(`tmp/m0-environment/raw-evidence/m003-normal/*.json`)未打开,只用了观察 JSON 内的摘要计数。
