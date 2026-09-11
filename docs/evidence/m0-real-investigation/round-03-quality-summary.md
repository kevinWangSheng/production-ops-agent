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
