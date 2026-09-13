# M0-03 四份候选报告的独立审查汇总（2026-09-11）

四份 `investigation_returned` 候选各由一个全新上下文的独立 Agent 只读审查：不读协调者既有审查、不读 `.env` / `private-protocol/` / 注入答案，0 模型请求，判据为 [v4 包](../../testing/first-investigation-v4-2026-09-10.md)五条与 P1/P2/P3 定义。协调者只做裁定与一手来源复核，不改写审查文件。

| Run | 侧 | 独立审查 | 未处置 P1/P2 | 文件 |
|---|---|---|---|---|
| m003c-normal-02 | 正常 | PASS | 0（1 P3） | [审查](round-03-independent-review-m003c-normal-02.md) |
| m003e-normal-02 | 正常 | FAIL | 1 P2 | [审查](round-03-independent-review-m003e-normal-02.md) |
| m003d-fault-02 | 故障 | FAIL | 2 P2 | [审查](round-03-independent-review-m003d-fault-02.md) |
| m003e-fault-05 | 故障 | PASS | 0（4 P3） | [审查](round-03-independent-review-m003e-fault-05.md) |

**v4 计数：正常 1/2、故障 1/2。有界开发包未通过。** 此前 quality-summary 的“包结果”表停在 m003e 之前，且所有 round-03 审查为协调者自查；本文件取代该计数。

## 协调者裁定

- m003e-normal-02 的 P2：报告 claim 把 Envoy 访问日志行中的 713/1501 写成 “upstream 713 ms / 1501 ms”。按 pinned 源码 `opentelemetry-demo@63649d6` 的 `src/frontend-proxy/envoy.tmpl.yaml` 访问日志格式 `%BYTES_RECEIVED% %BYTES_SENT% %DURATION% %RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%`，713/1501 是 bytes_sent，时延为 83/143 ms。view 未给字段标签，但 fact claim 断言了它无法支持的数值，属 v4 第 5 条 P2。不降级为 P3，不削弱判据。
- m003d-fault-02 的两处 P2：跨 trace 关联声明了 checkout view 中不可见的 trace；以省略 317 span 的 payment view（5 可见）断言“无下游子调用”。均为 v4 第 3 条点名的可见范围错误。核心定位（payment Charge → checkout grpc 13 → HTTP 500）与工程观察一致，但按判据 FAIL。
- 两份 FAIL 都不重跑、不改 prompt 让旧报告变绿；保留分母。

## 报告中保留的五条 unknown 的处置

用户 2026-09-11 决定：五条 unknown 作为本有界开发包的已披露限制接受；接受不等于关闭。核对依据与后续归属：

| unknown | 根源（已核） | 后续归属 |
|---|---|---|
| trace/log bounded sample | 只读代理契约 20 条 / 1 MiB；报告 visible/omitted 与 raw 一致 | 无 |
| checkout/payment 日志源缺失 | 环境 instrumentation gap（environment-results.md 第 18/28 行） | M1 环境任务：为 checkout/payment 接 OTLP 日志 |
| HTTP 500 无直接 access-log | m003e 故障窗 126 条代理日志只取最新 20 条，其中 0 条 /api/checkout；模型与工程侧同样看不到；m003d 窗 20 条内恰含 2 条 500 | 离线工具项：代理日志接口增加 path/status 有界过滤，先离线回归，不为此重跑 |
| 唯一请求数 / 失败率 | span-metrics increase() 外推固有；trace ID 给下界 | 无 |
| 无 SLO/baseline | v4 冻结不新增 SLA/HealthProfile；相邻窗口对照仅开发证据 | M1 任务：HealthProfile 定义（SPEC 恢复观察前提） |

## 附带发现（不影响判定）

- m003e-fault-05 的 `question-original.txt` 同时含 `requested_window` = m003e 窗口与残留的 `window` / `scope_revision=m0-03d-v1-fault01` / `run_id=m003e-fault-01`。可信 time policy 与全部工具查询均使用 m003e 窗口，claims 的 time_scope_ref 一致，报告未受影响；属 round03 runner 拼装输入的卫生缺陷，需在下次真实 Run 前修正并加回归。
- 审查者 P3 与未确认项见各审查文件。

## 对 SPEC gate 的影响

gate 保持 **not cleared**。报告质量一项由“待判定”变为“正常 1/2、故障 1/2，未达 v4”。下一步须另行授权：先离线修 runner 输入拼装与（可选）代理日志过滤，再在新合同下补 1 次正常 + 1 次故障真实 Run，并对新 Run 做独立审查；不重用旧账本。
