# ITBench 现场版全量（#866，2026-11-25 轮 `full-1125`）

结果标签：ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）。与 ITBench-Lite 分轨、分证据、分结果。

## 主机与版本

- 主机 `opspilot-tool-1`（8 vCPU / 32 GB）；OpsPilot main `78771840`（PR #869 合并后，未改动）；ITBench `5ed31cc4`；评测器 `ITBench-Evaluations@14f026fc`，judge Z.ai；模型 `deepseek-flash`。
- kind 集群 `kind-environment`（1 控制面 + 2 worker）。`opspilot-m1` 已停止，保留，所以加了 `--allow-other-clusters`。
- 范围：24 个可运行 SRE 场景 × 1 遍（用户 2026-11-25 定）。评测侧 namespace 标签补丁开启，每题披露 `EVAL_SCRAPE_NAMESPACE_PATCH`（用户 2026-11-25 定）。
- 时间（PT）：主轮 2026-11-25 10:34 → 11-26 01:11；S40、S63 重试 01:15 → 01:58。每题约 25–31 分钟（部署到清理结束），S102 因判分重试约 110 分钟。
- 每题都是新部署环境，结束时 `remove faults` 与 `undeploy` 全部成功，无清理错误，整轮没有中止。

## 结果

| 口径 | root_cause_entity_pass | 说明 |
|---|---|---|
| 全部 24 题 | **14/24（58.3%）** | 未判分、未调查、注入失败都计 0 |
| 去掉 2 道注入失败题（故障没注入，OpsPilot 未参与） | 14/22（63.6%） | 两题都重试一次，失败方式相同 |
| 有官方分的 19 题 | 14/19（73.7%） | 官方 root_cause_entity_f1 均值 0.833，root_cause_reasoning 均值 0.868 |

19 题有官方分时：确定性旁证 any_hit 均值 0.895、F1 均值 0.816；propagation_chain 与 fault_localization 均为 0（受答案转换限制，与 11-24 冒烟相同）；修复类指标不适用（只读产品）。

未得分的 5 题：

| 题 | 状态 | 原因 |
|---|---|---|
| S40 | `infra_failed: ITBENCH_INJECT_FAILED` | 上游 `inject faults` 返回 1（第 1 次 64 s、重试 64 s）。部署成功、清理成功；故障未注入，没有 Run。重试前的 summary 保存为 `S40/trial-1/attempt-1-summary.json` |
| S63 | `infra_failed: ITBENCH_INJECT_FAILED` | 同上（第 1 次 4.5 s、重试 3.5 s）。重试前 summary 为 `S63/trial-1/attempt-1-summary.json` |
| S44 | `stopped: NO_ENTRY_ALERT` | 注入成功，到清理为止没有新的入口告警，没有 Run |
| S116 | `stopped: NO_ENTRY_ALERT` | 同上 |
| S102 | completed，判分失败 | Run 正常完成（Run `15154f33`）。官方评测器 3 次都没给出 `root_cause_entity_f1`（`OFFICIAL_KEYS_MISSING`，每次约 28 分钟）。按 2026-11-15 用户裁决，重试用尽仍缺主指标的记缺失、计 0、留在分母 |

注入失败的原因未核实：上游日志含答案信息，按答案隔离不读。

## 逐题（有 Run 的 20 题）

诊断耗时为自投递起；故障起点代理为 `inject faults` 命令结束时刻。

| 题 | 入口告警 | 诊断耗时 | 官方 root_cause_entity_f1 | root_cause_reasoning | 旁证 F1 | token | 披露 | Run / trace |
|---|---|---|---|---|---|---|---|---|
| S20 | KubePodNotReady | 80 s | 1.0 | 1.0 | 1.0 | 479,892 | 告警名回退 | `4d6da588` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-abdc-ddcca1a8293a?poll=true) |
| S23 | KubePodNotReady | 80 s | 1.0 | 1.0 | 1.0 | 510,585 | 告警名回退 | `3ec878e6` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-2717-14ee556926b9?poll=true) |
| S30 | HighRequestErrorRate | 140 s | 1.0 | 1.0 | 1.0 | 1,220,999 | | `a3f69e6f` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-3134-9d123a0e0fc6?poll=true) |
| S31 | HighRequestLatency | 120 s | 1.0 | 1.0 | 0.667 | 1,046,801 | | `d0390b8d` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-294c-7e4f74f04e5e?poll=true) |
| S33 | KubePodNotReady | 75 s | 0.667 | 1.0 | 0.667 | 448,114 | 告警名回退 | `a891f571` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-992a-030699bf5b86?poll=true) |
| S34 | HighRequestErrorRate | 205 s | 1.0 | 0.5 | 1.0 | 2,013,148 | | `b6db9dd0` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-e992-815782148562?poll=true) |
| S39 | KubePodNotReady | 145 s | 0.0 | 0.0 | 0.0 | 788,671 | 告警名回退 | `45e688f9` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-3bc3-af4428aac4ef?poll=true) |
| S42 | NoRequestsReceived | 255 s | 1.0 | 1.0 | 1.0 | 2,558,735 | | `62c8763f` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-381a-2dcf776eec4a?poll=true) |
| S43 | HighRequestErrorRate | 150 s | 1.0 | 1.0 | 1.0 | 1,252,273 | | `cbdc104c` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-883e-6000caf38d50?poll=true) |
| S45 | KubeContainerWaiting | 110 s | 1.0 | 1.0 | 1.0 | 649,674 | 部署重试、告警名回退 | `0c8ce18f` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-62e3-e40c464eddfe?poll=true) |
| S46 | HighRequestErrorRate | 115 s | 1.0 | 1.0 | 1.0 | 876,504 | | `cd675fb8` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-8d89-e49e481415e1?poll=true) |
| S49 | KubePodNotReady | 80 s | 1.0 | 1.0 | 1.0 | 490,548 | 告警名回退 | `691729bc` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-2d87-5c34d9006959?poll=true) |
| S56 | KubePodNotReady | 95 s | 1.0 | 1.0 | 1.0 | 513,331 | 告警名回退 | `b56ce232` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-fb1e-911421245053?poll=true) |
| S57 | HighRequestErrorRate | 130 s | 1.0 | 1.0 | 1.0 | 801,606 | | `9bf659f4` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-a506-7c80e689bd63?poll=true) |
| S58 | NoRequestsReceived | 125 s | 1.0 | 1.0 | 1.0 | 575,142 | | `6737f979` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-0ef5-c30126705a72?poll=true) |
| S59 | KubeContainerWaiting | 95 s | 0.667 | 1.0 | 0.667 | 409,623 | 告警名回退 | `ea04f451` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-c47b-f151c073a015?poll=true) |
| S60 | HighRequestErrorRate | 160 s | 0.5 | 1.0 | 0.5 | 905,245 | | `96f6d42a` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-524d-92b9c4a833ea?poll=true) |
| S102 | HighRequestErrorRate | 180 s | 未判分 | — | — | 1,078,779 | | `15154f33` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-efd8-856980b2f57c?poll=true) |
| S105 | KubePodCrashLooping | 145 s | 1.0 | 1.0 | 1.0 | 854,886 | 部署重试、告警名回退 | `ab37b823` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-6f4e-c248f941bb68?poll=true) |
| S114 | HighRequestErrorRate | 406 s | 0.0 | 0.0 | 0.0 | 4,340,430 | | `6fd4561f` [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/3426d6bb-c9b3-4fce-8973-33b59f513848/r/00000000-0000-0000-93f9-7417f3d757da?poll=true) |

- LangSmith 项目 `opspilot-lab-itbench-live-full-1125`；20 个 Run 的 trace 都已回读（`read_back: found`）。
- 「告警名回退」= `TARGET_FALLBACK_ALERTNAME_MATCH`（告警不带 `service_name`，目标按 alertname 匹配）。「部署重试」= `DEPLOY_RETRIED`（首次部署失败，自动重试一次后成功）。
- 每题都带披露 `EVAL_SCRAPE_NAMESPACE_PATCH`、`JAEGER_V1_API_ABSENT_V3_ONLY`、`LIST_FAILED:endpointslices`、`LIST_FAILED:limitranges`（只读 RBAC 不含这两类资源，未放宽）。
- 全部 20 个 Run 的最终状态是 `completed`。

## 费用

- DeepSeek（token × 单价估算）：20 个 Run 共 prompt 21,271,628（缓存命中 19,188,864、未命中 2,082,764），completion 543,358。按 LangSmith 内置 `deepseek-flash` 条目（输入 $0.15、缓存读 $0.003、输出 $0.60 每百万 token，不分峰谷）约 **$0.70**。不含 Z.ai judge。
- 主机机时：本轮约 15.5 小时（10:34 → 01:58 PT）；租金以用户账单为准。

## 已知限制（照实披露）

- 主分只来自官方 judge 的 `root_cause_entity_f1`；确定性旁证只作对照。
- 每题 1 遍，没有重复试验，单题结果有波动可能。
- 评测侧 namespace 标签补丁偏离上游默认；补丁后有一条请求延迟告警在注入前就持续 firing，被当作注入前噪声排除。
- 2 道题入口告警没有出现，2 道题注入失败，原因都未核实（答案隔离）。
- 20 个 Run 中 17 个至少有一次工具结果被产品的大小保护拒收（`RESULT_TOO_LARGE`，按 summary 中 `reason` 计数）。产品照常继续调查，但这部分数据没进上下文；是否影响得分未分析。
