# ITBench-Lite 冒烟：r3 发布，S8、S14、S16 各 3 次

类别：集成运行，只看行为门（r3 验收计划阶段 1）。代码 `b0652dbd`（主机本地分支 `r3-combined` = PR-G `ea2bc41b` + PR-R `719919e7`，代码树与 PR-G 最终 HEAD `d065c41e` 相同，见[全量记录](../itbench-lite-sre-lite-full-r3/run.md)）。9 个 summary 的版本都是 `prompt-replay-candidate-k8s-1137251eea4d` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。时间 UTC，2026-11-17。

- 命令：`driver round --round lite-smoke-r3 --scenarios <题> --trials 3`，3 个进程各跑一题（端口偏移 0/+100/+200），19:40:13–20:00:16，退出码都是 0，没有作废。词表、kinds 旁路与分档在本轮重新生成（35 份，`tiers.json` 与 1117 相同）。
- 同机条件：kind lab 开着，F3 尚未开跑。swap 全程 0。

## 门

| 门 | 读数 | 结论 |
|---|---|---|
| 设置题（S8、S14）6 次里 ≥ 4 次根因不是被模板未变重启的 Deployment | 6/6：根因都是 ConfigMap `otel-demo/flagd-config`。每次都读到了开着的故障设置（S8 `paymentUnreachable=on`；S14 `adFailure=on`、`paymentFailure=100%`），把重启记为模板未变（restartedAt 注解、镜像相同），部署作为竞争假设或机制保留 | 过 |
| 没有一次在没读到「设置点名对象 = 被重启对象」时写 supported | 6 次都是 partial，0 次 supported | 过 |
| S16 3/3 不退 | 3/3：Deployment `otel-demo/shipping`，supported | 过 |
| REPORT_INVALID 0 | 0/9 | 过 |

| trial | 结论 | 根因（kind / namespace / name） | 终答轮 | root_cause_entity_pass |
|---|---|---|---|---|
| S8/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 2 | 1.0 |
| S8/trial-2 | partial | ConfigMap / otel-demo / flagd-config | 2 | 1.0 |
| S8/trial-3 | partial | ConfigMap / otel-demo / flagd-config | 1 | 1.0 |
| S14/trial-1 | partial | ConfigMap / otel-demo / flagd-config | 1 | 1.0 |
| S14/trial-2 | partial | ConfigMap / otel-demo / flagd-config | 1 | 1.0 |
| S14/trial-3 | partial | ConfigMap / otel-demo / flagd-config | 2 | 1.0 |
| S16/trial-1 | supported | Deployment / otel-demo / shipping | 1 | 1.0 |
| S16/trial-2 | supported | Deployment / otel-demo / shipping | 2 | 1.0 |
| S16/trial-3 | supported | Deployment / otel-demo / shipping | 1 | 1.0 |

分数只记录，不作门。首答被拒（终答轮 ≥ 2）4/9，重试后全部通过。`load_seconds` 49.5–81.8，`gap_seconds` ≤ 5.005。

json_mode：没有逐个回读 LangSmith span。间接核对：#686 的 loop 对每个 `round-K` 请求都设 `json_mode=True`，只有压缩请求为 False；9 个 ledger 都没有压缩步，9 次首个终答都以 `{` 开头。

DeepSeek：缓存命中 10,364,032、未命中 1,081,392、输出 347,302，北京时间 11-18 03:40–04:00（闲时），约 CNY 2.68。秘密值搜索与 sha256 核对同[全量记录](../itbench-lite-sre-lite-full-r3/run.md)「证据与秘密值」。
