# M0-07 收敛总结果（范围止于 M0）

日期：2026-09-12。当前任务新增 allocation 共 14 个模型 HTTP、0 trace，known cost upper `0.369123 CNY`，未超过总上界 30 HTTP/30 CNY；各项 ledger 见 [`round-07-m0-ledger-summary.json`](round-07-m0-ledger-summary.json)。

| 工作包 | 状态 | 关键证据 |
|---|---|---|
| WP3 控制/恢复 | **部分/有界通过** | wp23 PG 5 passed；真实 pause→resume 新 Run 1 HTTP，旧请求 `PAUSED` 拒绝、generation 2 response accepted。 |
| WP2 模型协议 | **部分** | compressor 真实超阈值 Run 1 HTTP/200、paired transcript 保真；stream/工具错误隔离 probe 已有历史证据，尚未接入产品 StepStore/PG 审计。 |
| WP5 上游/eval | **部分** | 同 replay tool face 下 candidate/upstream normal/fault 各 1 Run；candidate 有 JSON，upstream DSML 无最终报告。首次 holdout 两场各 10/10，仍非正式保留集。 |
| WP6 trace/平台 | **部分** | trace linkage 字段核查和白名单回归已完成；B4 真实出口尚未携带新增 subject/attempt，恢复导出仍缺。 |
| WP7 资源/费用 | **部分** | wall-time/清理上界有候选测量；账单与最终冻结值待用户。 |

失败与证据不足：wp5 candidate 首次启动参数转发失败（0 HTTP）已保留；wp23 真实 probe 首次 envelope allowance 失败（0 HTTP）已保留；upstream 两场没有最终报告；compressor keep floor 仍可能 over-threshold；K8s RBAC、Holmes 宿主 OS 隔离、正式 judge/盲测和供应商账单仍缺。

结论：M0 设计验证工件已集中到可回读终态，但“部分/证据不足”不能汇总成产品 M0 通过；SPEC gate 保持 `not cleared`，M1 未授权。
