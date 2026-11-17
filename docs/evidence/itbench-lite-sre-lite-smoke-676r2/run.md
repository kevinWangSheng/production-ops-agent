# ITBench-Lite 冒烟：#676 合同 r2，S19、S15 各 2 次

类别：集成运行，只看行为门。代码 `feature/676-investigation-guidance` `d96823fc`，ext 档位。4 个 summary 的版本都是 `prompt-replay-candidate-k8s-0b066c7cd6d1` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。时间 UTC，2026-11-17。

- 命令：`driver round --round lite-smoke-676r2 --scenarios S19,S15 --trials 2`，回放端口各 +200，与[集成批](../itbench-lite-sre-lite-batch-676r2/run.md)的两个进程并行。01:21:56–01:47:56，退出码 0，没有作废。

## 门

| 门 | 读数 | 结论 |
|---|---|---|
| 无 REPORT_INVALID | 0/4 | 过 |
| S19 不带 namespace 读事件、按 `involvedObject.kind` 分组，报告提到该类对象 | 两次都先不带 namespace 按 kind/namespace/reason 分组计数，再筛 NetworkChaos / PodNetworkChaos / Schedule；`root_cause` 两次都是 Schedule `chaos-mesh/otel-demo-email-checkout-network-partition` | 过 |
| S15 读 flagd-config | 两次都读了 `flagd-config` 的 `demo.flagd.json` | 过 |

| trial | 结论 | 根因 | 模型轮次 | root_cause_entity_pass |
|---|---|---|---|---|
| S15/trial-1 | supported | Deployment / otel-demo / ad | 25 | 0.0 |
| S15/trial-2 | supported | Deployment / otel-demo / ad | 30 | 0.0 |
| S19/trial-1 | partial | Schedule / chaos-mesh / otel-demo-email-checkout-network-partition | 22 | 1.0 |
| S19/trial-2 | partial | Schedule / chaos-mesh / otel-demo-email-checkout-network-partition | 16 | 1.0 |

首答重试 4/4，强制重试后全部通过。首答被拒原因：JSON 解析失败 2 次（S15 t1 前有文字，S19 t1 有 ```json 围栏），`cites_non_ok_view` 1 次（S15 t2，引用的是带过滤的 `fetch_pod_logs` no_data 结果），缺 `next_steps` 1 次（S19 t2）。

DeepSeek：缓存命中 5,270,016、未命中 753,595、输出 188,046，北京时间高峰，约 CNY 3.22。秘密值搜索与 sha256 核对同集成批记录。
