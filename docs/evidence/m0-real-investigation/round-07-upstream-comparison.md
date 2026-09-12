# 工作包 5：M004 上游与候选同条件比较结果

执行日期：2026-09-12。依据 [`round-07-upstream-comparison-contract.md`](round-07-upstream-comparison-contract.md)。使用冻结 replay packet，无新故障注入、无 m0-otel 查询、无 trace 上传；normal/fault 各执行候选与 Holmes upstream 1 Run，每 Run 最多 2 HTTP。

## Run 矩阵

| 场景 | 组别 | Run | HTTP | 结果 | 费用上界（CNY） |
|---|---|---|---:|---|---:|
| normal | candidate | `m004-normal-r07-candidate-retry2` | 2 | `final_returned`，严格 `m0-report-v1` JSON | 0.059514 |
| normal | upstream | `m004-normal-r07-upstream` | 2 | `final_returned` 外壳，但内容为 DSML tool-call，无最终报告 | 0.051687 |
| fault | candidate | `m004-fault-r07-candidate` | 2 | `final_returned`，严格 `m0-report-v1` JSON | 0.064128 |
| fault | upstream | `m004-fault-r07-upstream` | 2 | `final_returned` 外壳，但内容为 DSML tool-call，无最终报告 | 0.053256 |

第一次 normal candidate 启动因 launch 参数转发错误退出（0 HTTP），原始失败保存在 `m004-normal-candidate-launch.txt`；修复隔离 launcher 后用新 Run ID 重跑，未把失败从 HTTP 分母删除。

Ledger [`round-07-wp5-ledger.json`](round-07-wp5-ledger.json)：8 HTTP、known cost upper `0.228585 CNY`、trace 0；每次 HTTP reservation 1 CNY，未超过本节 8 HTTP/8 CNY 上界。

## 逐 claim 并排

| 场景 | 候选可观察报告 | upstream 可观察结果 | 差异披露/处置 |
|---|---|---|---|
| normal：窗口是否出现 checkout 失败 | 返回 partial JSON，引用 8 个 replay evidence，明确“未观察到失败”而非健康认证，并列出采样/SLO gaps | 两次 HTTP 后仍停在 DSML `otel_logs`/`otel_metrics` 请求，无最终报告 | 两方 packet hash 相同；候选 prompt/tool schema hash 与 upstream Holmes 配置不同，且 upstream report protocol 未形成。结果进入失败分母，不比较质量分。 |
| fault：失败依赖方向 | 返回 partial JSON，基于 checkout/payment/Charge 与 500 视图提出有界 hypothesis，保留 parent edge/采样/内部原因 unknown | 两次 HTTP 后仍停在 DSML 查询请求，无最终报告 | 两方 packet hash 相同、工具查询顺序不同；upstream 无可评分 claim。不能计算候选优于 upstream。 |
| 工具面 | replay `otel_services/metrics/logs/traces`，非法查询返回受限错误 | 同名 replay toolset + Holmes planner，默认 shell/curl/kubectl/网络未加载 | 工具集合目标相同，但 Holmes system prompt/调用格式不同；差异作为实验因素记录。 |
| 费用/请求 | 4 HTTP，known upper `0.123642 CNY` | 4 HTTP，known upper `0.104943 CNY` | 费用只能按工程上界记录，供应商账单仍 unknown；不以成本抵消质量缺口。 |

## 配置与证据哈希

- normal packet：`ac80c078b6e13f53792943d404e7424eb5dfc8808b4bd796d297306a4e99c7ff`；fault packet：`61ca39b0bf29c111dd73ee65efd83616de96f2d95169d4dca25a618d91c3fd28`。
- upstream normal/fault config hash：`ca4eccd5f77048d16c2f621a5efbf5c8245d198cd57214fcaa49562cdb10e004` / `e5c11c3c2aed346e6cc3099d0f14797ac2408fca6d0eb18d8ec55309f2db678c`。
- candidate runner hash：`4d7b890d37113f8a0771b2d735712b5956f675f13ceab70c54780a8d5370f3c4`；candidate normal/fault result hash：`91ea1e11284120e277425617de0e9b29b1a54fbec37f3016d9ab5187551ed694` / `06578ae047e1517321ef0890b9fcbac052999f849d7274674515af0bb59750ae`。

## 结论

本节完成了**同一 replay packet、同一受限工具面、各 2 HTTP** 的可执行比较协议；候选两场形成可评分 JSON，上游两场均无最终报告，故正式质量差异、非退化和优劣结论均为 **证据不足**。失败/工具协议不匹配进入分母；不修改 SPEC gate 或 feature passes。
