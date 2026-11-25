# ITBench 现场版 SRE 冒烟（#564，2026-11-24 轮）

结果标签：ITBench 现场版 SRE（自托管，自测，不与官方榜单并列）。与 ITBench-Lite 分轨，不合并分数。冒烟不计分，只验证评测设施。

## 主机与版本

- 主机：`opspilot-tool-1`，Ubuntu 24.04 x86_64，8 vCPU / 32 GB，开跑前可用内存约 29 GB，swap 未用。
- 同机其他集群：`kind-sregym` 已按 lead 指示删除；`opspilot-m1` 节点容器已停止（`Exited`），保留不删。因此 preflight 加了 `--allow-other-clusters`。
- 评测设施：分支 `chore/564-itbench-live`，主机检出 `70ee4ff5`（PR #852 首个提交）。冒烟之后的 Jaeger 探测修复不影响本轮数据，见下文「失败与修复」。
- ITBench `5ed31cc4bef041af7f61120811ebff7f36612e4b`。mise `2026.11.13`；kind 0.33.0、cloud-provider-kind 0.12.0、helm 4.3.2、uv 0.13.9 按上游 mise 配置安装。`mise trust` 时 mise 改写了 `mise.lock`，HEAD 不变。
- 评测器 `ITBench-Evaluations@14f026fc`（重新克隆，按 `uv sync --frozen` 安装）。judge：Z.ai `glm-5.3-flashx`，与官方示例 judge（gemini-2.5-pro）不同。
- 产品版本：`prompt-replay-candidate-k8s-068c56342888`，`otel-demo-ext-e00b0e7d95c9`，DeepSeek。

## 集群

上游 `mise -E kind run create:kind:environment` 在装 Cilium 时失败：helm 没拿到新集群的 kubeconfig，回落到 `localhost:8080`。这是环境问题：上游任务假定 shell 里已经导出了 `KUBECONFIG`。处理办法是导出 `KUBECONFIG=clusters/kind/kubeconfigs/kind-environment` 后，手动补跑该任务剩下的两步：helm 装 Cilium 1.20.5、`approve_kubelet_csrs.py`。之后 3 个节点全部 Ready，context 为 `kind-kind-environment`。cloud-provider-kind 在后台运行。它对停止的 `opspilot-m1` 报错，不影响本集群。冒烟后集群已删除，provider 已停止。

## 逐题

时刻均为 UTC（2026-11-25）。每遍都从部署新环境开始，结束时 `remove faults` 加 `undeploy`，两步都成功。

| 题 | 部署 | 注入 | 告警到达（自注入结束） | Run | 清理 | 状态 |
|---|---|---|---|---|---|---|
| S20 | 437 s | 4 s，rc 0 | 5 分 45 秒（轮询 24 次） | 115 s，`completed` | remove 4 s + undeploy 406 s | completed |
| S23 第 1 次 | 430 s | 4 s，**rc 1** | — | — | 4 s + 408 s | `infra_failed: ITBENCH_INJECT_FAILED` |
| S23 第 2 次 | 418 s | 4 s，**rc 1** | — | — | 4 s + 406 s | 同上 |

S20：
- Run id `2f51c09e-5dea-5baa-93f3-c6bfa2b5c078`。[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/6701a471-5a6e-435f-bd1c-cea93d704682/r/00000000-0000-0000-0398-5c7c9d75f734?poll=true)，项目 `opspilot-lab-itbench-live-2026-11-24`。
- 诊断耗时：自故障起点代理 468 s，自投递 115 s。
- token：prompt 739,838，其中缓存命中 696,960；completion 19,349；共 16 轮。
- 入口告警是 `KubePodNotReady`。该告警不带 `service_name`，所以目标按 alertname 退回匹配，已标 `TARGET_FALLBACK_ALERTNAME_MATCH`。注入前已在响的告警组合有 7 个，均已排除。
- 报告结论 `supported`，报告格式为结构化根因，词表 203 个名字。

S23：第 1 次失败后，把它的 summary 移到旁边（本目录 `S23/trial-1/attempt-1-summary.json`），只重跑这一题。第 2 次失败方式完全相同，可以判定是确定性失败，不是偶发。

## 判分（只报诊断类指标）

| 题 | 官方 root_cause_entity_f1 | 官方 pass | root_cause_reasoning | propagation_chain | fault_localization | 旁证精确匹配 any_hit / F1 |
|---|---|---|---|---|---|---|
| S20 | 1.0 | 1.0 | 1.0 | 0.0（受转换限制） | 0.0 | 1.0 / 1.0 |
| S23 | 未判分（没有 Run） | — | — | — | — | — |

计数：2 题中 1 题有分（`scores.json`）。修复类指标不适用于只读产品，没有计算。

## 失败与修复

1. **S23 注入失败（未归类）。** 上游 `itbench inject faults --scenario 23` 两次都在约 4 秒内返回 1。原因写在 CLI 日志里，但按答案隔离规定不读那份日志，只按模式归类的做法也被本会话的权限拒绝了，所以目前无法判定是环境问题还是上游问题。排除了设施本身的问题：同一流程 S20 注入成功，部署和清理都成功，can-i 探针也没有不符。
2. **Jaeger 只提供 v3 查询接口（环境与产品的集成缺口，设施已修正记录方式）。** 上游 Jaeger v2 用 ClickHouse 存储，`/api/services`、`/api/traces` 返回 404，只有 `/api/v3/services` 能应答；`/jaeger` 前缀下返回的是界面 HTML。产品的 trace 工具调用 v1 接口，所以 S20 的 `traces_list_services` 和 `traces_search` 都报 error。trace 在本轨记为「未集成」（A564-9）。设施的修改已推到 PR #852：识别到只有 v3 时记 `JAEGER_V1_API_ABSENT_V3_ONLY`，并从 v3 取服务名补进词表。产品没改。
3. **只读身份不能列 endpointslices 和 limitranges（环境，不扩权）。** kind lab 的 RBAC 清单没有这两类资源，词表少了这两类对象名，已记 `LIST_FAILED`。

## 未核实假设的结果

- 上游全量部署（含 agents、recorders）能成功，单次约 7 分钟。**成立。**
- agents 步骤会影响我方 RBAC 清单和 port-forward。**未发生。**
- 需要 cloud-provider-kind。**未判定**：一直在后台运行，没做不运行的对照。
- Prometheus pod 名和 `svc/jaeger-collector` 名正确。**成立**；Jaeger 路径前缀问题见上文第 2 条。
- 入口告警带 `service_name`。**不成立**：S20 的首条新告警是 `KubePodNotReady`，走了退回匹配。
- `inject faults` 返回时故障已经生效：S20 注入后 5 分 45 秒出现告警，与规则 `for: 5m` 相符。
- 评测器能直接读 live 答案格式。**成立**：S20 官方判分有完整数值。

## 费用

DeepSeek 按 token × 单价计，约 1 个 Run，费用很小。Z.ai judge 判了 1 次。主机机时约 1 小时（07:30–08:32）。

## 答案隔离

执行者没有打开 `upstream-*.log`、ITBench 的 `.itbench/runs/`，也没有打开 `library/scenarios/` 下的任何文件。判分进程的输出已丢弃。原始 ledger 在主机仓库外，不入库。
