# 工具面拓展 B3：Pod 与节点实时用量

- 状态：进行中（公开接口已定；实现与本机单测完成，等测试作者提交与 CI）
- 更新日期：2026-11-19
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) N4、R8-9、R8-15、R8-21、R8-25 与 r11 B3-U1（用户 2026-11-19 选 a）；C3 第 8 节「当前状态类观察」；上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/kubernetes.yaml:660-681`（`kubernetes/live-metrics`）。issue #562。预审全文在仓库外（`~/.agent/handoffs/opspilot-b3-prereview-2026-11-19.md`），可逆默认 B3-1 至 B3-16 全部采纳，细节以它为准
- 工作区：实现 `../poa-562-live`，分支 `feature/562-live-usage`（起点 `a9fe55df`）

## 目标

调查能看到 Pod（含容器明细）与节点此刻的 CPU、内存用量。工具只进扩展档位 `otel-demo-ext`，且只有 web 与 worker 都设了 `OPSPILOT_K8S_LIVE_METRICS=1` 时才出现；开关关闭时 `otel-demo` 档位与扩展档位所有现有 revision 字节不变。

## 行为（公开接口）

- 开关（B3-1）：`OPSPILOT_K8S_LIVE_METRICS`，只在 `otel-demo-ext` 档位读，启动时读一次，web 与 worker 都读。`1` 开；未设、空串、`0` 关；其他值启动失败 `K8S_LIVE_METRICS_INVALID`（两个进程都拦，免得拼错的值静默关闭）。开了也不新增凭据或地址：用现有 `k8s-ro` 槽与 `OPSPILOT_K8S_API_URL`，地址仍受 lab 闸门 `KUBERNETES_NOT_LAB` 约束；入口脚本已放行 `OPSPILOT_K8S_*`。

| 工具 | 参数 | 请求（只有 GET，路径写死） | 返回 `results` 每行 | `row_unit` |
|---|---|---|---|---|
| `kubectl_top_pods` | `namespace`（可选，DNS label；不传是整个集群） | `GET /apis/metrics.k8s.io/v1beta1/pods`，或 `/apis/metrics.k8s.io/v1beta1/namespaces/{namespace}/pods`；不带查询串 | `{namespace, pod, cpu_millicores, memory_bytes, timestamp, window, containers: [{name, cpu_millicores, memory_bytes}]}`，按 `namespace`、`pod` 排序，容器按 `name` 排序 | `pods` |
| `kubectl_top_nodes` | 无 | `GET /apis/metrics.k8s.io/v1beta1/nodes`；成功后再读 `GET /api/v1/nodes`（同 A2 的分页读取，`limit=500`，只取 `status.allocatable` 的 `cpu`、`memory`） | `{node, cpu_millicores, cpu_percent, memory_bytes, memory_percent, timestamp, window}`，按 `node` 排序 | `nodes` |

- 单位（B3-6，按 kubectl 口径）：数量按 K8s 数量语法解析（十进制后缀 `n u m k M G T P E`、二进制后缀 `Ki Mi Gi Ti Pi Ei`、纯数字、小数、`e` 记法）。CPU 换算成毫核、内存换算成字节，都**向上取整**为整数（同 K8s `Quantity.MilliValue()`/`Value()`）。Pod 合计先把各容器的精确值相加再取整，所以 Pod 的 `memory_bytes` 等于容器之和（字节本是整数时），`cpu_millicores` 不大于容器毫核之和、差小于容器个数。节点百分比 = 用量 × 100 ÷ allocatable，整数向下取整（同 kubectl top），可以为 0，也可以超过 100；节点在 `/api/v1/nodes` 里找不到或 allocatable 缺失、为 0 时，该百分比为 `null`，notes 写明。任一数量解析不了，整条报 `MALFORMED_RESULT`，不猜。
- `timestamp` 是 metrics-server 给的采样时刻，规范化为带 `Z` 的 RFC3339（到秒）；`window` 是 metrics-server 给的采样窗口原文（如 `"15.022s"`）。两者解析不了或缺失，整条 `MALFORMED_RESULT`。原始数量字符串、`metadata.labels` 等不进视图，也不进证据体（证据体就是上面的投影，与其他集群工具一致）。
- 时间（B3-U1=a，C3 §8 当前状态类）：`source_start_at`、`source_end_at`、`data_as_of` 都留空；报告校验不因此拒收，`reports.py` 不改。视图另加 `sampled_at: {earliest, latest}`（全部行 `timestamp` 的最早与最晚）。`notes` 第一条：`Sampled at <earliest>–<latest>: current usage, <N> s after the end of the authorized window (<window end>).`，采样早于框末尾时写 `<N> s before`。`cannot_prove`：此刻的用量，不是事故窗内的用量，也不是历史曲线（要看事故窗内的变化用 Prometheus）；结果里没有的 Pod 可能刚启动、已结束或还没被采到，不代表没在跑；用量高不等于被限流或 OOM，请求与上限用 `kubernetes_tabular_query` 看。工具不接受 `start`/`end`。
- 其他视图字段：Pod 工具 `scope: {namespace, all_namespaces}`；节点工具 notes 写「N 个节点没有用量样本」（`/api/v1/nodes` 里有、metrics 里没有的）。`result_path=("results",)`，`source="otel-demo"`，`verb="list"`，`may_contain_secrets=True`，`backend_<unit>_returned` 为行数。
- 大小与超时：每次调用 30 秒期限、不重试；一次调用读取合计 4 MiB（节点工具两个 GET 共用），超限整条 `RESULT_TOO_LARGE`；投影体 2 MiB、视图 25k token，超限整条 `RESULT_TOO_LARGE`，不截断（描述里提示用 `namespace` 缩小）。
- 错误分类（B3-8，两个工具自己的表，不改 `ERROR_CLASSES`）：发请求前 `INVALID_NAMESPACE`（`namespace` 不是 DNS label）归 `INVALID_PARAMS`；`404`（集群没有 `metrics.k8s.io`，即没装 metrics-server）报 `SOURCE_ERROR`，notes 写「这个集群没有提供实时用量接口」；`400` 报 `INVALID_PARAMS`；`401`、`403` 报 `SOURCE_FORBIDDEN`；`429` 与 5xx（含 metrics-server 不可用时 API server 回的 503）报 `SOURCE_UNAVAILABLE`；超时为 `TOOL_TIMEOUT`；连不上为来源不可用；成功但 0 行为 `no_data`；返回体不是 `PodMetricsList`/`NodeMetricsList` 形状为 `MALFORMED_RESULT`。节点工具的第二个 GET 失败按同一张表报整条失败。
- 档位与 revision（B3-1、B3-9）：后端固定顺序 Elasticsearch → Loki → VictoriaLogs → 实时用量；开关开时两个工具接在工具数组末尾，`data_sources.available.kubernetes` 加入这两个名字（不新开键），revision 变化。`tool_schema_revision(..., live_metrics=)`；开关关时现有 8 个值（无后端 `c63f058afb18`、仅 ES `4f7faf728706`、仅 Loki `dd942a2f1292`、ES+Loki `f11fc2ce4d18`、及含 VictoriaLogs 的 4 个）字节不变；新常量 `LIVE_METRICS_TOOL_SCHEMA_REVISION`、`LIVE_METRICS_TOOL_NAMES`（仅开关）。含开关各组合的值实现后实测写进本记录。worker 把两个工具按名字路由到现有 `KubernetesTransport`（同一个 `KubernetesClient`，不跟重定向、不走代理）。web 只开、worker 不开（或反之）时，Run 在认领时因版本不一致被拦，与 B1/C5 相同。
- 模型可见说明（B3-10）：只改工具描述，不改调查提示词。描述写：此刻的 CPU 与内存、单位、只是快照不能画趋势、请求与上限看 `kubernetes_tabular_query`、事故窗内的变化看 Prometheus、没装 metrics-server 时报 `SOURCE_ERROR`。Pod 工具比上游多一个可选 `namespace`，属于工具面差异（R8-15）。
- 权限（B3-4，N4）：RBAC 不改。只读身份已授权 `metrics.k8s.io` 的 `pods`、`nodes`（get、list，`scripts/kind_lab/opspilot-readonly-rbac.yaml:89-93`）与核心 `nodes`。jq 资源表不加 metrics，实时用量只能经这两个工具读。`scripts/k8s_authz_probe.py` 放行表加两行：`list pods.metrics.k8s.io`、`list nodes.metrics.k8s.io` 期望 200。

## 这个 PR 做什么

- 新模块 `opspilot/tools/kubernetes_live_metrics.py`、开关与档位组合、`KubernetesTransport` 按名分派、模型可见描述；C3 §8 当前状态类那一条补半句「实时用量（B3）同属此类」（B3-U1=a 的落实）；B/C 记录登记数据源。
- kind lab（B3-12）：`scripts/kind_lab.py up` 在应用只读身份之前装 metrics-server，清单 vendored 到 `scripts/kind_lab/metrics-server-v0.8.<最新补丁>.yaml`（文件头记来源、sha256、下载日期，镜像钉 digest），加 `--kubelet-insecure-tls`、`--kubelet-preferred-address-types=InternalIP`，等 Available。lab 部分不在本机实跑，PR 写明。
- 直接取样脚本（R8-21）：`scripts/live_metrics_sample.py`，经工具 transport（只读 token）取一次两个工具，同一时刻用工程师 kubeconfig `kubectl get --raw` 取同两份原始数据并存 `kubectl top` 输出，判定行集合一致、`timestamp` 相同的行毫核与字节相等，写 `docs/evidence/b3-live-metrics/<题号>/sample.json`；不对故障下断言。租机（#669/#671）时跑。
- 合同测试（B3-11 清单）由未参与实现的测试作者按本节编写，本机用 `http.server` 替身。

## R8-25 集成测试（CI job `k8s-live-metrics`）

- 新 job，`ubuntu-24.04`，`timeout-minutes: 15`，与 `log-backends` 并行，scope 预检同其他 job。kind 二进制按 sha256 下载，节点镜像钉 digest；单节点集群装与 lab 同一份 metrics-server 清单；应用 `opspilot-readonly-rbac.yaml`，`kubectl create token` 发只读 token，工具只用这把 token。测试用 `OPSPILOT_K8S_LIVE=1` 门控，本机跳过。
- 合成负载（镜像钉 digest）：`ns-a` 里吃 CPU 的 Pod（CPU limit 200m）、固定占约 64 MiB 的 Pod、双容器 Pod；`ns-b` 里一个空闲 Pod；等 `kubectl top pods -n ns-a` 出全 3 行再测。
- 要覆盖的行为（B3-15）：两个工具都有真实返回；`namespace=ns-a` 只有 ns-a 的行，不传时含 `kube-system` 与 `ns-b`；CPU Pod 在 120m–260m、内存 Pod 不小于 64 MiB（只证明量级）；双容器 Pod 合计与明细符合上面的取整规则；与 `kubectl get --raw` 对齐：同一时刻取原始 metrics，`timestamp` 相同的行按上面的规则算出的毫核与字节完全相等；节点行有值、百分比非负；来源时间与 `data_as_of` 为空，`sampled_at` 与 notes 正确；错误分类用真实响应：无 metrics 权限的 SA token 报 `SOURCE_FORBIDDEN`（403）、错 token 401、metrics-server 缩到 0 副本报 `SOURCE_UNAVAILABLE`（503）、最后删 `APIService v1beta1.metrics.k8s.io` 报 `SOURCE_ERROR`（404）；两层只读：工具发出的请求只有上表路径（测试里包一层记录 opener），只读 token 对 metrics 路径 `POST`/`DELETE` 得 403 或 405、对 `secrets` 的 list 得 403。不调模型。

## 实现决定（实现者自定，可逆）

- 开关非法值启动失败，不静默当关：拼错的值让两个进程都起不来比静默缺工具好查。
- 采样早于授权框末尾时写「早 N 秒」：Run 紧跟提交时，metrics-server 最近一次采样可能就在提交之前，只写「晚」会给出负数。
- 证据体只存投影、不存原始数量串：与 A2/A6 集群工具一致，原始串对读者没有额外信息，换算规则写在描述里。
- `window` 保留原文不解析：避免为 Go duration 写解析器引入新的失败点，模型读 `15.022s` 没有障碍。
- 节点百分比取整与 kubectl 一致（向下取整），不保留小数：方便与 `kubectl top nodes` 对照。
- notes 里的「晚/早 N 秒」按最早一行的采样时刻算，取整到秒：给出至少晚了多少，偏保守。
- 节点工具两个 GET 共用一次调用的 4 MiB 读取预算（第二个 GET 只拿剩余额度）：与接口「一次调用读取合计 4 MiB」一致。
- metrics-server 用 v0.8.1（0.8.x 最新补丁，2026-01-29 发布；最新是 v0.9.1，但预审按 ITBench 真实集群选 0.8 线），镜像 digest `sha256:b2d2efaf…`（registry.k8s.io，2026-11-19 读取），清单 `scripts/kind_lab/metrics-server-v0.8.1.yaml` 文件头记原文件 sha256；lab 与 CI 用同一文件。
- CI 用 kind v0.31.0 与 `kindest/node:v1.35.0`（digest 取自 kind 发布说明），同上游 HolmesGPT eval CI；负载镜像 `busybox:1.37.0` 钉 digest，占内存的 Pod 往内存型 emptyDir 写 64 MiB（计入该 Pod 的内存 cgroup），清单 `tests/fixtures/b3_live_metrics/workloads.yaml`。
- CI job 先跑测试作者的 `tests/integration/test_tool_expansion_b3*.py`（不含 `_impl`），再跑实现者的 `_impl`：后者最后一个测试把 metrics-server 缩到 0、删 APIService 取真实 503/404，结束时恢复。
- 直接取样脚本对照的期望值由原始数量按本记录规则算出（Pod 合计先精确相加再取整），不依赖 `kubectl top` 的显示取整；`kubectl top` 输出只存档。

## 证据

- revision（本分支实测）：开关关时 8 个现有值不变；开关开时：仅开关 `otel-demo-ext-4730c27997c9`，ES `f49f281c318a`，Loki `4994814c7688`，VictoriaLogs `3eec17c97bae`，ES+Loki `ebbaaaa09494`，ES+VictoriaLogs `884953bd455e`，Loki+VictoriaLogs `61aad69191fd`，三者都有 `263512312688`（均带 `otel-demo-ext-` 前缀）。
- 本机：实现者单测 `tests/test_kubernetes_live_metrics.py` 59 passed（`http.server` 替身，经执行器）；`tests/safety`、B1/C5a/C5b 合同、`test_victorialogs_logs`、`test_architecture`、`test_tracing` 1208 passed；ruff、mypy 干净。集成测试本机跳过，交 CI `k8s-live-metrics`。

## 待决

无。实施中再冒出的不可逆项或合同层取舍停下回报 lead。
