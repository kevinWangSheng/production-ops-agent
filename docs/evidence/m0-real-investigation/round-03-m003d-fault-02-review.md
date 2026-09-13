# M0-03 m003d-fault-02 一次完整故障案

协调者执行并核对。独立子代理仍不可用。注入答案未进入调查输入。LangSmith 未上传。

## 前提（同一执行，发模型前）

窗 1789120620.138–1789120920.138（2026-09-11T09:57:00Z–10:02:00Z）。`paymentFailure` 保持 100% 满 300s（experiment `m003d-fault-01`），不是上一轮同秒 restore。

独立观察 [m003d-fault-observation.json](../m0-real-environment/m003d-fault-observation.json)：Charge code2 **increase=7.5**、code0=0；checkout 失败 trace **10** 条，含 Charge code2 / PlaceOrder 13 / HTTP 500。可诊断前提成立后才发模型。

## 调查结果

- `investigation_returned`，4 HTTP / 16 工具，`m0-03c` 账本 ordinal 11–14 均 200。
- 报告 SHA-256 `395c8e3fd0c92dcd…` 与 capture 一致；13 个 cited view hash 全部匹配。
- `time_scope_ref=m003d-fault01-window`。restore 后 flag 已 off。

核心定位与独立观察一致：本窗 checkout→PaymentService/Charge code2 increase 7.5、PlaceOrder code13 increase 7.5、payment 交易 increase 0；可见链 HTTP 500。e7 ERROR increase checkout=15 / payment=7.5 / frontend-proxy=15。e5：10 traces / 322 spans / **7 visible / 315 omitted**，checkout error_spans=20。e12：161 hits / 19 visible。报告按这些数写，未把 limit 20 当可见数，累计与 increase 分开。未把 flag 名当结论。

## 分层

- 结构通过。
- **可诊断故障正向样本 1/2**：本窗失败 trace 与依赖定位有据。
- 协调者核对未见 P1/P2 计数/窗口混淆。仍不是独立 Agent 终审，也不是整个 v4 包通过（还缺第二个故障 Run；normal-01 仍 FAIL）。
