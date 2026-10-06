# M1-02 第 0 步：kind 实验环境（OTel Demo Helm + kube-state-metrics）

- 日期：2026-10-05 21:10 PDT 创建 colima profile；2026-10-06 10:04–11:10 UTC 安装、测量与回归 Run。issue #82；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md) 计划 0、决策 D2/D4。
- 代码：分支 `chore/m1-02-kind-lab`（已合并 `origin/main` `0c97377`），产品代码零改动；新增工程脚本 `scripts/kind_lab.py` 与 `scripts/kind_lab/`。
- 授权：AGENTS.md「费用与真实调用」常设授权（DeepSeek 真实调用）；实验环境写操作只在工程脚本内。

## 环境与版本（`versions.txt`）

| 层 | 版本 |
|---|---|
| colima | 0.10.1，profile `m1-kind`：4 CPU / 8 GiB / 30 GiB，docker runtime（`m0-otel`、`default` 保留未动） |
| kind | v0.33.0，节点镜像 `kindest/node:v1.37.0`，集群 `opspilot-m1`（单节点，NodePort 30080/30090/30686 → 宿主 127.0.0.1:18080/19090/16686） |
| kubectl / helm | v1.37.1 / v4.3.0（Homebrew） |
| OTel Demo chart | `open-telemetry/opentelemetry-demo` **0.37.8**，appVersion **2.0.2**（与 M0 Compose 冻结的 demo 版本一致），values 见 `scripts/kind_lab/values.yaml` |
| kube-state-metrics chart | `prometheus-community/kube-state-metrics` **8.6.0**，app v2.20.0，只开 deployments/pods/replicasets/services 收集器 |
| 子 chart | otel-collector-contrib 0.131.0、Jaeger all-in-one（内存存储）、Prometheus v3.5.0（OTLP 接收，emptyDir，无持久卷） |

精简（D4）：**关闭** accounting、fraud-detection、kafka（checkout `KAFKA_ADDR=""` 跳过 producer）、grafana、opensearch、flagd-ui sidecar。**保留** 20 个 Deployment：ad、cart、valkey-cart、checkout、currency、email、flagd、frontend、frontend-proxy、image-provider、jaeger、kube-state-metrics、load-generator（2 用户、无浏览器流量）、otel-collector、payment、product-catalog、prometheus、quote、recommendation、shipping。ad/recommendation 保留是为了 frontend 不在每个商品页产生与 checkout 无关的错误。

## 实测内存（`memory.txt`）

| 测点 | 值 |
|---|---|
| 所有容器 working set 合计（crictl stats，全部就绪后 2 分钟） | **1288 MiB**（最大：kube-apiserver 247、ad 193、prometheus 146、frontend 97、collector 74） |
| kind 节点容器（docker stats） | 2.48 GiB 空载 → 2.71 GiB 回归 Run 期间（含页缓存） |
| VM 内（free -m） | used 2.0–2.3 GiB，buff/cache 5.8–6.0 GiB，无 swap |
| 宿主上 VM 进程 RSS | 1.2 GiB |
| 宿主整体 | 16 GB 机器始终 15 GB used、100–900 MB unused、压缩 2.4–6.7 GB（其他 agent 与小 PG 并行）；运行期间无 pod 重启（`kube_pod_container_status_restarts_total` 合计 0） |

结论：精简栈在 8 GiB VM 内稳定；宿主侧紧张来自并行负载而非本环境，未触及 D4「无法稳定运行」边界。

## 启动过程中的两个问题（已修）

1. **镜像拉取全部失败**：kind 把宿主的 `HTTP(S)_PROXY=127.0.0.1:1087` 写进节点 containerd，节点内连不到该代理（`proxyconnect ... connection refused`，首次集群 5 h45 m 全部 ImagePullBackOff）。VM 可直连 registry，脚本改为对 kind 剥离代理变量后重建集群，首次拉完约 20 个镜像用时 ~20 分钟。
2. **otel-collector CrashLoop**：values 里 `exporters.opensearch: null` 留下无 endpoint 的空壳被 collector 拒绝；改为保留定义、仅从 logs pipeline 移除。

## 健康与 K8s 状态信号（`lab-health.json`）

`scripts/kind_lab.py health`：Prometheus 有 `traces_span_metrics_calls_total`，有 `kube_deployment_status_replicas_available{namespace="otel-demo"}`（checkout=1）、`kube_pod_status_phase`（20 个 Running）、`kube_pod_container_status_restarts_total`；Jaeger 列出 15 个服务含 checkout；frontend 200；20 个 Deployment 全部就绪。资源属性 `opspilot.integration.id=m0-otel-20260909` 已在新 pod 的 series 与 `target_info` 上成为 `opspilot_integration_id` 标签。

## 回归：M1-01 `otel-demo` profile 不改地址、不改标签（`regression/`）

- 路径：`POST /intake/ui` → 常驻 `python -m opspilot.worker_main`（`profile=otel-demo`，`tool_schema_revision otel-demo-34bc78980747`）→ 真实 DeepSeek；PG 为一次性实例 127.0.0.1:55481（`make migrate` 到 `0002_state_checks`）；后端地址使用 `OtelDemoConfig` 默认值（19090 / 16686/jaeger/ui），**产品代码与 profile 零改动**。每案提交前后用独立脚本 `scripts/otel_demo_observe.py` 直读 Prometheus/Jaeger。

| 案例 | 事故 / Run | 独立观察（提交前） | 结果 | 工具 | 轮次 |
|---|---|---|---|---|---|
| normal-1（10:35:51Z） | `08317d06` / `009fc21b` | 控制窗成立，无故障（7 trace） | **published**，`completed`/`partial`，16 claim；报出窗口早段 cart `GetCart` 连接拒绝（见下） | 17 | 10 |
| fault-1（10:43:47Z） | `e6170e7f` / `bb0b49f4` | 控制窗成立，**故障确认**（6 trace 中 3 条 checkout 失败且 payment 子 span 失败） | **published**，`completed`/`partial`，17 claim；定位 checkout→payment `Charge`「Payment request failed. Invalid token」（gRPC 2 → 13 → HTTP 500），其他依赖 gRPC 0 | 20 | 7 |
| normal-2（11:02:48Z，恢复后干净窗口） | `d28dc372` / `8d21d00c` | 控制窗成立，无故障（5 trace） | **published**，`completed`/`partial`，18 claim；checkout 各操作 ERROR 增量 0，依赖调用全部 gRPC 0，引用窗内成功 trace | 31 | 10 |

三次 Run 的 `tool_charges_rows` 与 `tool_operations_used` 一致（17/20/31），`run_state completed`，事件流以 `run_completed` 结束（`sse.txt`）。

说明：
- normal-1 的 10 分钟查询窗（10:25:51–10:35:51Z）覆盖了 cart 晚于 checkout 就绪（busybox init 镜像拉取回退）期间的 `GetCart connection refused`，报告如实报出该失败并归因到 cart 调用——这是环境启动期的真实数据，不是工具退化；normal-2 在干净窗口重做。
- 故障钩子：`fault inject` 10:38:52Z patch ConfigMap，指标上 10:42:07Z 才可见（kubelet 同步 + flagd 重载，约 3 分钟，比 Compose 的文件直改慢）；`fault restore` 10:46:33Z，10:48:16Z 起 `Charge` 连续 2 分钟无错误；前后 SHA256 往返一致（`fault-1/fault-log.jsonl`），`observe-after-restore.json` 为恢复后干净窗口。
- 费用：DeepSeek 余额 6.71 → 6.14 CNY（`deepseek-balance-before.json` 04:14Z / `-after.json` 11:06Z），三次 Run 合计 **≤ 0.57 CNY**（余额差为上界：同一账号在此期间可能有其他 agent 的调用）。
- 安全：目录内无 key、口令、`Authorization` 头；`request.json` 用 `idempotency_label` 字段名（gitleaks 误报规避，见既有约定）。

## 启停

PR #111 审查后（2026-10-06）：故障历史目录固定在 `tmp/m1-kind-lab/engineer-only/`（不可用环境变量改指）；kubectl/helm/kind 全部使用 `tmp/m1-kind-lab/kubeconfig`，Helm 仓库配置与缓存也在 `tmp/m1-kind-lab/helm/`（宿主 `~/.kube/config` 与全局 Helm 配置经 sha 比对未变）；`fault inject|restore` patch 后重读 ConfigMap 核对 SHA-256，`fault-log.jsonl` 多出 `live_sha256`/`verified`；`health` 的 `ok` 包含 frontend 与 Deployment 就绪，每个组件带自己的 `ok`。本节的 `lab-health.json` 仍是修改前的字段形状。

```sh
.venv/bin/python scripts/kind_lab.py up       # 检查宿主可回收内存 ≥ 3 GiB → colima start m1-kind → kind create → helm upgrade --install ×2 → 等 Deployment 就绪
.venv/bin/python scripts/kind_lab.py health
.venv/bin/python scripts/kind_lab.py fault inject|restore --experiment-id <id>
.venv/bin/python scripts/kind_lab.py stop     # 只 colima stop；集群与 release 保留
```

停机/再启动验证（`stop-start-cycle.txt`，11:05–11:07Z）：`stop` 18 s 关闭 VM；`up` 后 colima 启动 ~15 s，API server 刚起时对 kubernetes-admin 返回 `nodes is forbidden`（RBAC 尚未加载，脚本已改为重试），随后 20 个 Deployment 在 ~75 s 内全部就绪，每个容器 RESTARTS=1、pod 未重建，Prometheus 中停机前（10:40–11:00Z）的样本仍可查到；`health` ok。最终状态：`m1-kind` Stopped，集群与两个 release 保留在 VM 磁盘上；`m0-otel`、`default` 未动。

## 未执行 / 限制

- Prometheus 与 Jaeger 均为内存/emptyDir 存储：pod 重建（`helm upgrade` 改模板、节点驱逐）即丢历史；单纯 `stop`/`up` 保留（见上）。
- 首次 `up` 需联网拉约 20 个镜像（本机约 20 分钟）；镜像未按 digest 锁定，`versions.txt` 记录了本次实际的 imageID digest。
- 未做 K8s RBAC 拒绝取证（#92，不在本步范围；本环境可支持）。
- 未做多次故障或 soak；本次是环境可用性与 profile 回归，不是 F6 验收。
