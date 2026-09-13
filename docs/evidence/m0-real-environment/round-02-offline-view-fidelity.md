# M0-02 额度耗尽后的离线视图保真修复

状态：**offline-only；本轮fault与normal03完整质量FAIL保持不变，真实质量待新授权验证**。本工作项未调用模型、trace或后端，未操作环境、账本或Git index。上限保持14000bytes/view、32768输出、512KiB请求及最多4HTTP/Run；不创建typed-fact平台，不按故障类别路由。

已观察缺口：报告将实际14个可见span写成cap20；raw存在但未投影的error detail被当成遥测缺失；已显示某条trace的细节被推广到未显示trace；缺失ERROR series被解释成0。这里只修明确的投影/说明缺口，不声称修复已提高模型质量。

## 新视图

- `m0-02-traces-v3`：backend_returned_trace_count/span_count与actual_visible_trace_count/span_count分开；omitted_span_count、具体limits、item/byte两类截断标志独立。backend聚合明确是返回raw范围统计，不能推断所有span细节均已展示。
- 按query.service优先，其次已列error status、duration，最后稳定trace/span ID顺序；这是通用相关源优先且有偏的样本。保留选中span的parent refs，并标parent_is_visible。只显示选中span涉及的身份表；未映射仍unknown。
- 对明确列出的error-description/exception/grpc错误字段保留每span最多4个、每字段最多600 UTF-8 bytes；标value_truncated/original_utf8_bytes、被省略字段数。error_detail_coverage分别给raw存在/字段数、已交付/省略/截断数量并列covered_keys。精确错误文字仅属于对应显示span/trace；省略字段不是遥测不存在。
- `m0-02-metrics-v2`：新增missing_series_semantics，明确缺失/空series不是零，ERROR系列缺失只能unknown/unobserved。原query/result原样保留。

新细节占用固定字节预算，故故障view显示span数量减少：checkout7、payment5；对应保留14/20个具体诊断字段。这是明确的细节与覆盖量取舍，不是扩大容量，不能称完整调用图。

## 离线验证

`tests/test_m0_trace_view.py` 的计数/细节回归先红（旧view缺backend_returned_span_count），实现后绿；与原定向套件合计49 passed，Ruff通过。真实Holmes+httpx.Request假pipe 3/4步回归仍通过，0真实HTTP，输出`round-02-offline-view-wire-probe.txt`。

`tests/fixtures/m0_environment/trace_fidelity_replay.py` 只读既有fault/normal03 raw及各自registry：四份trace逐一核原v2整个对象完全相同、新计数/字段raw前缀/截断/14000byte上限/源优先、旧raw bytes不变；两Run全部metric原v1也重放相等。结果见`round-02-offline-trace-fidelity.json`：

| 实际旧raw | backend traces/spans | 新可见spans | 新view bytes | raw/显示诊断字段 |
| --- | --- | --- | --- | --- |
| fault e8 checkout | 11/349 | 7 | 13755 | 143/14 |
| fault e9 payment | 11/349 | 5 | 13381 | 143/20 |
| normal03 e11 checkout | 5/248 | 14 | 13497 | 0/0 |
| normal03 e12 payment | 5/248 | 13 | 12963 | 0/0 |

旧22a96完整源码快照仍保留。`legacy_projections.py` 增加明确`trace_projection_v2`与`metric_projection_v1`；当前wrapper支持version显式选择历史算法，不对旧raw/view重签或静默升级。旧log v2也继续兼容。新七个runtime源码按实际SHA256保存在runtime-sources/只读文件，清单`round-02-offline-view-source-manifest.json`；旧同hash文件仅核对不覆盖。

本候选交独立审查；没有新的真实模型成绩，也未改本轮任何报告或通过标志。
