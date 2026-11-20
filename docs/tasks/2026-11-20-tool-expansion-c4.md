# 工具面拓展 C4：ArgoCD 只读（应用、同步状态、同步历史、资源同步状态）

- 状态：公开接口已定（本记录）；实现等 B4（#563，PR #752）合并后 merge `origin/main` 再开工（两者都改 `otel_demo_ext.py` 的集合计算、`change_history` 键和 CI job `k8s-backends`）
- 更新日期：2026-11-20
- 依据：issue #568；[B/C 批次记录](2026-11-16-tool-expansion-bc.md)；[决定表](2026-11-12-tool-expansion-contract.md) R8-9、R8-15、R8-19（扩展档位只读 `argoproj.io` 的 Application 类资源，其他 CR 照旧不授权）、R8-25、R8-27 与 r11（B4C4-U1 = a；C4-1 至 C4-11 可逆默认全部采纳）；C3 第 8 节；B4 记录 `docs/tasks/2026-11-20-tool-expansion-b4.md`（PR #752 分支 `feature/563-helm`）。预审全文在仓库外：`~/.agent/handoffs/opspilot-b4c4-prereview-2026-11-19.md`（C4 部分、BC-1/BC-4/BC-5），细节以它和本记录为准。上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/argocd.yaml`（`argocd/core`）
- 工作区：`../poa-568-argocd`，分支 `feature/568-argocd`，起点 `origin/main` `23a034c6`

## 目标

调查能看到 ArgoCD 应用的同步与健康状态、最近一次操作、最近几次同步记录和各资源的同步状态。完成标志（issue）：证据里有某应用的同步状态与最近几次同步记录。工具只进扩展档位 `otel-demo-ext`，只在 web 与 worker 都设了 `OPSPILOT_ARGOCD=1` 时出现；开关关时 `otel-demo` 与扩展档位所有现有 revision 字节不变。

## 工具（C4-1，名字照上游，取只读子集）

| 工具 | 参数（必填加粗） | 请求（代码拼，路径写死） | `results` 每行 | `row_unit` |
|---|---|---|---|---|
| `argocd_app_list` | `namespace`（可选；不给为整个集群） | `GET /apis/argoproj.io/v1alpha1/applications` 或 `.../namespaces/{namespace}/applications`，`limit=100` 分页（`continue`），复用 `KubernetesClient.get` 与 `kubernetes_core._page` | 每个应用一行摘要：`name`、`namespace`、`project`、`sources`（见投影）、`destination`、`sync_status`、`health_status`、`operation_phase`、`reconciled_at`；按 `namespace`、`name` 排序 | `applications` |
| `argocd_app_get` | **`app_name`**、`namespace`（默认 `argocd`） | `GET .../namespaces/{namespace}/applications/{app_name}` | 一行：白名单全量（见投影） | `applications` |
| `argocd_app_history` | **`app_name`**、`namespace`（默认 `argocd`） | 同上 | `status.history[]` 每条一行，按 `id` 升序 | `history` |
| `argocd_app_resources` | **`app_name`**、`namespace`（默认 `argocd`） | 同上 | `status.resources[]` 每条一行：`group`、`version`、`kind`、`namespace`、`name`、`status`（同步）、`health`（`status`、`message`）、`hook`、`requires_pruning`、`sync_wave`；按 `group`、`kind`、`namespace`、`name` 排序 | `resources` |

- 上游 `argocd/core` 另外 7 个不做：`argocd_app_diff`、`argocd_app_manifests`、`argocd_app_manifest_source_revision`（要 ArgoCD API/repo-server 渲染，CR 里没有）、`argocd_repo_list`、`argocd_cluster_list`（存在 ArgoCD 的 Secret 里）、`argocd_proj_list`、`argocd_proj_get`（AppProject 不在 R8-19「Application 类」里，C4-3）。这是与上游的工具面差距，工具描述与结果页照写。
- 参数校验（发请求前，码放 notes 开头，归 `INVALID_PARAMS`）：`namespace` 用 `check_namespace`，否则 `INVALID_NAMESPACE`；`app_name` 必须是 DNS-1123 子域（`^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$`，不超过 253 字符），否则 `INVALID_APP_NAME`。没有其他参数。
- 上限（2026-11-20 实现时实测修订）：`argocd_app_list` 最多 120 行、`argocd_app_resources` 最多 250 行（实测每行约 154 与 74 token，原估计的 300/500 行会被 25k 视图上限整条拒绝）、`argocd_app_history` 不设（ArgoCD 默认保留 10 条，`revisionHistoryLimit`）；排序后取前 N，标不完整。原始读取每次调用合计 4 MiB（list 分页按余额读，最多 50 页），投影体 2 MiB、视图 25k token，超限整条 `RESULT_TOO_LARGE`。行数上限按 C3 的教训在实现后实测每行 token 数，满上限放不进 25k 时下调并记理由。所有字符串值截到 2000 字符。

## 投影（C4-6，白名单新建，不做删除式过滤）

只从下列字段拷进新 dict，其余一概不读出：

- `name`、`namespace`（`metadata.*`）、`project`（`spec.project`）、`destination`（`spec.destination` 的 `server`、`name`、`namespace`）、`automated`（`spec.syncPolicy.automated` 存在时 `{prune, self_heal}` 两个布尔，否则 `null`）。
- `sources`：`spec.source` 或 `spec.sources[]` 统一成列表，每项只有 `repo_url`、`path`、`chart`、`target_revision`、`ref`、`name`。`repo_url` 输出前去掉 userinfo、查询串和片段（`https://user:token@host/x?a=b` → `https://host/x`；解析不了的整值换成 `null` 并在 notes 计数）。
- `sync`：`status.sync.status`、`status.sync.revision`/`revisions`（`comparedTo` 只取 `destination`，不取它的 `source(s)`）。
- `health`：`status.health.status`、`message`。
- `conditions`：`status.conditions[]` 的 `type`、`message`、`last_transition_time`。
- `operation_state`：`status.operationState` 的 `phase`、`message`、`started_at`、`finished_at`、`retry_count`、`sync_result.revision(s)`、`sync_result.resources[]`（`group`、`kind`、`namespace`、`name`、`status`、`message`、`hook_phase`、`sync_phase`）；**不取** `operation`（它带 `sync.source(s)` 的副本）。
- `reconciled_at`、`images`（`status.summary.images`）。
- 历史行：`id`、`revision`/`revisions`、`deployed_at`、`deploy_started_at`、`initiated_by`（`username`、`automated`）、`sources`（同上清洗，只六个字段）。
- **永不出工具**：`spec.source(s)` 的 `helm`（`values`、`valuesObject`、`parameters`、`fileParameters`、`valueFiles` 等全部）、`kustomize`、`plugin`（`env`、`parameters`）、`directory`（`jsonnet` 的 `extVars`/`tlas`）；同一 `ApplicationSource` 在 status 里的三处副本（`status.history[].source(s)` 只留六个字段、`status.operationState.operation`、`status.sync.comparedTo.source(s)`）；顶层 `operation`；`metadata.annotations`（含 `last-applied-configuration`）、`metadata.labels`、`managedFields`；`spec.info`、`spec.ignoreDifferences`。message 类字段经现有脱敏。`may_contain_secrets=True`。
- 时间统一为 UTC 带 `Z` 的 RFC3339 到秒，缺省或解析不了为 `null`。

## 时间（B4C4-U1 = a，C4-7）

当作对象当前状态：不按事故框裁剪，返回全部保留的历史，每行带自己的时间。`source_start_at`、`source_end_at`、`data_as_of` 留空；观察时间是查询时刻。notes 第一条：`ArgoCD Application state as stored now (queried at <t>); history entries outside the authorized window are listed too.` `cannot_prove`：应用状态是 ArgoCD 控制器最后一次对账（`reconciled_at`）的结论，不是查询时刻实时比较；同步历史只包括 ArgoCD 执行的同步；ArgoCD 之外对集群的改动只表现为 OutOfSync，看不到改了什么；`OutOfSync` 只说明对象与 Git 渲染结果不一致，字段级差异这里拿不到（C4-8），可以再用 Kubernetes 工具看实际状态；框外条目也列出，不能证明框内只发生过这些同步。只改工具描述，不改调查提示词。

## 错误分类（C4-9，本模块自己的表）

| 情形 | 归类 |
|---|---|
| `INVALID_NAMESPACE`、`INVALID_APP_NAME`（发请求前） | `INVALID_PARAMS` |
| 404 且响应 `Status.details.name == app_name`（CRD 在、应用不存在） | `no_data`，notes `APP_NOT_FOUND: <namespace>/<app_name>` |
| 其余 404（集合路径 404，或命名路径的 404 不带该名字：CRD 未装） | `SOURCE_ERROR`，notes `ARGOCD_CRD_MISSING: this cluster has no ArgoCD Application CRD (applications.argoproj.io).` |
| 401、403（身份没装 ArgoCD 追加 ClusterRole） | `SOURCE_FORBIDDEN`，notes `This cluster's read-only identity has no permission to read ArgoCD Applications.` |
| 400、422 | `INVALID_PARAMS` |
| 410（continue 过期） | `SOURCE_ERROR` |
| 429、5xx / 超时 / 超预算 / 非对象形状 | `SOURCE_UNAVAILABLE` / `TOOL_TIMEOUT` / `RESULT_TOO_LARGE` / `MALFORMED_RESULT` |
| list 0 行；`status.history` 或 `status.resources` 为空 | `no_data`（history/resources 的 notes 写应用名，说明「应用存在但没有记录」） |

每次调用 30 秒期限，不重试。

## 开关、档位与身份

- `OPSPILOT_ARGOCD`：只在 `otel-demo-ext` 读，web 与 worker 启动时各读一次；`1` 开；未设、空串、`0` 关；其他值启动失败 `ARGOCD_INVALID`（加进 `profiles.py` 的 `_STARTUP_CODES`）。写法同 B4 的 `OPSPILOT_HELM_RELEASES`（预审 C4-2 写的 `on` 按 B4 实现决定改为 `1`，同一档位一种写法）。`deploy/docker-entrypoint.sh` 放行列表加 `OPSPILOT_ARGOCD`。不另加 lab 闸门（C4-11）。依据：① 它走扩展档位已有的 K8s 只读客户端（同一 `KubernetesClient`、同一 `OPSPILOT_K8S_API_URL` 与 `k8s-ro` 身份），与 A2 的集群对象工具受同一条 `KUBERNETES_NOT_LAB` 检查（`tracing.check_lab_target`，扩展档位必查集群地址，A2-13）。更正（#757）：这条检查只停 lab 模式的 trace 导出，不阻止工具对非 lab 集群运行（B4 的 `HELM_NOT_LAB` 会让 worker 不启动，这里没有对应的一道）；R8-19 没有要求 ArgoCD 加 lab 闸门，所以不加；② 与 B4 不同，它不读 Secret，身份层只多了 `applications` 的 get/list，没有「身份可读全部 Secret」那类残余风险，R8-19 也没有把它限定在合成环境。不新增凭据或地址：同一 `k8s-ro` 槽、同一 `KubernetesClient`。
- 不进通用 `RESOURCES` 表（C4-5），`resolve_kind` 与 `resource_table()` 不变；jq、count、tabular 工具拿不到 Application。B4 的网关（`KubernetesClient.get`）不改：本模块只拼 `applications` 的固定路径，网关对非 Secret 路径照常放行。
- 档位：四个工具接在 B4 的 Helm 三个工具之后、`fetch_skill` 与 `TodoWrite` 之前（以 B4 合并后的 main 为准）；`tool_schema_revision(..., argocd=False)` 时现有值字节不变；新常量 `ARGOCD_TOOL_NAMES`、`ARGOCD_TOOL_SCHEMA_REVISION`；`data_sources.available["change_history"]` 追加四个名字（与 Helm 名字一起排序），开关只开 ArgoCD 时 `change_history` 只有这四个，`not_integrated` 同样去掉 `change_history`。web 与 worker 开关不一致时 Run 在认领时被拦。
- 注册：`source="otel-demo"`、`verb="get"`（list 工具 `"list"`）、`may_contain_secrets=True`、`result_path=("results",)`；新模块 `opspilot/tools/argocd_apps.py`（`ArgoCDTransport`，C4-10）。
- 身份层（C4-4）：新增 `scripts/kind_lab/opspilot-readonly-rbac-argocd.yaml`：ClusterRole `opspilot-investigator-argocd`，唯一规则 `apiGroups: [argoproj.io]`、`resources: [applications]`、`verbs: [get, list]`；ClusterRoleBinding 同名，绑 `opspilot-readonly/opspilot-investigator`。基础清单不动。`scripts/k8s_authz_probe.py` 加 `--expect-argocd-overlay`：applications 的 get/list 期望 200，`applicationsets`、`appprojects` 期望 403，其余同基础期望。

## CI（R8-25，往 B4 的 `k8s-backends` job 加步骤；名字与变量定死）

- 同一 kind 集群（B4：`b4`，`kindest/node:v1.35.0`，apiserver `127.0.0.1:36443`），不另起集群。ArgoCD `v3.6.1`（2026-11-04，满两周；`v3.6.2` 是 11-18 发的不用；tag 提交 `94037c4d4e1af4bd0651d37a179fbd9e347e668c`）。CI 只装 CRD，不跑 ArgoCD 控制器（Application CRD 是 `subresources: {}`，status 随对象一起写入，`application-crd.yaml:7229`【核实】）。三个 CRD 从 `https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.1/manifests/crds/<file>` 下载并校验 sha256（2026-11-20 读取）：`application-crd.yaml` `9fd0da3e7097ad6d1af614fabd5c960bc7ddb00d768c3ef485a2fda6e9460df8`、`applicationset-crd.yaml` `980128848a8d2152b946aec6a11cdd1174b38b04587f4fba94537c040fd84202`、`appproject-crd.yaml` `084150a2feccd188a1dee09cd8a3a259dfacecdfb7e10037df78900f60816482`。Application CRD 416 KB，超过 `last-applied-configuration` 注解上限，用 `kubectl apply --server-side`。
- 步骤（在 B4 的测试步骤之后）：下载并校验 CRD → `kubectl apply --server-side` 三个 CRD 并 `kubectl wait --for=condition=Established` → apply `scripts/kind_lab/opspilot-readonly-rbac-argocd.yaml`（同一 SA `opspilot-investigator`，`B4_K8S_TOKEN_FILE` 的 token 随即拥有 applications 读权限；`B4_K8S_BASE_TOKEN_FILE` 的 SA 不绑，用来测 403）→ `tests/fixtures/c4_argocd/seed.sh`（管理员 kubeconfig，`kubectl create -f tests/fixtures/c4_argocd/apps.yaml`，已存在则 `replace`）→ 测试作者 `tests/integration/test_tool_expansion_c4_argocd.py` → 实现者 `tests/integration/test_tool_expansion_c4_argocd_impl.py` → `kubectl delete crd applications.argoproj.io` → 再跑两文件里标了 `C4_ARGOCD_CRD_REMOVED=1` 门控的「CRD 未装」测试 → 失败时打印 `kubectl get applications -A -o name`（只列名字）。门控沿用 `OPSPILOT_K8S_BACKENDS_LIVE=1`；变量沿用 B4 的 `B4_K8S_API_URL`、`B4_K8S_CA_FILE`、`B4_K8S_TOKEN_FILE`、`B4_K8S_BASE_TOKEN_FILE`，另加 `C4_CANARIES=tests/fixtures/c4_argocd/canaries.txt`、`C4_ARGOCD_NAMESPACE=argocd`。
- 合成数据（`tests/fixtures/c4_argocd/apps.yaml`，status 时间写死为过去的固定时刻）：namespace `argocd`：
  - `c4-healthy`：Synced/Healthy，`spec.source` 单源，history 2 条，resources 3 个都 Synced/Healthy。
  - `c4-degraded`：OutOfSync/Degraded，conditions 一条 `SyncError`，`operationState` phase `Failed`（message、`syncResult.resources` 一条失败的 Deployment），resources 里一个 OutOfSync 的 Deployment。
  - `c4-history`：`spec.sources` 多源，history 12 条（id 1–12，测排序与不裁剪：一半 `deployedAt` 早于授权框）。
  - `c4-canary`：金丝雀放进每个「永不出工具」位置：`spec.source.helm.values`、`helm.valuesObject`、`helm.parameters[].value`、`helm.fileParameters[].path`、`plugin.env[].value`、`plugin.parameters[].string`、`kustomize.patches[].patch`、`directory.jsonnet.extVars[].value`、`metadata.annotations`、`metadata.labels`、顶层 `operation.sync.source.helm.values`、`status.history[0].source.helm.values`、`status.operationState.operation.sync.source.helm.values`、`status.sync.comparedTo.source.helm.values`、`repoURL` 的 userinfo 与查询串；各用不同的低熵串（`CANARY-C4-VALUES`、`CANARY-C4-VALUESOBJ`、`CANARY-C4-PARAM`、`CANARY-C4-FILEPARAM`、`CANARY-C4-PLUGINENV`、`CANARY-C4-PLUGINPARAM`、`CANARY-C4-KPATCH`、`CANARY-C4-JSONNET`、`CANARY-C4-ANNOTATION`、`CANARY-C4-LABEL`、`CANARY-C4-OPERATION`、`CANARY-C4-HISTSRC`、`CANARY-C4-OPSTATE`、`CANARY-C4-COMPARED`、`CANARY-C4-REPOUSER`、`CANARY-C4-REPOQUERY`），清单 `canaries.txt`。
  - namespace `c4-other`：`c4-elsewhere`（测整个集群 list 与 namespace 参数）。
  - `argocd` 里另建 ApplicationSet `c4-set` 与 AppProject `c4-proj`。
- 断言清单（经真实 `KubernetesClient` 与执行器，不调模型）：
  - A-1 四个工具都有真实返回；`argocd_app_list()` 含五个应用、按 namespace/name 排序；`argocd_app_list(namespace="c4-other")` 只有 `c4-elsewhere`。
  - A-2 `c4-degraded` 的 get：sync/health、conditions、operation_state 与 seed 一致；resources 里 OutOfSync 的那一行。
  - A-3 `c4-history` 的 history 12 行、id 升序，框外的条目也在，来源时间与 `data_as_of` 为空，notes 第一条与 `cannot_prove` 如上。
  - A-4 金丝雀**正对照**：管理员读回 `c4-canary` 时每个金丝雀都在（证明 apiserver 没有把字段裁掉）；**断言**：每个金丝雀都不出现在任何工具的 body、视图、notes、PG 里的 raw 与 view 证据、ledger、span 导出、`caplog`；`repo_url` 输出为去掉 userinfo 与查询串后的值。
  - A-5 不存在的应用 → `no_data`/`APP_NOT_FOUND`；不存在的 namespace 的 list → `no_data`。
  - A-6 `B4_K8S_BASE_TOKEN_FILE` 的 token：四个工具都是 `SOURCE_FORBIDDEN`。
  - A-7 身份层：`B4_K8S_TOKEN_FILE` 的 token 直接 `GET .../applicationsets`、`.../appprojects`（列表与命名对象）得 403；对 applications 的 `PATCH`、`DELETE`、`POST` 得 403，数据不变。
  - A-8 开关关时四个工具不在工具面、调用被拒；`otel-demo` 字节不变。
  - A-9（CRD 删除后）`argocd_app_list` 与 `argocd_app_get` → `SOURCE_ERROR`/`ARGOCD_CRD_MISSING`，不是 `no_data`。

## 合同测试（未参与实现的测试作者，本机替身）

文件 `tests/test_tool_expansion_c4_contract.py`，`http.server` 或间谍 opener 替身：① 投影白名单——金丝雀参数化放进上面每个位置（含 status 三处 source 副本、顶层 operation、注解、repoURL userinfo 与查询串），断言不出现；② 404 的两种区分（带名字的 NotFound 与 CRD 未装）；③ 参数校验在发请求前拒绝；④ 上限与不完整、分页与 4 MiB 预算；⑤ 错误码表；⑥ 开关、`ARGOCD_INVALID`、开关关时 `otel-demo-ext` 现有 revision 与 `otel-demo` 字节不变、`change_history` 键；⑦ 时间字段与 notes 第一条；⑧ 请求路径只有 `applications` 的固定形状，不碰 `applicationsets`/`appprojects`/Secret。

## 自建功能验收（R8-27 冒烟，不计分，租机上跑 1 遍）

- 脚本 `scripts/c4_argocd_scenario.py`（评测侧，工程师 kubeconfig）：
  - `setup --kubeconfig <工程师>`：在租机 kind 集群装 ArgoCD core（非 HA：`https://raw.githubusercontent.com/argoproj/argo-cd/v3.6.1/manifests/core-install.yaml`，sha256 `0b38189d6e1fd76ef5c3db10355026c48a02020d82bf21a1a1b5ab16dffe4f3c`，镜像 `quay.io/argoproj/argocd:v3.6.1@sha256:296e3d74f449b2ce45265613fcb68c18ed7366488360c8ea32f5f2a93d2e5c54`），等控制器就绪；建应用 `guestbook`（`https://github.com/argoproj/argocd-example-apps`，`path: guestbook`，`syncPolicy.automated`，目标 namespace `c4-demo`），`targetRevision` 先设 `e27374f5ea0afcf87ffcf1c9c377797862437312`、同步完成后改为 `8088f4c0d970abb09e250248cc97e35623447cb5`（两次同步，history 2 条），最后把 `path` 改成不存在的 `guestbook-missing`（得 `ComparisonError` 与 Unknown/OutOfSync）；`helm.values` 放金丝雀 `CANARY-C4-SCENARIO`。apply ArgoCD 追加 ClusterRole。
  - `check --summary <summary.json> --evidence-dir <dir>`：判定报告引用了 `argocd_app_get` 或 `argocd_app_history` 的证据、证据里有同步状态与至少 2 条同步记录（各带 revision 与时间）、金丝雀在证据目录与 `summary.json` 里搜不到；退出码 0/1。
  - `teardown`：删 `guestbook` 应用、`c4-demo` namespace 与 ArgoCD core（只删本脚本建的对象，按名字与 label 核对）。
- 提问原话：「guestbook 应用现在的同步状态是什么，最近几次同步是什么时候」，1 遍，放 #669 检查点阶段。环境：web 与 worker 都设 `OPSPILOT_TOOL_PROFILE=otel-demo-ext`、`OPSPILOT_ARGOCD=1`，worker 的 `OPSPILOT_K8S_*` 指向租机集群只读身份。
- 未确认（冒烟时核实）：租机能否拉到 GitHub 与 quay.io 镜像；guestbook 镜像拉不到时 health 为 Degraded，不影响同步状态与历史的判定。

## 新数据源登记草稿（合并时移入 B/C 记录）

- C4（#568，只在 `otel-demo-ext` 且 web 与 worker 都设了 `OPSPILOT_ARGOCD=1`）：同一 kube-apiserver 的 `GET /apis/argoproj.io/v1alpha1[/namespaces/{ns}]/applications[/{name}]`（list 带 `limit`/`continue`），凭据槽 `k8s-ro`（身份另加 `applications` 的 get/list）。返回 Application 的源位置（去掉 userinfo 与查询串）、目标、自动同步开关、同步与健康状态、conditions、最近一次操作与其资源结果、资源同步状态、镜像列表、同步历史；spec 与 status 里的 helm values/参数、kustomize、插件环境变量、jsonnet 变量、顶层 operation、注解与标签不出工具。来源时间留空（当前状态类，B4C4-U1）。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（`KUBERNETES_NOT_LAB`）。不新增接收方。

## 实现决定（可逆）

- 开关值 `1`（不是预审的 `on`）：与 B4 实现一致。
- get/history/resources 的 `namespace` 可选、默认 `argocd`：上游 `app get`/`resources` 不带 namespace、`history` 带 `--app-namespace`；ArgoCD 默认把 Application 放在自己的 namespace。
- 404 区分靠响应体 `details.name`：命名路径上 CRD 未装与应用不存在都是 404，只有前者的 Status 不带对象名。
- CI 只装 CRD、不跑控制器：status 可直接写入，省去控制器启动与 Git 访问；控制器真实写出的 status 由租机冒烟覆盖。
- 行数上限：接口原估计 300/500，实测（DeepSeek 计数器，合成数据的典型行）摘要行约 154 token、资源行约 74 token，改为 120/250，留出视图其他字段的余量。
- 开关值用 `1`（预审写的是 `on`），与 B4 的 `OPSPILOT_HELM_RELEASES` 一致。
- lead 裁决（2026-11-20，测试作者歧义）：`app_name` 用 `check_object_name`（A3-6，每段 DNS label ≤63、总长 ≤253）；list 触到 50 页上限按 `RESULT_TOO_LARGE`（同 B4 的 200 页）；`ARGOCD_INVALID` 进 `_STARTUP_CODES`；`status.operationState.syncResult.source(s)` 是 status 里第 4 份 source 副本，白名单本来就排除。
- 资源行与 `sync_result.resources` 的 `group` 缺省为空串（Kubernetes 核心组的写法；ArgoCD 对核心组省略该字段），不为 `null`。理由：按 group 排序的行键要可比较（CI 实测测试作者 A-1）。
- 合成数据比接口多一个金丝雀位置：`status.operationState.syncResult.source`（同一 `ApplicationSource` 的第四处副本，CRD 里有这个字段），`CANARY-C4-SYNCRESULT`。
- 工具描述（#757，2026-11-20）：原文说 Helm values 与参数「never returned」，但 Helm 渲染失败时 ArgoCD 写进 condition、health、operationState 的 message 常带完整命令行（含 `--set k=v`），这些 message 字段只做现有的凭据脱敏（合同接受）。改为「never returned as fields」，并写明 message 按 ArgoCD 原文返回、可能引用 Helm 命令行。模型可见文本变了，`ARGOCD_TOOL_SCHEMA_REVISION` 从 `otel-demo-ext-84f2799d2477` 变为 `otel-demo-ext-d46fcce79896`；不开 ArgoCD 时各值不变。
- 合成数据文件是 JSON 写的 `apps.yaml`（kubectl 当 YAML 读）：由脚本生成、字段多，JSON 便于核对。

## 下一步

B4（PR #752）合并后 merge `origin/main`，按本接口实现；合同测试与集成测试交未参与实现的测试作者；Draft PR 交 CI。
