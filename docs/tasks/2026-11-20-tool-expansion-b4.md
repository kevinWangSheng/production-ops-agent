# 工具面拓展 B4：Helm 发布历史

- 状态：进行中（实现与本机测试完成，Draft PR 等 CI；F11 第 4 步代码面论证与 B4 冲突，待 lead 裁决，见「待决」）
- 更新日期：2026-11-20
- 依据：[B/C 批次记录](2026-11-16-tool-expansion-bc.md)、[决定表](2026-11-12-tool-expansion-contract.md) R8-9、R8-15、R8-18、R8-25、R8-27 与 r11（B4C4-U1=a、B4 残余风险、修正 R8-18 措辞）；C3 第 8 节；上游 HolmesGPT `a0009195` `holmes/plugins/toolsets/helm.yaml:11-42`（`helm/core`）。issue #563。预审全文在仓库外（`~/.agent/handoffs/opspilot-b4c4-prereview-2026-11-19.md`），可逆默认 B4-1 至 B4-16 全部采纳；本记录与预审不同处在「实现决定」逐条写理由，以本记录为准
- 工作区：`../poa-563-helm`，分支 `feature/563-helm`（起点 `882bd49c`，实现前 merge `origin/main` `923c734e`；测试作者提交 `bf218708` cherry-pick 为 `7c51c951`，断言未改）

## 目标

调查能回答「这个 namespace 最近一次发布是什么时候、是哪个版本、成功没有」。三个工具只进扩展档位 `otel-demo-ext`，只在 web 与 worker 都设了 `OPSPILOT_HELM_RELEASES=1`、且 worker 的集群地址是 lab 主机时出现；开关关时 `otel-demo` 与扩展档位现有 revision 字节不变。

## 残余风险（r11 原文，记录与对外宣称照写）

**该身份可读取合成环境内全部 Secret 的内容，只靠 OpsPilot 网关收窄到 Helm 发布记录。**

展开（B4-13）：开启后，OpsPilot 的集群只读身份在 Kubernetes 层面可以列出整个集群所有 Secret 的完整内容（`list` 返回对象全文，RBAC 不能按 Secret 类型或字段授权）。OpsPilot 只发带 `fieldSelector=type=helm.sh/release.v1` 的 list，其余 Secret 请求在发出前拒绝，解码后只输出发布元数据；这一限制只由 OpsPilot 代码保证，不由 Kubernetes 保证。拿到这个 token 的人或进程（worker 进程、它的环境文件、租机上的 `k8s-investigator.env`）可以读取集群里所有 Secret，包括其他 ServiceAccount 的 token。所以只在合成环境开启（`HELM_NOT_LAB`），不在真实集群开启。上游文档要求的权限（`secrets` 的 get/list/watch、整个集群）比这更宽。证据：CI 用同一 token 直接 `GET /api/v1/secrets` 得 200 且能读到 Opaque 金丝雀的值（集成断言 I-12）。

## 行为（公开接口）

### 工具

| 工具 | 参数 | 请求（代码拼，模型只给参数） | `results` | `row_unit` |
|---|---|---|---|---|
| `helm_list` | `namespace`（可选，DNS label；不传为整个集群） | `GET /api/v1/secrets` 或 `/api/v1/namespaces/{namespace}/secrets`，查询串见「网关」形状 L | 每个 (namespace, release) 一行，取 `revision` 最大的一条，不按状态过滤（同 Helm 4 `helm list -a`）；按 `namespace`、`name` 排序 | `releases` |
| `helm_history` | `release_name`、`namespace`（都必填） | `/api/v1/namespaces/{namespace}/secrets`，形状 N | 该 release 保留的全部 revision，按 `revision` 升序 | `revisions` |
| `helm_status` | `release_name`、`namespace`（都必填） | 同 `helm_history` | 最多一行：`revision` 最大的一条 | `releases` |

- 不提供 `helm_values`、`helm_manifest`、`helm_hooks`、`helm_notes`（R8-18：values 与 manifest 不出工具；hooks、notes 同为渲染内容，可能带秘密）；`helm_chart` 不单列，chart 名、版本、appVersion 并入每行。工具描述写明这些不提供。
- 参数校验（发请求前）：`namespace` 用 `check_namespace`，否则 `INVALID_NAMESPACE`；`release_name` 必须匹配 `^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$` 且不超过 53 字符，否则 `INVALID_RELEASE_NAME`。两者都归 `INVALID_PARAMS`。没有其他参数，`helm_history` 不设条数参数（Helm 默认每 release 保留 10 个 revision）。

### 每行投影（只从下列字段新建，其余一概不读出）

`name`、`namespace`、`revision`（`version`）、`status`（`info.status`）、`chart`（`chart.metadata.name`）、`chart_version`（`chart.metadata.version`）、`app_version`（`chart.metadata.appVersion`，缺省 `null`）、`first_deployed`、`last_deployed`、`deleted`（`info.*`，规范化为 UTC 带 `Z` 的 RFC3339 到秒，零值或缺省为 `null`）、`description`（`info.description`，截到 2000 字符后经现有脱敏）、`rollback_revision`（`info.rollback_revision` 为正整数时取它，否则 `description` 完全匹配 `^Rollback to (\d+)$` 时取该数，否则 `null`；Helm 3 记录没有该字段【推断】）。

- **永不出工具**（不进 `TransportResponse.body`、视图、notes、PG 的 raw 与 view 证据、超限保存的 raw、ledger、span、日志）：`config`、`chart.values`、`chart.templates`、`chart.files`、`chart` 其余字段、`manifest`、`hooks`、`info.notes`、`info.resources`（Helm 4 部署对象快照）、`labels`、`apply_method`；Secret 的 `metadata.annotations`、全部 `metadata.labels`（系统标签只用于校验）、`data` 其余键。确定性保证：解码在 worker 进程内存里完成，每条只把上表字段拷进新 dict，解码中间值不赋给任何会被记录、返回或抛出的对象；解码失败的异常不带原文（抛出前丢弃 `__cause__`/`__context__`，日志只记计数）；整页原始字节在逐条处理后即丢弃，不经 `list_objects`。

### 解码（B4-8、B4-10）

每条 Secret：`data.release` → base64（K8s 层，严格校验）→ base64（Helm 层，严格校验）→ 前三字节是 `1f 8b 08` 才 gzip 解压（`zlib.decompressobj(wbits=31)`，`max_length` 8 MiB，超过即本条 `too_large`）→ UTF-8 JSON 对象。以下任一条本条记 `malformed` 丢弃、不回显：解码或 JSON 失败；缺 `name` 或整数 `version`；`status` 不匹配 `^[a-z-]{1,32}$`；时间解析不了；Secret 标签 `owner` 不是 `helm`、或 `name`/`version` 标签与正文不一致；`metadata.namespace` 与正文 `namespace` 不一致。视图字段 `dropped: {malformed: N, too_large: N}`，非零时 notes 写条数。整页不是 `SecretList` 形状（无 `items` 列表）→ 整条 `MALFORMED_RESULT`，body 为空错误体。

### 大小与超时（B4-9）

每次调用 30 秒期限，不重试。专用分页循环：每页 `limit=5`，带上一页的 `continue`，最多 200 页；整次调用原始读取合计 16 MiB（每页 `max_bytes` 为余额），超限整条 `RESULT_TOO_LARGE`，不给部分结果，notes 提示给 `namespace` 或 `release_name`。投影体 2 MiB、视图 25k token，超限同样整条 `RESULT_TOO_LARGE`。`helm_history`/`helm_status` 的 `release_name` 进 `labelSelector`，只读该 release 的 Secret。

### 时间（B4C4-U1=a，B4-11）

发布历史当作对象当前状态：不按事故框裁剪，返回全部保留的历史，每行带自己的时间。`source_start_at`、`source_end_at`、`data_as_of` 留空；观察时间是查询时刻。notes 第一条：`Helm release records as stored now (queried at <t>); revisions outside the authorized window are listed too.` `cannot_prove`：时间框外的条目也列出，不能证明框内只发生过这些变更；发布历史只是 Helm 自己的记录，`kubectl apply`/`edit` 或 Helm 之外的改动不在里面；revision 时间来自 Helm，不是 Pod 滚动完成时间；`deployed` 只表示 Helm 认为成功，不表示 Pod 健康。

### 错误分类（B4-12，Helm 工具自己的表，`ERROR_CLASSES` 不改）

| 情形 | `source_status` | 归类 |
|---|---|---|
| `INVALID_NAMESPACE`、`INVALID_RELEASE_NAME`（发请求前） | 同名 | `INVALID_PARAMS` |
| 网关拒绝（发请求前，`sent=False`） | `RESOURCE_NOT_ALLOWED` | `RESOURCE_NOT_ALLOWED` |
| 401、403（身份没装 Helm 追加 ClusterRole） | `401`/`403` | `SOURCE_FORBIDDEN`，notes：`This cluster's read-only identity has no permission to read Helm release records.` |
| 400、404、422 | 状态码 | `INVALID_PARAMS` |
| 410（continue 过期） | `410` | `SOURCE_ERROR` |
| 429、5xx | — | `SOURCE_UNAVAILABLE` |
| 超时 / 超预算 / 非 list 形状 | — | `TOOL_TIMEOUT` / `RESULT_TOO_LARGE` / `MALFORMED_RESULT` |
| 0 行（release 或 namespace 不存在、或只有被丢弃的条目） | — | `no_data`；`helm_history`/`helm_status` 的 notes 写查的 namespace 与 release 名 |

### 网关（B4-5、B4-6、r11 键集合固定）

位置：唯一出口 `KubernetesClient.get`，发请求前调用纯函数 `secret_request_problem(url: str, accept: str, *, helm_page: bool) -> str | None`（`kubernetes_core.py`，单独可测）；返回码非空时 `get` 抛 `KubernetesRequestRefused(code)`（新异常，不继承 `TransportError`），opener 一次都不调用；各 K8s transport 在分派处统一捕获并返回 `refused(code)`。`KubernetesClient(..., helm_releases: bool = False)`；`get(..., helm_page: bool = False)` 只由 `helm_releases.py` 传 `True`，函数收到的 `helm_page` = 两者同时为真。

判定顺序（任一不满足即 `RESOURCE_NOT_ALLOWED`）：

1. 路径逐段百分号解码；解码后含空段（末尾斜杠除外的 `//`）、`.`、`..`、或解码出 `/` 的段 → 拒绝（对所有请求）。
2. 资源位：`/api/v1/{r}` 的 `r`、`/api/v1/namespaces/{ns}/{r}` 的 `r`、`/apis/{g}/{v}/{r}`、`/apis/{g}/{v}/namespaces/{ns}/{r}`。资源位解码后小写为 `watch` 或 `proxy` → 拒绝（对所有请求；现有工具不发这类路径）。资源位解码后小写为 `secrets` → 进入第 3 步；否则放行（名字或 namespace 恰好叫 `secrets` 的正常路径照常放行）。
3. `helm_page` 为假 → 拒绝。原始路径（不解码）必须逐字节等于 `/api/v1/secrets` 或 `/api/v1/namespaces/{ns}/secrets`（`ns` 过 `check_namespace`），后面不跟名字或子资源。`Accept` 必须逐字节等于 `application/json`。URL 无 fragment、无 userinfo。
4. 查询串：先拒绝含 `;` 的；`parse_qsl(strict_parsing=True, keep_blank_values=True)` 解码后，键集合必须**恰好**是 `{fieldSelector, labelSelector, limit}`（首页）或再加 `continue`（续页），每键恰好出现一次，键按原样大小写比较；值：`fieldSelector` 全等 `type=helm.sh/release.v1`；`limit` 全等 `5`；`labelSelector` 全等 `owner=helm`（只用于 `helm_list`）或全等 `owner=helm,name=` 加一个满足 release 名规则的名字；`continue` 匹配 `^[A-Za-z0-9_=+/-]{1,4096}$`。
5. 原始查询串必须逐字节等于把上述键值按 `fieldSelector, labelSelector, limit, continue` 顺序用 `urllib.parse.urlencode` 重新编码的结果（`field%53elector`、多余的百分号编码、`+` 与 `%20` 混用都因此拒绝）。

开关关时第 3 步恒拒（今天 `resolve_kind` 已拒，网关再加一道）。jq、count、tabular 工具的 `resolve_kind` 不变，**不**加 Secret 种类。

### 配置、开关与闸门

- `OPSPILOT_HELM_RELEASES`：只在 `otel-demo-ext` 读，启动时读一次，web 与 worker 都读。`1` 开；未设、空串、`0` 关；其他值两个进程启动失败 `HELM_RELEASES_INVALID`。不用 `OPSPILOT_K8S_` 前缀（A2-3：web 不读那类变量）。`deploy/docker-entrypoint.sh` 放行列表加 `OPSPILOT_HELM_RELEASES`。
- `HELM_NOT_LAB`：开关开时，worker 构建执行器前要求 `OPSPILOT_K8S_API_URL` 通过 `tracing._is_lab_host`，否则以 `HELM_NOT_LAB` 退出（加进 `profiles.py` 的 `_STARTUP_CODES`），与 `TRACE` 模式无关。web 不读该地址，只靠 worker 拦。
- 不新增凭据或地址：同一 `k8s-ro` 槽、同一 `KubernetesClient`（不跟重定向、不走代理）。

### 档位、revision 与覆盖页（B4-2、B4-14）

- 工具数组：Helm 三个工具（`helm_list`、`helm_history`、`helm_status` 顺序）接在实现开工时 main 上全部数据源后端（含实时用量，及已合并的 C6a、C3）之后、`fetch_skill` 与 `TodoWrite` 之前。`tool_schema_revision(..., helm=)`；新常量 `HELM_TOOL_NAMES`、`HELM_TOOL_SCHEMA_REVISION`（仅开关）；开关关时所有现有值字节不变，开关开的各组合实现后实测写进本记录。
- `data_sources`：开时 `available["change_history"] = ["helm_history", "helm_list", "helm_status"]`（插在最后一个后端键之后、`knowledge` 之前），`not_integrated` 去掉 `change_history`；关时字节不变。C4 以后往同一个键里加名字。`DATA_SOURCES_INSTRUCTION` 不改。
- 注册：`source="otel-demo"`、`verb="list"`、`may_contain_secrets=True`、凭据槽 `k8s-ro`、`result_path=("results",)`、`backend_<row_unit>_returned` 为行数；新模块 `opspilot/tools/helm_releases.py`（`HelmReleasesTransport`）。web 与 worker 开关不一致时 Run 在认领时被拦（B1/C5 同）。
- 视图其他字段：`helm_list` 带 `scope: {namespace, all_namespaces}`；三者都带 `dropped`。
- 模型可见说明（B4-16）：只改工具描述，不改调查提示词。描述写读的是 Helm 存在集群里的发布记录、给哪些字段、不给 values/manifest/hooks/notes、Helm 之外的改动看不到（结合 ReplicaSet 与事件）、历史不按事故框裁剪。

### 身份层（B4-4）

- 基础清单 `scripts/kind_lab/opspilot-readonly-rbac.yaml` 与 `docs/evidence/a1-cluster-identity/` 不动。新增 `scripts/kind_lab/opspilot-readonly-rbac-helm.yaml`：ClusterRole `opspilot-investigator-helm-releases`，唯一规则 `apiGroups: [""]`、`resources: [secrets]`、`verbs: [list]`；ClusterRoleBinding `opspilot-investigator-helm-releases` 绑到 `opspilot-readonly/opspilot-investigator`。只在开 Helm 的租机或 CI 上 apply。
- `scripts/k8s_authz_probe.py` 加 `--expect-helm-overlay`：三种 Secret list 期望 200，Secret get 与 `watch=true` 期望 403，其余同基础期望；探测只记状态码，200 的响应体读完即丢弃，不写报告、不打日志。

## CI job `k8s-backends`（R8-25；名字与变量定死，测试作者与实现者共用）

- `ubuntu-24.04`，`timeout-minutes: 15`，与其他 job 并行，scope 预检照 `k8s-live-metrics`。kind `v0.31.0`、`kindest/node:v1.35.0`，sha256 与 digest 与 `k8s-live-metrics` 同值。Helm 3 `v3.22.1`（2026-10-15）与 Helm 4 `v4.3.1`（2026-10-15；`v4.3.2` 是 11-13 发布，不满两周），二进制按官方 `.sha256sum` 校验，放 `$RUNNER_TEMP/helm3/helm`、`$RUNNER_TEMP/helm4/helm`。C4 往同一 job 加步骤。
- 集群：`kind create cluster --name b4 --config tests/fixtures/b4_helm/kind-config.yaml`。配置钉 `networking.apiServerAddress: 127.0.0.1`、`apiServerPort: 36443`，并挂审计：策略 `tests/fixtures/b4_helm/audit-policy.yaml`（`secrets` 记 `Metadata` 级，其余 `None`；不用 `Request`/`RequestResponse`，免得审计日志存 Secret 内容），日志在节点 `/var/log/kubernetes/audit/audit.log`，模式 `blocking`。
- 身份：apply 基础清单、Helm 追加清单、`tests/fixtures/b4_helm/base-only-sa.yaml`（namespace `opspilot-readonly` 里的 SA `opspilot-investigator-base`，ClusterRoleBinding `b4-base-only` 只绑基础 ClusterRole `opspilot-investigator-readonly`）。token 用 `kubectl create token --duration=1h` 写文件、`chmod 0600`，不进日志。
- 合成数据：`tests/fixtures/b4_helm/seed.sh`（参数无，读环境 `B4_HELM3_BIN`、`B4_HELM4_BIN`，用 runner 的管理员 kubeconfig），夹具 chart `tests/fixtures/b4_helm/chart/`（名 `b4-fixture`，只有 ConfigMap、Secret 与一个 hook ConfigMap 模板，不拉镜像）：
  - namespace `b4-a`，release `shop`，Helm 3：rev1 `install`（chart 0.1.0 / app 1.0.0）→ rev2 `upgrade`（seed 把 chart 拷一份改成 0.2.0 / 1.1.0）→ rev3 `upgrade --set failName=true`（模板渲染出非法 ConfigMap 名，apiserver 拒绝，rev3 = `failed`）→ rev4 `rollback shop 1`。期望状态 1 `superseded`、2 `superseded`、3 `failed`、4 `deployed`；rev4 chart 0.1.0、`rollback_revision=1`。
  - namespace `b4-b`，release `cart`，Helm 4：`install` 后 `upgrade --history-max 10` 12 次（共 13 个 revision），期望只剩 4–13，13 为 `deployed`。
  - namespace `b4-junk`（`kubectl` 直接建，类型都是 `helm.sh/release.v1`、标签 `owner=helm`）：`sh.helm.release.v1.junk.v1`（data 为垃圾，malformed）、`sh.helm.release.v1.mismatch.v1`（正文 name 与标签不一致，malformed）、`sh.helm.release.v1.bomb.v1`（gzip 炸弹，解压超 8 MiB，too_large）、`sh.helm.release.v1.applied.v1`（`kubectl apply` 建、正文合法，带 `last-applied-configuration` 注解与金丝雀注解、标签）。期望该 namespace 1 行（`applied`），`dropped = {malformed: 2, too_large: 1}`。
  - namespace `b4-a` 另有 Opaque Secret `plain-canary`（data 含 `CANARY-B4-OPAQUE`），任何工具请求都不该返回它。
  - 金丝雀（低熵，避开 gitleaks）：`CANARY-B4-VALUES`（values.yaml）、`CANARY-B4-SET`（`--set`）、`CANARY-B4-MANIFEST`（ConfigMap data，同时进 Helm 4 `info.resources`）、`CANARY-B4-HOOK`、`CANARY-B4-NOTES`（NOTES.txt）、`CANARY-B4-TEMPLATE`（模板注释）、`CANARY-B4-FILE`（chart `files/`）、`CANARY-B4-LABEL`（`--labels`）、`CANARY-B4-ANNOTATION`、`CANARY-B4-OPAQUE`。清单 `tests/fixtures/b4_helm/canaries.txt`，测试从该文件读。
- 写进 `$GITHUB_ENV`：`B4_K8S_API_URL=https://127.0.0.1:36443`、`B4_K8S_CA_FILE=$RUNNER_TEMP/b4/ca.crt`、`B4_K8S_TOKEN_FILE=$RUNNER_TEMP/b4/readonly.token`（带 Helm 追加权限）、`B4_K8S_BASE_TOKEN_FILE=$RUNNER_TEMP/b4/base-only.token`（无 Helm 权限）、`B4_KIND_NODE=b4-control-plane`、`B4_AUDIT_LOG=/var/log/kubernetes/audit/audit.log`、`B4_CANARIES=tests/fixtures/b4_helm/canaries.txt`。测试门控 `OPSPILOT_K8S_BACKENDS_LIVE=1`（本机跳过）。测试读审计用 `docker exec "$B4_KIND_NODE" cat "$B4_AUDIT_LOG"`，按 `user.username == system:serviceaccount:opspilot-readonly:opspilot-investigator` 且 `objectRef.resource == secrets` 过滤。
- 步骤顺序：起集群 → 装 Helm → apply 身份 → seed → setup-uv、`make setup` → 测试作者 `tests/integration/test_tool_expansion_b4_helm.py` → 实现者 `tests/integration/test_tool_expansion_b4_helm_impl.py` → 失败时打印 `kubectl get secrets -A --field-selector type=helm.sh/release.v1 -o name`（只列名字）。

### 集成断言清单（经真实 `KubernetesClient` 与执行器，不调模型）

- I-1 `helm_history(shop, b4-a)` 4 行，状态、chart、版本、appVersion、`rollback_revision`、description（rev3 含失败原因、rev4 为 `Rollback to 1`）与 seed 一致，时间非空且单调不减。
- I-2 `helm_status(shop, b4-a)` 一行 rev4；`helm_status(cart, b4-b)` 一行 rev13。
- I-3 `helm_history(cart, b4-b)` 恰为 rev4–13（Helm 4 记录可解码，`--history-max` 生效）。
- I-4 `helm_list()` 含 `shop`、`cart`、`applied` 三行且各为最新 revision；`helm_list(b4-a)` 只有 `shop`。
- I-5 `helm_list(b4-junk)` 1 行，`dropped = {malformed: 2, too_large: 1}`。
- I-6 不存在的 release 与 namespace：`no_data`，notes 含查的名字。
- I-7 `B4_K8S_BASE_TOKEN_FILE` 的 token：三个工具都是 `SOURCE_FORBIDDEN`（403）。
- I-8 金丝雀：`canaries.txt` 每一项都不出现在任何工具的 body、视图、notes、PG 里的 raw 与 view 证据、ledger、span 导出、`caplog`。
- I-9 来源时间与 `data_as_of` 为空，notes 第一条与 `cannot_prove` 如上。
- I-10 网关绕过表经真实 client 跑（无 fieldSelector；重复 fieldSelector 两种顺序；`type!=`、`type==`、值后追加条件；`FieldSelector`；`field%53elector`；`%73ecrets` 路径；`Secrets` 路径；`/secrets/<name>` 与其子路径；`watch=true`；`resourceVersion=0`；`;` 分隔；多余键；Accept 为 Table；`helm_page=False`；开关关时合法形状）：全部 `RESOURCE_NOT_ALLOWED`，前后审计日志里该 SA 的 secrets 事件数不变。
- I-11 名字或 namespace 恰好叫 `secrets` 的正常路径照常放行（在 `b4-a` 建名为 `secrets` 的 ConfigMap）。
- I-12 残余风险证据：同一 `readonly.token` 直接 `GET /api/v1/secrets`（无 fieldSelector）得 200，响应里能解出 `CANARY-B4-OPAQUE`（只断言、不打印）；`GET /api/v1/namespaces/b4-a/secrets/plain-canary` 得 403；`GET /api/v1/secrets?watch=true` 得 403。断言结果写进本记录「证据」。

## 合同测试（未参与实现的测试作者，本机替身）

文件 `tests/test_tool_expansion_b4_contract.py`，`http.server` 或间谍 opener 替身，覆盖预审「合同测试」① 至 ⑧：网关绕过表（断言 opener 零调用）、两层 base64 有无 gzip 与 Helm 3/4 夹具字节、金丝雀参数化（全部位置一个测试）、坏数据与炸弹、16 MiB 预算、`HELM_NOT_LAB` 与 `HELM_RELEASES_INVALID`、开关关时字节不变、`change_history` 移动。Helm 3/4 样本由 seed 同款命令在本地或 CI 生成后存为 `tests/fixtures/b4_helm/releases/*.json`（Secret 对象原样，金丝雀为低熵串）。

## 自建功能验收（R8-27 冒烟，不计分）

- 脚本 `scripts/b4_helm_scenario.py`，子命令：`setup --kubeconfig <工程师> --helm <bin> --namespace b4-demo --release web`（`helm create` 的 nginx chart，镜像钉 digest：rev1 install → rev2 改 tag upgrade → rev3 换成不存在的 tag `--wait --timeout 60s`，得 `failed` 与 ImagePullBackOff；values 放金丝雀 `CANARY-B4-SCENARIO`）；`check --summary <summary.json> --evidence-dir <dir>`（判定报告引用了 `helm_history` 或 `helm_status` 的证据、证据里有 revision 与时间、金丝雀在证据目录与 `summary.json` 里搜不到）；`teardown`。
- 提问：「这个 namespace 最近一次发布是什么时候」「这个 namespace 的服务为什么起不来」，各跑 1 遍，放 #669 检查点阶段。环境：web 与 worker 都设 `OPSPILOT_TOOL_PROFILE=otel-demo-ext`、`OPSPILOT_HELM_RELEASES=1`，worker `OPSPILOT_K8S_API_URL=https://127.0.0.1:<port>`。

## 这个 PR 做什么

`kubernetes_core.py` 网关与 `KubernetesRequestRefused`；`helm_releases.py`；`otel_demo_ext.py`、`profiles.py`（开关、`HELM_RELEASES_INVALID`、`HELM_NOT_LAB`）；`docker-entrypoint.sh`；追加 RBAC 清单与探测模式；`tests/fixtures/b4_helm/`；CI job `k8s-backends`；C3 §8 补一句「Helm 发布历史（B4）按对象当前状态，不按框裁剪」（B4C4-U1 落实）；B/C 记录登记数据源（草稿如下）。网关属安全边界变更，完成前独立审查。

## 新数据源登记草稿（合并时移入 B/C 记录）

- B4（#563，只在 `otel-demo-ext`、web 与 worker 都设了 `OPSPILOT_HELM_RELEASES=1`、且 worker 的 K8s API 地址是 lab 主机时）：同一 kube-apiserver 的 `GET /api/v1[/namespaces/{ns}]/secrets?fieldSelector=type%3Dhelm.sh%2Frelease.v1&labelSelector=owner%3Dhelm[%2Cname%3D…]&limit=5[&continue=…]`，用现有 `k8s-ro` 只读身份（另加 `secrets` 的 `list`，见残余风险：该身份可读取合成环境内全部 Secret 的内容，只靠 OpsPilot 网关收窄到 Helm 发布记录）。工具在 worker 内存中解码，只返回发布元数据（release 名、namespace、revision、状态、chart 名与版本、appVersion、首次与最近部署时间、删除时间、description、回滚来源）；values、manifest、hooks、notes、chart 内容、部署对象快照、Secret 注解与标签不出工具，也不落盘。来源时间留空（当前状态类，B4C4-U1）。`may_contain_secrets=True`：投影后的响应存入证据库（PG），视图经现有脱敏后进入 Run 所选模型；lab 内随 LangSmith trace 导出（`KUBERNETES_NOT_LAB`、`HELM_NOT_LAB`）。不新增接收方。

## 实现决定（可逆，与预审的差异及理由）

- 开关值用 `1` 不用 `on`：与 B3、C1、C2b 实际实现一致（都是 `1`/`0`/空、其他值 `*_INVALID`），同一档位一种写法。
- 网关从「白名单内可选键」收紧为「键集合固定、恰一次、值全等」并要求原始串等于重编码：r11 裁决原文；重编码比较让编码变体无需逐个枚举。
- 网关另加 `helm_page` 调用标记：即使将来有 bug 拼出 Helm 形状的 URL，jq/tabular 等拿完整对象的工具也取不到 Secret。
- 资源位的 `watch`、`proxy` 对所有请求拒绝：旧式 watch 路径会把 `secrets` 挪到别的位置，现有工具不发这类路径（`git grep` 核实，`opspilot/` 里没有拼 watch/proxy 路径的代码）。
- `helm_history`/`helm_status` 必填 `namespace`：同上游 CLI 参数；`helm_list` 的 `namespace` 可选（R17）。
- 时间到秒、UTC：与 B3 一致，`helm history` 本身也只显示到秒。
- CI 钉 apiserver 端口 36443、审计 `Metadata` 级：测试与 seed 不必从 kubeconfig 取端口；审计只需 requestURI 与用户，不存内容。

- 实现时补的（可逆）：
  - 档位位置：C6a、C3 实际排在 `fetch_skill`/`TodoWrite` **之后**，「全部后端之后且在这两者之前」做不到；Helm 排在实时用量之后、`fetch_skill` 之前，SQL、Kafka 不动（不动它们的 revision）。`change_history` 键插在 `victorialogs` 之后、`knowledge` 之前；`evidence_context` 经 `canonical` 后键按字母序，位置在面上不可见。
  - 超页数或超 16 MiB 时，结果不是通用的 transport 拒绝，而是 `source_status=RESULT_TOO_LARGE` 加一条 note，提示给 `namespace`/`release_name`。为此 `registry.SOURCE_ERROR_REASONS` 加了 `RESULT_TOO_LARGE`：通用拒绝只说「缩小窗口或 limit」，对这三个工具没用。
  - 「queried at」用执行器时钟（`KubernetesTransport(now=clock.now)`）：产品代码禁用 `datetime.now`。
  - `chart`、`chart_version`、`description` 缺字段时为 `null`；字段存在但不是字符串，整条按 malformed 丢弃。`status` 缺字段同样按 malformed。
  - 页面要求 `kind == "SecretList"` 且 `items` 必须存在（缺 `items` 为 `MALFORMED_RESULT`；`_page` 把缺省当作空列表，这里不复用它）。
  - 自建场景的金丝雀放在模板不渲染的 values 键里：放进 Pod 注解的话，别的集群工具能合法读到，检查会误报泄漏。nginx 镜像只钉 tag，不钉 digest：`helm create` 的模板不支持 digest。
  - safety：加 `tests/safety/test_safety_helm_releases.py`，以及变异 M39（放开网关的 Secret 收窄）、M40（把 values 拷进行）。

## 证据

- revision（本分支实测）：开关关时现有值不变（无后端 `c63f058afb18`、仅实时用量 `4730c27997c9`，与 B3 记录一致）；只开 Helm 为 `otel-demo-ext-ec596e186760`，Helm 加实时用量为 `5887c48a3d72`，Helm 加 ES 为 `3ef779d06145`，Helm 加 Loki 为 `cfb67cba921f`。
- 本机（共享 `.venv`）：测试作者合同测试 637 条中 626 过、11 失败（见「待决」第 2 项，均属断言与接口或替身行为不符，未改断言）；`tests/safety` 加 B1/B3/C1/C2b/C5a/C5b/C6a 合同、`test_tracing`、`test_architecture`、`test_m1_otel_demo_contract` 等共 2354 过；新 safety 7 过；变异 M39、M40 均被抓到。ruff 通过。mypy 只在 `kafka_admin.py` 报 6 处，C3 合同测试 1 条失败，都因为共享 venv 没装 `confluent_kafka`，属环境问题，与本改动无关。
- 集成测试本机跳过，交给 CI `k8s-backends`。

## 待决

1. **F11 第 4 步代码面论证与 B4 冲突（需 lead/用户裁决）**：`tests/acceptance/test_f11_step4_verdicts_no_gate.py::test_item3_no_product_module_names_kubectl_or_a_cluster_ci_or_forge_api` 禁止产品模块的字符串常量出现 `\bhelm\b`、`\bkubectl\b`，A2 只给 `kubernetes_core.py` 的读路径开了例外。B4 有三处绕不开：`kubernetes_core.HELM_FIELD_SELECTOR`（`type=helm.sh/release.v1`）、`HELM_OWNER`（`helm`），以及 `helm_releases._CANNOT_PROVE` 里的 `kubectl`（测试作者断言要求 `cannot_prove` 含 kubectl）。其余文字已改写避开。按 A2 先例（X1/Q1/Q2 后由独立测试作者修订论证），建议由独立测试作者把例外扩到这三处：只读的 Helm 记录选择器，加上 `cannot_prove` 说明文字。验收步骤原文不改。
2. **测试断言与接口不符（未改，列给 lead）**：
   - `test_other_secrets_are_never_read_even_when_present`：断言 `fieldSelector == ["helm.sh/release.v1"]`。接口与 `LEGAL` 表里都是 `type=helm.sh/release.v1`，断言漏了 `type=`。
   - `test_a_bad_entry_is_dropped_counted_and_never_echoed[owner-label-missing / owner-label-not-helm]`：替身 apiserver 按 `labelSelector=owner=helm` 过滤（真实 apiserver 也是），这两条记录根本不会返回，`malformed` 只能是 0。工具里的 owner 校验是纵深防御，这两个用例测不到它。
   - `test_switch_on_adds_exactly_the_three_tools_and_the_change_history_source[8 个组合]`：断言 `available` 的键顺序是 `change_history` 排在最后一个后端之后，但 `_surface` 经过 `canonical`，键按字母序。本机核对过同一测试的其余断言，按集合比较全部成立。
