# 工具面拓展 A5：Prometheus 即时查询、序列与全部标签

- 状态：实施中（实现已在本地分支提交，待合同测试合入与 CI）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r3 的 A5-1 至 A5-8）、C3 第 8 节；上游 HolmesGPT `a0009195` 的 `prometheus/prometheus.py`；Prometheus `v3.5.0` API 文档。issue #557。开工前预审在仓库外（A4、A5、B2 预审，2026-11-12），A5 没有需要用户裁决的项。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-a5-impl`，分支 `feature/tool-expansion-a5-prometheus-instant`（起点 `origin/main` `989940a8`）。

## 目标

调查能做 PromQL 即时查询、按选择器列出时间序列、列出全部标签名。三个新工具只进扩展档位 `otel-demo-ext`；现有 `metrics_range_query` 与 `otel-demo` 档位不变。lab 的 Prometheus 只读代理已经放行这三个接口，沿用现有只读账号，不需要新凭据。

## 行为（公开接口）

- `execute_prometheus_instant_query(query, time?)`：在某一时刻算一次 PromQL。不给时刻时取查询窗的终点（告警 Run 的默认窗终点，否则授权时间框终点），不是「现在」；给了就必须在授权框内。表达式回看的区间不能早于授权框起点，`offset` 和 `@` 照旧拒绝。超时固定 30 秒。结果太大时整条拒绝，给出摘要和 `topk` 建议，口径与区间查询相同。
- `get_series(match, start?, end?)`：按选择器列出时间序列，`match` 必填。
- `get_all_labels(match?, start?, end?)`：列出标签名，`match` 可选。
- 序列与标签固定最多 100 条；返回恰好 100 条或 Prometheus 带了告警时标不完整。不给时间时默认 1 小时窗，与现有发现工具一致。

参数、时刻规则、条数与错误分类以决定表 A5-1 至 A5-8 为准。

## 这个 PR 做什么

- 新模块 `opspilot/tools/prometheus_ext.py`（B2 之后也进这里）：三个工具的 schema 与注册。新工具名**不加进**参与 `otel-demo` 版本哈希的 Prometheus 工具集合。基本认证推荐给现有传输加一个「哪些工具走基本认证」的构造参数（默认值不变），少复制一份 HTTP 代码；两种做法都不动 `otel-demo` 字节（A5-7）。
- 扩展档位的 Prometheus 可用工具加这三个名字。
- 合同测试对着替身 Prometheus 响应写，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码。

## 不做

POST 表单查询、`timeout` 参数、按指标限条数、改现有 `metrics_range_query`、评测回放。

## 完成条件

1. 代码 PR：合同测试与 CI 通过，`otel-demo` 钉值不变。这是功能 PR。
2. 代码 PR 合并后 #557 关闭。真实证据归租机集中取证（#580）：租机后的真实 Run 里出现即时查询或序列、标签发现的证据，报告引用；仓库里有 `summary.json` 与 LangSmith trace 链接。

## 没有真实实例时的已知风险

1. Prometheus v3.5 截断序列与标签结果时是否也加告警（未确认，所以两种信号都认）。
2. `get_series` 在宽选择器下的体积：100 条标签集一般不超 25,000 token，未实测。

## 依赖 A2 的假设

扩展档位、按工具名分派的传输路由与数据源拼装由 A2 带出；A5 不加评测回放分支。全量探针未跑：A2 合并后开草稿 PR 经 CI 补跑，预计只加扩展档位、0 失败。

## 实施记录

探针骨架提交 `8e7fc6e5` 只把三个工具名放进扩展档位。本地定向跑出 2 个失败，都是 A2 钉死「扩展档位只比 otel-demo 多两个工具」的断言：`tests/test_tool_expansion_a2_contract.py:350`（独立合同测试，实现者不改）和 `tests/test_554_kubernetes_core_impl.py:421`（A2 单元测试）。`otel-demo` 钉值 `otel-demo-a3a95a0f888e` 没变。CI 全量由 lead 跑。

自行决定的可逆细节：

- 「恰好 100 条标不完整」用 `TransportResponse` 新加的 `incomplete` 字段表达，与注册的 `warnings` 标记取或，非 bool 记 MALFORMED_RESULT。原样响应字节里没有这个信号，改写字节又会破坏「原始证据是原样响应」。代价是动了执行器的一个可选字段，默认 False，现有工具不受影响。
- 传输按 A5-7 给 `OtelDemoTransport` 加了 `prometheus_tools` 构造参数，默认 `PROMETHEUS_TOOLS`。它同时管基本认证、503 timeout 判超时和 400/422 错误回显，A5-8 要求这三条一起生效。新工具由 `prometheus_ext.PrometheusExtTransport` 复用同一个传输实例的 GET，经 `RoutedTransport(routes=...)` 按名字分派。
- 即时查询：`time` 只收带时区的 ISO-8601（与 start/end 一致，unix 秒数与无时区的写法都拒绝）。时刻越出授权框、回看早于框起点、`offset`/`@`，都在发请求前拒绝，码为 `QUERY_OUT_OF_WINDOW`（INVALID_PARAMS），拒绝里带授权框。有回看时视图的 `window` 是 `[时刻-回看, 时刻]`，并给出 `lookback_seconds`/`lookback_start_at`；没有回看时 `window` 保持授权框，因为 `Window` 不收零长区间。两种情况都另加视图字段 `evaluation_time`。查询含 calls_total 时，与区间查询一样附 `series_note`。
- scalar/string 结果的来源时间取结果里那一个时间戳；vector/matrix 沿用 `_prometheus_bounds`。
- 超限摘要：即时查询复用区间查询的 `overflow_summary`（摘要算在 `query` 参数上）；列表工具的行单位记为 `series` 和 `label_names`。
- 列表拒绝码沿用发现工具的 `MATCH_INVALID`、`QUERY_OUT_OF_WINDOW`。

已知限制：错误分类只登记了 400/422/503，与发现工具一样；401/403/404 落到注册表默认的 `SOURCE_ERROR`。上节两条真实实例风险仍未确认。

## 下一步

A2 合并后第一个开工（A5 最小、无新语义）：开 worktree，派测试作者与实现者，按 AGENTS.md 的功能 PR 流程走。
