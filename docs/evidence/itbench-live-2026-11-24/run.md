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

S23：第 1 次失败后，把它的 summary 移到旁边（本目录 `S23/trial-1/attempt-1-summary.json`），只重跑这一题。第 2 次失败方式完全相同，可以判定是确定性失败，不是偶发。第 2 次的 summary 现在保存为 `attempt-2-summary.json`；`summary.json` 是补上 configure 之后的重跑。

## 判分（只报诊断类指标）

| 题 | 官方 root_cause_entity_f1 | 官方 pass | root_cause_reasoning | propagation_chain | fault_localization | 旁证精确匹配 any_hit / F1 |
|---|---|---|---|---|---|---|
| S20 | 1.0 | 1.0 | 1.0 | 0.0（受转换限制） | 0.0 | 1.0 / 1.0 |
| S23（补上 configure 后重跑） | 1.0 | 1.0 | 1.0 | 0.0（受转换限制） | 0.0 | 1.0 / 1.0 |
| S30（替补 1，补上 configure 前后各一次） | 未判分（两次都没有入口告警，没有 Run） | — | — | — | — | — |
| S31（替补 2） | 未判分（没有入口告警，没有 Run） | — | — | — | — | — |

计数：冒烟目标 2 题（S20 + S23），**2/2 有分**（`scores.json` 记了 4 题，其中 2 题有分）。S23 原来的失败原因是设施流程缺了一步，补上后重跑通过，见文末。修复类指标不适用于只读产品，没有计算。

## 替补题（2026-11-25）

- **为什么换题：** S23 在上游注入这一步确定性失败，冒烟目标「2 题 × 1 遍，每题都跑完并判分」没有达到。S23 照旧记为原因未归类的上游注入失败，日志不读。
- **选题规则：** 沿用任务记录的非答案标准，按 id 升序取 23 之后的下一个可运行 SRE 场景。依据只有公开场景表和设施 preflight 按目录名列出的可运行清单，不打开场景库。公开表里 23 之后是 24，但它没有场景目录、跑不了，所以选 **30**。上限是 2 个替补，按 lead 规定只有替补也在注入步失败时才继续往下换。
- **版本：** 设施为 `main` `7d0d49ef`（含 PR #852 与 Jaeger v3 修复），主机新克隆。ITBench 仍是 `5ed31cc4`。集群按上游任务新建，这次先导出了 `KUBECONFIG`，建集群一次成功。

| 题 | 部署 | 注入 | 告警等待 | Run | 清理 | 状态 |
|---|---|---|---|---|---|---|
| S30 | 436 s | 4 s，rc 0 | 20 分钟内没有新的 firing 告警（轮询 81 次） | 没有 | remove 4 s + undeploy 407 s | `stopped: NO_ENTRY_ALERT` |

S30 的注入成功了，但等待期内没有出现「注入前不在响」的新 firing 告警。注入前已在响或 pending 的告警组合有 8 个，按设施规则都被排除。可能的原因有两种：一是故障引起的告警与注入前已在响的告警是同一组标签，被排除了；二是故障在 20 分钟内没有触发任何告警规则。设施没有记录注入前告警的具体标签，所以本轮判断不了是哪一种（未核实）。这不是注入失败，因此按 lead 的规则没有继续换第 2 个替补，停下汇报。

S30 跑完后已删除 ITBench 集群，停掉了本任务的 cloud-provider-kind 进程。`opspilot-m1` 始终处于停止状态，没有动过。

## 失败与修复

1. **S23 注入失败（当时未归类；后来查明是设施缺了 configure 步骤，见文末）。** 上游 `itbench inject faults --scenario 23` 两次都在约 4 秒内返回 1。原因写在 CLI 日志里，但按答案隔离规定不读那份日志，只按模式归类的做法也被本会话的权限拒绝了，所以目前无法判定是环境问题还是上游问题。排除了设施本身的问题：同一流程 S20 注入成功，部署和清理都成功，can-i 探针也没有不符。
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

## 第 2 个替补：S31（2026-11-25）

- **选题：** 同一规则，30 之后的下一个可运行 SRE 场景是 31。这是 lead 规定的最后一个替补，不论失败方式如何，跑完即停。
- **版本：** 设施为 `main` `df0f5a49`，主机新克隆，设施代码没有改动。ITBench 仍是 `5ed31cc4`。先导出 `KUBECONFIG` 再按上游任务建集群，一次成功（10:14–10:15 UTC）。

| 题 | 部署 | 注入 | 告警等待 | Run | 清理 | 状态 |
|---|---|---|---|---|---|---|
| S31 | 451 s | 4 s，rc 0 | 20 分钟内没有新的 firing 告警（轮询 81 次） | 没有 | remove 4 s + undeploy 407 s | `stopped: NO_ENTRY_ALERT` |

S31 与 S30 情况相同：注入成功，等待期内没有出现「注入前不在响」的新 firing 告警。注入前已在响或 pending 的告警组合有 5 个，都被排除。原因仍是那两种可能之一，设施没有记录被排除告警的标签，所以判断不了（未核实）。

结论：两个替补都是同一种失败，即注入后设施没有拿到入口告警。这是设施的入口告警规则和这些场景之间的问题，不是注入失败。全量（24 × 3）之前，应先让设施记录被排除的告警标签组合以及它们注入后 `activeAt` 是否变化，以判断是排除规则误伤还是告警规则根本没有触发。这一项由 lead 决定是否开 issue。

S31 跑完后已删除 ITBench 集群，检查并停掉了本任务的 cloud-provider-kind 进程（PID 1733478、1733421）。`opspilot-m1` 仍处于停止状态，没有动过。

## 补上 `itbench configure environment` 后重跑 S23 与 S30（2026-11-25）

- **原因（lead 经用户授权，由隔离 Agent 诊断）：** 设施流程在建好 kind 集群之后没有执行上游的 `itbench configure environment`。这一步负责给 sandbox worker 节点打 `itbench.io/sandbox=true:NoSchedule` 污点。少了它，S23 的注入会停在上游通用的 worker 节点检查上，什么都还没应用。这是环境配置缺口，与故障内容无关。之前的「失败与修复」第 1 条据此更正为：原因已查明，属于设施问题，已修复。
- **修复：** PR #863（分支 `chore/564-itbench-configure-env`，`9105aa3e`）让设施在每遍部署之前先执行 `itbench configure environment`，再核对至少有一个节点带这个污点，没有就停在部署前，记 `SANDBOX_TAINT_MISSING`。子命令的依据只有上游 CLI 的 `--help`。两遍都标记了 `kind-environment-worker`。
- **版本：** 主机新克隆该分支。集群先导出 `KUBECONFIG` 再按上游任务新建，一次成功（11:03 UTC）。`main` 里已经合并了 S23 和 S30 的证据，会被驱动当作已完成而跳过，所以先在主机上把这两份移到 `tmp/.../set-aside/`，在同一轮次里重跑。本目录保留旧 summary，作为 `attempt-*-summary.json`。

| 题 | configure | 部署 | 注入 | 告警到达（自注入结束） | Run | 清理 | 状态 |
|---|---|---|---|---|---|---|---|
| S23 | 0.7 s，rc 0 | 471 s | 5 s，rc 0 | 5 分 15 秒 | 110 s，`completed` | 4 s + 408 s | completed |
| S30 | 0.8 s，rc 0 | 419 s | 4 s，rc 0 | 20 分钟内没有（轮询 81 次） | 没有 | 4 s + 402 s | `stopped: NO_ENTRY_ALERT` |

S23：
- Run `45a97d03-141a-579d-b77f-da208bab68dc`，[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/6701a471-5a6e-435f-bd1c-cea93d704682/r/00000000-0000-0000-6d09-99e22c434cf2?poll=true)。
- 诊断耗时：自故障起点代理 433 s，自投递 110 s。
- token：prompt 674,379，其中缓存命中 623,104；completion 19,795。
- 入口告警是 `KubePodNotReady`，按 alertname 退回匹配。
- 官方 judge 的分数见上方判分表。这次 judge 耗时约 22 分钟，比 S20 慢。

S30：补上 configure 之后仍然没有入口告警。注入前已在响的告警组合有 4 个，均已排除。缺这一步解释不了 S30 和 S31 没有入口告警；原因仍未核实，是排除规则误伤还是告警规则没有触发，尚无定论。

主机：跑完后已删除 ITBench 集群，检查并停掉了本任务的 cloud-provider-kind 进程（PID 1781721、1781679）。`opspilot-m1` 仍处于停止状态，没有动过。
