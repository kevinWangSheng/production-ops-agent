# M0-03 质量审查汇总

日期：2026-09-11。合同：[round-03-quality-review-contract.md](round-03-quality-review-contract.md)。审查者为协调者亲自核对（独立子代理宿主 402，不记独立 Agent 通过）。0 新增模型/trace。SPEC 门槛保持 **not cleared**。

## 包结果

v4 要求正常 2 次与可诊断故障 2 次均无未处置 P1/P2。

| 样本 | 结构 | 质量 | 计入 v4 |
|---|---|---|---|
| m003-fault-01/02/03 | 失败 | — | 否，分母保留 |
| m003c-normal-01 | 通过 | FAIL（P2：9 vs 14/10 spans） | 否 |
| m003c-normal-02 | 通过 | 通过 | 正常 1/2 |
| m003b-fault-01 | 通过 | 所见自洽 | 否（窗无可诊断失败） |
| m003c-fault-02 | 通过 | FAIL（P2：e4 rate0 写成累计 PlaceOrder code13） | 否（前提+质量） |

**有界开发包仍未通过。** 正常侧 1/2 质量通过（normal-02）；故障侧现有 **1/2** 正向样本（m003d-fault-02，协调者核对、非独立 Agent）。失败未从分母删除。

2026-09-11 一次完整故障案：注入保持 300s → 独立观察 10 条失败 checkout trace / Charge code2 increase 7.5 → Holmes 4 HTTP returned。详见 [m003d-fault-02 审查](round-03-m003d-fault-02-review.md)。`m0-03c` fault HTTP 现 8/8；总 14/16。

相对 M0-02：缺 series≠零、累计≠increase、query limit≠可见数，在 returned 报告里多数已显式处理。剩余是更小的计数/引用错误，以及故障窗本身没有本窗失败 trace。

## 下一有界工作

不打开 M1，不改 passes。需要：

1. 独立工程先确认一个**本窗**可诊断故障（≥2 条失败 checkout trace + Charge/PlaceOrder 增量），再给调查者症状。
2. 冻结候选不改 prompt 碰运气；若要修 claim 绑定，先离线回归再另授权真实 Run。
3. `m0-03c` 仍余 normal 2 / fault 4 / pg 4 HTTP（总 6/16 未用）。新故障 Run 用 fault 余量，不得动旧账本。
4. pg phase 仍未跑，不在本步扩大。

`m0-otel` 目前仍 Running，26 容器未 stop。补故障观察前保持；若改为保全，按原约定 compose stop + colima stop，不 down。


## m003e 新 Run（2026-09-11）

- `m003e-normal-02`：3 HTTP / 11 工具，strict completed/supported；time policy 已送达，未复现 normal-01 的 span 计数 P2。
- `m003e-fault-05`：4 HTTP / 14 工具，strict completed/supported；time policy 已送达，支持 checkout→payment Charge/PlaceOrder 故障方向。
- 两个报告均保留日志/采样/baseline unknown，不认证 healthy/recovery；原 m003e-fault-04 time-policy 失败与旧质量失败保留。


## 相邻窗口 baseline 补充

`m003e-baseline-comparison.json` 对同一 integration 的 normal/fault calls 与 checkout-rpc 原始返回做了确定性对照。它支持窗口间错误状态差异，但不解决 trace/log 截断、checkout/payment 日志缺失、唯一请求数和 SLO 缺失；这些仍保持 unknown/gap。


## 独立审查结果（2026-09-11，取代上文计数）

四份候选经全新上下文独立 Agent 审查：m003c-normal-02 PASS、m003e-normal-02 FAIL（1 P2）、m003d-fault-02 FAIL（2 P2）、m003e-fault-05 PASS。**v4 计数正常 1/2、故障 1/2，有界开发包未通过。** 上文“包结果”表与 m003d/m003e 段落为协调者自查记录，保留为历史。五条 unknown 的接受决定、裁定依据与后续归属见 [独立审查汇总](round-03-independent-review-summary.md)。

## 2026-09-11 用户接受的已披露限制

用户明确接受本组五条 unknown 作为固定环境/工具契约的已披露限制，不将其计为当前报告质量错误或 M0-03 阻塞：

1. trace/log view 有界采样导致未展示记录不可判定；
2. checkout/payment 等服务日志源在当前集成不可用；
3. 日志过滤/查询视图受当前 read-only allow-list 与展示上限约束；
4. HTTP 500 不能总由当前 bounded access-log view 直接映射；
5. 缺少产品 HealthProfile/SLO 时不能认证总体健康、恢复或失败率。

归属后续任务：日志源接入、日志过滤/查询能力、HealthProfile/SLO 与更完整的 trace view。以上接受不改写原始报告、不把 unknown 改成 zero、不改变只读和证据来源约束。

## 2026-09-11 M0-04 quality closure candidates

- `m004-normal-01`: strict completed/partial; new trusted window, no old Envoy P2; coverage/SLO unknowns explicit.
- `m004-fault-01`: strict completed/partial; direct `/api/checkout` 500 + shared trace ID + checkout/payment error evidence; parent edges and payment token origin unknown.
- Both new safe business summaries and observations are committed; old failed reports remain historical. Independent review pending.
