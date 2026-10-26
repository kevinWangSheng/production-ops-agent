# F2 PR-E 复用证据清单

本次 kind 会话只补预审 §2 的 K1–K5；下列证据按合同 §5「E 只补未覆盖项」与 E-R1、E-R3 复用。每份都按它当时的 commit 与 revision 解读，**不是**在 PR-E HEAD（`b5276a9`，产品代码同 main `036fb10`，`prompt-replay-candidate-bef70649a3a8` / `otel-demo-68ed2ca8f5d6`）上取得的。

| 步骤 | 证据 | commit | prompt / tool_schema revision | 结论 |
|---|---|---|---|---|
| 1 重复告警 `replayed` | [m1-04-intake-live](../m1-04-intake-live/run.md) | `495004c` | 见原 run.md | 通过 |
| 1 重复告警 `replayed`（手工重投） | [f12-live](../f12-live/run.md) 08:29:09 | `f497424` | 见原 summary | 通过（用户 2026-10-16 接受手工重投） |
| 1 再 firing 1a/1b/1c（关闭后新事故带关联） | [f2-alert-correlation-live](../f2-alert-correlation-live/run.md) | `91541db` | `dd9018031487` / `otel-demo-4d7168180749` | 1a、1b、1c 通过；断言 2 当时失败（#304），本次 K1 补跑通过 |
| 1 部署事件附加到开放告警事故（U2-A / F11 U8） | [f11-pr4b-live](../f11-pr4b-live/run.md) S4 | `5943b0f` | `bef70649a3a8` / `otel-demo-68ed2ca8f5d6` | 通过（靠关 pass 控序）。本次 K5 两轮在默认 30 s、不控序下自然复现了这条路径 |
| 1 重复发布 webhook `replayed` | [f11-pr3-release-intake-live](../f11-pr3-release-intake-live/run.md) | `68221a2` | 见原 run.md | 通过 |
| 1 不同并发事故分开 | [f2-b2-live](../f2-b2-live/README.md) | `f98e9aa` | `dd9018031487` / `fixture-3` | 通过（12 条不同 fingerprint 各自建事故与 Run） |
| 2 断点 ① 模型在途 | [f2-kill-resume-live/model_in_flight](../f2-kill-resume-live/model_in_flight/summary.json) | `424a6bf` | `dd9018031487` / `otel-demo-2a944edd110a` | 通过 |
| 2 断点 ② 响应已提交、工具未派发 | [f2-kill-resume-live/response_committed](../f2-kill-resume-live/response_committed/summary.json) | `264a271` | `dd9018031487` / `otel-demo-2a944edd110a` | 通过 |
| 2 断点 ③ 工具在途（并行 2） | 原轮 [tool_in_flight](../f2-kill-resume-live/tool_in_flight/) FAIL 保留；#332 修复后 [tool_in_flight-332](../f2-kill-resume-live/tool_in_flight-332/summary.json)；无效轮 `-r1-invalid` 保留 | `a41622b`（即 #337 合入内容） | `2f30c2380b23` / `otel-demo-68ed2ca8f5d6` | 复跑通过（M2 授权要求引用此复跑） |
| 2 #242 压缩（单独断言，非门槛） | [242-compaction-live](../242-compaction-live/README.md) | 见原 README | 见原 README | 两次真实 Run 通过 |
| 3 查询卡住（PR-A） | [f2-query-deadline-live](../f2-query-deadline-live/run.md) | `ab34df1` | `dd9018031487` / `otel-demo-3a6a3ded3d76` | 通过；早于 #309 并行与 #337，本次 K2 在当前代码、并行 2 下补跑 |
| 4 告警风暴（非 kind 真实风暴，U9） | [f2-b2-live](../f2-b2-live/README.md)；[f2-b1-live](../f2-b1-live/README.md) | `f98e9aa`；`818fdab` | `dd9018031487` / `fixture-3` | 通过 |
| 5 全局 / 按目标暂停时在途轮 `late_result` | [f7-step4-live](../f7-step4-live/run.md) | `b272bdb` + 未提交探测期望修正（非产品代码） | 见原 summary | 通过 |
| 5 接管后、运行中 close 后 `late_result` | [f12-live](../f12-live/run.md) | `f497424` | 见原 summary | 通过；事故级 pause/cancel 的在途迟到写回由本次 K3、K4 补齐 |

revision 前缀 `dd9018031487`、`2f30c2380b23`、`bef70649a3a8` 分别是 `prompt-replay-candidate-<hash>`。
