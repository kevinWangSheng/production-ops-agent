# 独立审查:m003e-normal-02
审查者:独立 Agent(全新上下文),0 模型请求,只读。

## 核对范围
- 读取:`result-business.json`(final_report、finish_reason=stop、assessment_status=completed、conclusion=supported、tool_queries=11)、`time-policies.json`、e1–e11 的 `-tool-model-view.json` / `-manifest.json`、e5 的 `-raw.json`(仅用于确认 Envoy 日志字段是否有结构化标注)、`delivered-business.json`(仅提取 target_catalog 与 view_bindings,用于核对 target_refs)、`question-original.txt`。
- 未读取:`.env`、`private-protocol/`、任何 injector/answer/flag/experiment 文件、round-03 审查结论。
- 判据:v4 包"确定性与报告判据"第 1–5 条。
- 工程观察:`m003e-normal-observation.json`(窗口 1789128210–1789128510)。

### Manifest 哈希重算
按 manifest 中声明的算法 `sha256(json.dumps ensure_ascii=False sort_keys=True UTF-8)` 重算 e1–e11 全部 11 组 raw/view 的 canonical sha256,以及文件字节 sha256(`raw_file_sha256` / `view_file_sha256`):**22 项全部一致**(超过要求的 3 组)。view_bindings 里的 view_hash 与 manifest 的 view_sha256 一致(e2 抽查:155c6871…)。

### time_scope_ref / target_refs
- `time-policies.json` 仅有一个 id `m003e-normal01-window`(2026-09-11T12:03:30Z–12:08:30Z)。全部 10 条 claim 的 time_scope_ref 都等于该 id。
- target_refs 不在 view 文件内,而是由宿主在 user 消息中以 `target_catalog` + `view_bindings` 交付。逐条比对 claim.target_refs 与其引用 evidence 的 view_bindings.target_refs:全部是绑定集合的子集(e2/e3/e7/e8→1e26…;e6→9a5c…;e4→afa6…+162d…;e5→253c…;e11→a81b…;e9→28e9…;e10→65af…+3367…)。无来自未交付 view 的 target。

## 逐 claim 核对表
| # | kind | 报告陈述(摘) | 引用证据 | view 中实际 | 判定 |
|---|---|---|---|---|---|
| 1 | fact | checkout STATUS_CODE_ERROR rate=0、UNSET=0.5157/s,评估点 1789128510,ERROR 系列存在为显式零 | e2 | vector 两条:UNSET 0.5157319、ERROR "0",value 时间 1789128510;查询为 rate(...[5m]) 瞬时求值,与 metric_semantics 一致 | OK |
| 2 | fact | PlaceOrder(server) code=13 rate 0、code=0 0.0374/s;client Charge code=2 rate 0;其它下游成功 0.037–0.108/s | e7, e3 | e7:PlaceOrder 2.0.2 code0=0.03743、code13=0;e3:Charge code2=0,GetCart/EmptyCart/GetQuote/ShipOrder/Charge 0.03743、GetProduct 0.0707、Convert 0.1081 | OK(文中"status_code=0"应为 rpc_grpc_status_code=0,仅表述,P3) |
| 3 | fact | 集成范围 ERROR 系列返回的 6 个服务 rate 均 0;未返回系列的服务是 unknown 不是零 | e6 | 恰好 6 条:load-generator/frontend/payment/frontend-proxy/recommendation/checkout,全为 "0";无其它服务系列 | OK |
| 4 | fact | trace 工件源窗口 12:04:28.124Z–12:08:03.924Z,所有服务 error_spans_by_listed_status_tags=0,checkout 124 span / max 107,315µs,raw_has_listed_error_details=false;9 trace/462 span 返回,14 可见,448 省略 | e4 | backend 9 trace / 462 span;checkout 124 / 0 / 107315;12 个服务全部 error=0;raw_has_listed_error_details=false;visible 14、omitted 448;source_start/end 与 binding 一致 | OK |
| 5 | fact | frontend-proxy 19 行(19 of 157),12:07:57.942Z–12:08:29.188Z,两条 POST /api/checkout 均 200,trace a679…(**upstream 713 ms**)、6cef…(**upstream 1501 ms**);两 trace id 也在 checkout trace 集合中 | e5, e4 | total_hits 157 eq、returned 20、visible 19、时间范围一致;两条 checkout 行状态 200、trace id 一致且均在 e4 backend_trace_ids 中。但日志行为 `… 200 - via_upstream - "-" 386 713 83 81 …` 与 `… 382 1501 143 141 …`:按 Envoy 默认访问日志格式,713/1501 是 BYTES_SENT,83/143 是 DURATION(ms),81/141 是 upstream service time(ms)。view 与 raw 均无对这几个字段的结构化标注,报告把发送字节数当成了 upstream 毫秒 | **P2**(数值语义错读;不影响"200 成功"这一核心含义) |
| 6 | fact | cart 日志 20 of 73,12:07:18.915Z–12:08:03.899Z,全部 Information,无错误;含 trace 6cef…/a679…,与 e4 checkout trace 匹配 | e11, e4 | total 73 eq、returned 20、visible 20;20 行 severity 全为 Information(9);两 trace id 各出现 2 行;均在 e4 backend_trace_ids 中。跨证据关联仅基于 view 中可见的 trace_id 字符串相等,未声明 parent 关系 | OK |
| 7 | counter_evidence | increase(calls_total{checkout}[5m]) 在窗口末=154.72,外推、分数,不是请求数 | e8 | 一条:checkout 2.0.2 = 154.71957;查询确为 increase(...[5m]);语义声明与 metric_semantics 一致 | OK |
| 8 | fact | payment app_payment_transactions_total rate 0.0291/s;payment trace 样本 14 可见 / 18 返回,源窗口 12:04:28.147Z–12:08:03.885Z,Charge span 均 rpc.grpc.status_code 0,无 error_details | e9, e10 | e9:payment 0.02911(结果无 status_code 标签,报告未据此声称状态,OK);e10:payment span_count 18、visible 14、omitted 448(backend_returned_span_count 462 为全 trace 总数);14 个可见 span 全为 payment,7 个 Charge status 0、7 个 charge 无 status 标签,error_details 均空;源窗口与 binding 一致 | OK("18 返回"指 payment 的 backend span_count 而非 backend_returned_span_count=462,表述略松,P3) |
| 9 | hypothesis | 暂定:checkout 请求本窗口很可能无失败;明确为"查询信号内的阴性结果",非健康/恢复认证 | e2,e7,e3,e4,e5 | 各引用项与上表一致;已标 hypothesis,未升级为 fact | OK |
| 10 | recommendation | 仅建议(未执行):去掉 20 行上限重查、按错误状态过滤 trace、补基线/SLO | e5,e11,e4 | 建议内容与 e5/e11 的 limit 20 newest-first、e4 的 selection_policy 相符;无修复动作 | OK |

## summary 与 gaps 核对
- summary 数字:0.516/s UNSET vs 0 ERROR(e2 ✓)、PlaceOrder code13=0 / code0=0.037/s(e7 ✓)、Charge code2=0 / 成功 0.037/s(e3 ✓)、checkout 124 span / 107.3 ms(e4 ✓)、两条 POST /api/checkout 200(e5 ✓)。summary 未复述 713/1501 ms 的错读。
- summary 的定性("supports 'no failure evidence found in the queried signals', not a health certification")与 claim 9 一致;结构化 claims 里没有与 summary 相矛盾的陈述。
- gaps:
  1. "No checkout application logs exist in this environment":question-original 的 available_log_sources 仅 cart/frontend-proxy,且 limits 写明"logs unavailable for some services"。"不存在"略强于"本环境未提供",P3。
  2. 19/157、20/73、14/462(448 省略)均与 view 一致;把省略正确写为"未证明无错",没有把投影省略说成遥测缺失 ✓。
  3. raw_has_listed_error_details=false、raw_detail_span_count=0 ✓(e4/e10 均如此)。
  4. 未返回 ERROR 系列的服务列为 unknown ✓;"recommendation-only in the aggregate check"指 recommendation 的 ERROR 系列只在 e6 出现,与 claim 3 不矛盾,表述含糊,P3。
  5. 无基线/SLO,未认证健康/恢复 ✓。
  6. increase 154.72 外推、无请求数分母 ✓。
- 未见模型声称看到了未交付内容;所有引用的数字均能在对应 view 中定位。

## 与工程观察对照
| 项目 | 工程观察(increase[5m] @1789128510) | 报告(rate[5m] @同点 ×300) | 方向一致 |
|---|---|---|---|
| checkout calls_total ERROR | 0 | 0 | ✓ |
| checkout calls_total UNSET | 154.72 | 0.5157×300=154.72 | ✓ |
| checkout→Charge code2 | 0 | 0 | ✓ |
| checkout→GetCart 等 code0 | 11.23 | 0.03743×300=11.23 | ✓ |
| app_payment_transactions_total | 8.73 | 0.02911×300=8.73 | ✓ |
| traces-checkout observed_spans | 12 服务 error_spans 全 0,checkout 124 | e4 error=0,checkout 124 | ✓ |
| traces-payment | payment 18 span, error 0 | e10 payment 18 / 0 | ✓ |
| logs-frontend-proxy total_hits | 157 | 157 | ✓ |
- 工程观察 calls 里 load-generator/frontend/payment/frontend-proxy/recommendation/checkout 的 ERROR 系列均为 0,其它服务确实没有 ERROR 系列,与报告 claim 3 的"返回/未返回"划分完全一致。
- 工程观察窗口内没有可见失败(error_spans 全 0,无非 2xx 采样),报告没有漏报;也没有把不存在的失败写进报告。

## 发现清单
1. **P2(claim 5)**:frontend-proxy 两条 `POST /api/checkout` 的 "upstream 713 ms" / "upstream 1501 ms" 是对 Envoy 访问日志字段的错读。按行内顺序(`bytes_received bytes_sent duration upstream_service_time`),713/1501 是发送字节数,实际 duration 为 83 ms / 143 ms,upstream 81 ms / 141 ms。view 与 raw 都没有为这些位置字段提供标注,模型给了一个未经字段标注支持的单位与语义。该错误不改变"两次 checkout 返回 200"的结论,也未进入 summary,但它是 fact 类 claim 中可观察的数值/来源错误,按 v4 第 3/5 条属 P2。
2. P3(claim 2):"status_code=0" 应写 rpc_grpc_status_code=0。
3. P3(claim 8):"14 visible spans of the 18 returned" 混用了 payment 的 backend span_count(18)与 backend_returned_span_count(462);上下文可辨。
4. P3(gap 1):"No checkout application logs exist in this environment" 略强于证据("本环境未提供 checkout 日志源")。
5. P3(gap 4):"recommendation-only in the aggregate check" 表述含糊。
6. 正向确认:无健康/恢复认证、无修复执行、无发布门禁;hypothesis/fact/counter_evidence/recommendation 分类正确;缺失系列写为 unknown;累计/increase/rate 未混淆;省略未写成遥测缺失;跨 trace 关联仅基于可见 trace_id。

## 判定:FAIL
依据 v4 第 5 条"任一未处置 P1/P2 → FAIL":发现 1 项未处置 P2(claim 5 的 713/1501 ms 数值语义错读)。核心结论("查询信号内未发现 checkout 失败证据,非健康认证")本身由交付证据支持,与工程观察一致;该 FAIL 是单点数值错误,而非结论或权限/状态问题。若审查方裁定"view 未标注字段语义,模型解析不可从可见内容判定"从而降为 P3,则本报告其余全部 OK,可改判 PASS;本审查按判据字面执行,记 FAIL。

## 未确认
- Envoy 访问日志字段顺序按 Envoy 默认格式(`%BYTES_RECEIVED% %BYTES_SENT% %DURATION% %RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%`)推断;本环境 frontend-proxy 的实际 access_log 格式配置未读取(在环境仓库中,超出本次只读范围),故 713/1501 的具体字段名为推断,"不是毫秒延迟"这一判断的置信度较高但未由配置文件直接证实。
- 未核对 `model_http_requests` 的逐条请求体与 `report-capture-business.json` 的 hash 绑定(任务未要求)。
- 未读取 `observations.json` 全量与 `deployment-registry.json`;target_catalog 的 container_id/config_revision 与 e4 source_identity_table 一致(a57660d1e0e3…、8183839e…)仅做了抽查。
