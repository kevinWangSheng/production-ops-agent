# ITBench-Lite 冒烟：#676 调查指导，S19、S15 各 2 次

类别：集成运行，只看行为门，不按分数决定去留。依据 [任务记录](../../tasks/2026-11-16-676-investigation-guidance.md)。时间 UTC，2026-11-16。

- 代码：`feature/676-investigation-guidance` `e8b98507`，ext 档位。4 个 summary 的版本都是 `prompt-replay-candidate-k8s-f09a033198ca` / `otel-demo-ext-c63f058afb18` / `ctx-ctx-policy-v1-a30e65ea52f4`。
- 命令：`driver round --round lite-smoke-676 --scenarios S19,S15 --trials 2`，其余参数同全量；单进程，kind lab 停着。21:34:09–22:09:03，退出码 0；没有作废。

## 门

| 门 | 读数 | 结论 |
|---|---|---|
| ① 4/4 无 REPORT_INVALID | S19 t2 被产品拒收（两次终答都被拒，原因见下） | **不过**（3/4） |
| ② S19 的事件查询不带 namespace 或覆盖其他命名空间、结果带 `involvedObject.kind`，报告提到该类对象 | 两次都先不带 namespace，按 `involvedObject.kind`/`namespace`/`reason` 分组计数，再筛 NetworkChaos / PodNetworkChaos / Schedule（同样不带 namespace）；`root_cause` 两次都是 Schedule `chaos-mesh/otel-demo-email-checkout-network-partition` | 过 |
| ③ S15 读了 flagd-config | t1 用 configmaps 的 tabular 查询读出了全部 ConfigMap 的 data，summary 写到 flagd ConfigMap 里的 `adFailure`、`paymentUnreachable` 为 on；t2 用 jq 读了 `flagd-config` 的 `demo.flagd.json` | 过 |

lead 裁决（2026-11-16）：门① 不过记录在案。在基线拒收率（lite-full-1116 为 3/105）下，1/4 不构成退化证据；第 27 轮是 evidence id 抄错，与改动没有直接关系。因此进入下一阶段，集成批门① 继续兜底。

## 每个 trial

| trial | 结论 | 根因 | 模型轮次 | root_cause_entity_pass |
|---|---|---|---|---|
| S15/trial-1 | partial | Deployment / otel-demo / ad | 26 | 0.0 |
| S15/trial-2 | supported | Deployment / otel-demo / ad | 31 | 0.0 |
| S19/trial-1 | partial | Schedule / chaos-mesh / otel-demo-email-checkout-network-partition | 17 | 1.0 |
| S19/trial-2 | 产品拒收（REPORT_INVALID） | — | 27 | 0.0 |

## S19 t2 拒收原因（只读重放）

做法同[集成批记录](../itbench-lite-sre-lite-batch-676/run.md)「拒收原因」一节（trial 自己的 PG → `rebuild_transcript` → `validated_report`）。

- 第 26 轮：claim 8（fact）与 claim 25（counter_evidence）报 `cites_non_ok_view`。它们引用的是第 21 轮 `fetch_pod_logs` 的结果：对 quote pod 用 `error|fail|exception|warn` 过滤，状态是 `no_data`。模型把「过滤查询没有返回行」当成了 ok 证据。
- 第 27 轮（强制终答重试）：上一轮的问题已经改掉，但 claim 26（recommendation）报 `unknown_evidence_id`，引用的 id 有一位抄错了。
- PR #677 新增的句子里没有一句涉及 no_data、日志过滤结果或「没有错误」这类证据该怎么写。所以判断为已有的失败模式，不是新句子引起的（推断）。

## 费用与证据

DeepSeek 合计缓存命中 6,571,264、未命中 809,949、输出 236,182，北京时间闲时，约 CNY 1.89。入库的是 4 份 summary 与 `scores.json`，秘密值搜索与 sha256 核对同集成批记录。
