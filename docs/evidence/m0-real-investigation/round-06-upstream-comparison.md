# B6 M004 上游与候选对照结果

执行日期：2026-09-12。依据 [`round-06-upstream-comparison-followup-contract.md`](round-06-upstream-comparison-followup-contract.md)。本轮共 2 个确认到模型内容的 HTTP（normal 1、fault 1），无 trace 上传；ledger 保留 4 CNY unknown reservation。由于 Holmes 在单步响应中只生成了 shell tool-call，没有形成最终报告，不能计算质量差异或“候选优于上游”。

## 逐 claim 对照

| 场景/claim | 候选已有报告 | Holmes 上游本轮 | 差异披露与处置 |
|---|---|---|---|
| M004 normal：checkout 窗口错误状态与证据缺口 | 候选报告支持“可见证据未发现失败”，并披露采样、日志源、SLO gaps | 无最终报告；模型只请求读取本地/环境信息，未执行工具 | 输入问题、模型 alias、固定 checkout 相同；上游没有可比较 evidence view，结果进入失败分母。 |
| M004 fault：checkout→payment Charge 失败方向 | 候选报告支持可见 Charge/PlaceOrder 错误与 unknown 边界 | 无最终报告；模型只请求 `cat /tmp/.holmes`/`curl`，未执行工具 | 上游工具面与候选 strict proxy 不匹配；不计算因果/质量分。 |
| 证据可复核性 | 候选有 raw/view/hash/独立复核 | 上游未产生 report/raw/view | 只能记录协议前提已到达模型，不能比较 claim 分数。 |

## 原始工件与账本

- normal stdout/stderr/status：[`round-06-upstream-normal.stdout`](round-06-upstream-normal.stdout)、[`round-06-upstream-normal.stderr`](round-06-upstream-normal.stderr)、[`round-06-upstream-normal.status`](round-06-upstream-normal.status)；stdout SHA-256 `e88f83cfdb920815c79d61845c10bfdbea6d87ca1230c16feacf4589c2a71771`。
- fault stdout/stderr/status：[`round-06-upstream-fault.stdout`](round-06-upstream-fault.stdout)、[`round-06-upstream-fault.stderr`](round-06-upstream-fault.stderr)、[`round-06-upstream-fault.status`](round-06-upstream-fault.status)；stdout SHA-256 `b2a94ed2c8c901f3c5b6b3a917cf529aba119da4e20b757156c8ed17a1ae5e4d`。
- ledger：[`round-06-upstream-comparison-ledger.json`](round-06-upstream-comparison-ledger.json)，2 HTTP、0 known cost、4 CNY unknown reservation、trace 0。

## 结论

本轮只证明固定 Holmes checkout 能通过私有 DeepSeek 认证并返回模型内容；toolset 初始化与未执行 shell 请求使两场景均无法形成最终报告。B6 正式同条件比较、judge 校准和保留集盲测仍未完成；失败进入分母，未下非退化或收益结论。
