# M0 账单对账模板（待用户提供账单）

用途：把工程 ledger 的请求、token、预留和 unknown 与供应商账单逐行对照。ledger 的 cost upper 是估算上界，不是发票；账单未提供前，差额保持 unknown，不释放预留。

| 日期/ allocation | 来源 ledger | HTTP | prompt tokens | completion tokens | known cost upper (CNY) | unknown reservation (CNY) | 供应商账单金额 | 差额/状态 | 账单证据路径 |
|---|---|---:|---:|---:|---:|---:|---:|---|---|
| 历史 M0-02 | `round-02-final-usage.json` | 20 | 360798 | 39606 | 1.438848（估算） | 6.88128 |  | unknown |  |
| 2026-09-12 B4 | 环境 worktree `m0-05-b4-ledger.json` | 3 |  |  | 0.004293 | 1.0 |  | unknown |  |
| 2026-09-12 B6 candidate | [`m0-06-b6-ledger.json`](m0-06-b6-ledger.json) | 2 | 603 | 253 | 0.004086 | 0 |  | unknown |  |
| 2026-09-12 protocol probes | [`round-06-protocol-ledger.json`](round-06-protocol-ledger.json) | 5 | 616 | 1376 | 0.014244 | 2.0 |  | unknown |  |
| 2026-09-12 upstream M004 | [`round-06-upstream-comparison-ledger.json`](round-06-upstream-comparison-ledger.json) | 2 | unknown | unknown | 0 | 4.0 |  | unknown |  |
| 2026-09-12 auth follow-up | [`round-06-upstream-auth-followup-status.txt`](round-06-upstream-auth-followup-status.txt) | 1 | unknown | unknown | 0 | 2.0 |  | unknown |  |

## 汇总（工程记录，不是供应商结算）

- 当前可提交树可回读的新增 HTTP：13（B4 3 + B6 candidate 2 + protocol 5 + upstream 2 + auth follow-up 1）；本任务新增 allocation 本身为 protocol 5 + upstream 2，未超过 12 HTTP / 12 CNY 任务上界。
- 可回读 known cost upper：至少 0.022623 CNY（B4 0.004293 + B6 candidate 0.004086 + protocol 0.014244）；auth/upstream usage 未捕获，unknown reservation 至少 7 CNY（不含历史）。
- 供应商账单、入账时间、缓存费率和账户聚合差额均待用户提供截图/导出后填写；不得用余额变化替代逐 Run 对账。
