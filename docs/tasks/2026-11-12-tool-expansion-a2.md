# 工具面拓展 A2：扩展档位与集群对象只读查询

- 状态：实施中（独立合同测试已合入；剩余一条按裁决由测试作者改写）
- 更新日期：2026-11-13
- 依据：[工具面拓展任务记录](2026-11-12-tool-expansion.md)、[决定表 r3](2026-11-12-tool-expansion-contract.md)（r1 用户裁决，r2 的 A2-1 至 A2-15；r3 是后续各项对 A2 的要求）、SPEC 2026-11-12 段、C3 第 3、8、12 节；上游 HolmesGPT `a0009195` 的 `kubernetes/core`。issue #554。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-a2-impl`，分支 `feature/tool-expansion-a2-k8s-core`（起点 `origin/main` `52d640a0`）。

## 目标

调查能只读查询被调查集群里的对象与事件。新增扩展档位 `otel-demo-ext`：`otel-demo` 的全部工具加两个新工具。`otel-demo` 档位的工具面与版本一个字节不变。

## 行为（公开接口）

- `kubernetes_jq_query(kind, jq_expr, namespace?, label_selector?, field_selector?)`：列出某类资源，在取回的对象上跑模型写的 jq 表达式，返回结果列表。
- `kubernetes_count(同样的参数)`：jq 过滤后计数，返回匹配总数与前 10 条预览。
- 名字与参数照搬上游。`kind` 是复数资源名，只认静态表里的；事件用 `kind=events` 查。
- `namespace` 不传就是整个集群。
- 问 Secret：直接拒绝，不向集群发请求，模型看到明确的拒绝原因。
- 对象是查询那一刻的状态，工具描述写明它不能证明事故发生时的状态；事件只返回授权时间框内的。
- 结果超过大小上限时整条拒绝，不返回部分结果。
- 配置只在 worker 读：`OPSPILOT_K8S_API_URL`、`OPSPILOT_K8S_TOKEN`、`OPSPILOT_K8S_CA_FILE`、`OPSPILOT_K8S_NAMESPACE`。worker 选了扩展档位但缺配置或找不到 jq 时启动失败。
- 读配置的边界：web 也会选档位（启动时取版本与工具面），所以扩展档位的版本与工具面必须在没有任何 `OPSPILOT_K8S_*` 时也能构造；Kubernetes 配置只在 worker 构造执行器时读。合同测试覆盖两条：web 没有这些变量能正常启动；worker 缺配置时以固定代码启动失败。
- 凭据不串用：执行器会把目标的凭据引用放进每个请求（`opspilot/tools/executor.py:860-864`），Kubernetes 传输不用它，只用自己的凭据槽 `k8s-ro`；Prometheus、Jaeger、Kubernetes 三个后端互不回退。合同测试证明：Kubernetes 请求不带 Prometheus 或 Jaeger 的凭据，反过来也一样；缺 `k8s-ro` 时 Kubernetes 工具报凭据错误，不拿别的凭据顶替。

细节（分页、jq 子进程的限制、错误分类、时间字段、原因码）以决定表 A2-1 至 A2-15 为准。

## 这个 PR 做什么

- 独立模块 `opspilot/tools/kubernetes_core.py`：两个工具的 schema 与注册（含五段模型可见描述）、配置、只发 GET 的 HTTPS 传输、静态资源表、jq 执行器。不往 `otel_demo.py` 里加。
- `profiles.py` 加档位；传输路由改成按工具分派；`registry.py` 允许登记可能含秘密的数据源；两个新原因码；LangSmith lab 闸门认新档位；runtime 镜像装 jq。
- 合同测试对着替身 API 响应写，由未参与实现的测试作者按本记录、决定表与 C3 编写，不看实现代码。
- 按用户裁决改写两处现有测试：`tests/test_m1_tool_registry.py` 里「可能含秘密的来源在登记时被拒绝」改为允许声明为真、非布尔值仍拒绝；只读安全验收里「每个出厂工具都是有界只读」那条加上扩展档位的注册。

## 后续各项对 A2 的要求

来自 A1、A3/A6、A4/A5 的开工前预审（决定表 r3）。不改 A2 的行为，只约束实现形状，免得后续各项返工：

- 传输路由按显式工具名分派，不按 `kubernetes_` 前缀匹配：`RoutedTransport` 现只有两路（`opspilot/tools/evidence_read.py:373-386`）；A3 的 `fetch_pod_logs` 不带这个前缀，前缀匹配会把它送到 otel-demo 传输。
- 配置、GET 传输、静态资源表、分页与 4 MiB 累计、错误映射、事件按授权框筛选做成可复用函数，A3（Pod 日志）与 A6（按列查询）直接调用；A6 要复用「取一类资源的全部对象（已筛事件）」这一步，事件筛选不共用会让按列查询绕过授权框（A6-1）。
- 定下 jq 超时报哪个结果类（A2-8 只写了超时用剩余时间）；A3、A6 的正则子进程沿用 jq 子进程的约束，超时报同一个类（A3-7、A6-4）。
- 新工具名不加进参与 `otel-demo` 版本哈希的集合：`PROMETHEUS_TOOLS`（`opspilot/tools/otel_demo.py:179`）进了 otel-demo 的 `data_sources` 哈希（`otel_demo.py:1047-1083`，A5-7）；Kubernetes 与后续工具名同理只进扩展档位。
- 容器里的 worker 拿集群凭据归 A1，不在 A2：部署入口脚本的变量放行名单只导出 `OPSPILOT_OTEL_*` 与 `LANGSMITH_*`（`deploy/docker-entrypoint.sh:54-60`），CA 文件要靠 lab 覆盖 compose 挂进只读的 worker 容器（`deploy/compose/compose.yaml:18-30,130-153,213-231`），重发 token 时要替换而不是保留旧值（`scripts/kind_lab.py:558-570` 的 `_merge_env`）（A1-7）。A2 只负责 worker 读这四个变量；新加的 API 地址 lab 检查放进 `check_lab_target`（`opspilot/tracing.py:181`），与 Prometheus、Jaeger 两项并列（A1-8）。
- 视图字段键名：A3 的 `lines_read` 等、A6 的 `total_items_processed` 要能与 A2 用到的通用视图字段并存，A2 落地后对照一次。

## 不做

按列查询（A6）、Pod 日志（A3）、提示词里的 Kubernetes 调查指导（A7）、扩展档位的评测回放、真实 Run 脚本的档位选项、`events.k8s.io` 组、watch、自定义资源、Pod 环境变量值脱敏（#574）、工作台专门视图、token 自动续发。

## 完成条件

1. 代码 PR：合同测试通过，CI 通过，`otel-demo` 档位的钉值测试与结构回放不变。这是功能 PR。
2. 代码 PR 合并后 #554 关闭。真实证据归租机集中取证（#580）：租机后的真实集群上，worker 切到扩展档位、注入故障后提问，工作台证据里出现集群对象查询，报告引用了 Pod 状态或事件；问 Secret 看到明确拒绝；仓库里有 `summary.json` 与 LangSmith trace 链接。

第 1 条不需要集群，在本机完成。第 2 条与集群只读身份的拒绝取证（A1）等一次租机集中做。

## 没有真集群时的已知风险

替身响应按官方 Status 结构写，下列各项到真集群上取样核对，可能返工：

1. worker 连 API server 用的地址、证书名是否对得上，lab 闸门是否认这个地址。
2. 错误体形态：403、namespace 不存在、翻页 token 过期（410）。
3. 整个集群的列表体积是否碰到 4 MiB 与 2 MiB 上限。
4. 事件的时间字段常有空值。
5. 镜像里 jq 的版本。

## 本机约束

本机内存紧张：不跑 `make check`、全量 pytest，不起 Docker、colima、kind。定向测试用 `.venv/bin/python -m pytest <文件>`，全量交给 CI。

## 实施记录

落点：`opspilot/tools/kubernetes_core.py`（两个工具、配置、GET 客户端、资源表、受限子进程）、`opspilot/tools/otel_demo_ext.py`（档位组合）、`profiles.py`、`evidence_read.py` 的 `RoutedTransport`、`registry.py`、`outcomes.py`、`tracing.py`、`Dockerfile`；实现者单元测试 `tests/test_554_kubernetes_core_impl.py`。

自行决定的可逆细节：

- 档位组合单放 `otel_demo_ext.py`：A3–A6 往这里加工具，不让 `kubernetes_core` 反向依赖后续模块。
- `RoutedTransport` 加可选 `kubernetes=`（两个集群工具名精确分派，合同测试的接缝）与通用 `routes`，都不传时行为不变；`read_evidence` 不能被路由覆盖。
- 两个新原因码进 `ERROR_REASONS` 与 `SOURCE_ERROR_REASONS`，结果类为 `error`（按合同测试；R11「拒绝并可见」未指定结果类）；`PROJECTION_REVISION` 不升，`otel-demo` 视图字节不变。
- jq 超时报 `timeout/TOOL_TIMEOUT`、来源接触 `confirmed`（已读过集群）。A3、A6 的子进程用同一个 `run_isolated`。
- 每次调用只起一次 jq（A2-9，合同测试计进程数），所以编译错误在读集群之后才知道，报 `JQ_COMPILE_ERROR`；运行期错误（退出码 5）报 `JQ_RUNTIME_ERROR`，均归 `INVALID_PARAMS`；其他失败 `JQ_FAILED` 归 `SOURCE_ERROR`。表达式写进临时文件用 `-f` 传，不进命令行；`-L` 指向空目录。
- 交给 jq 的是 `{"apiVersion":"v1","kind":"List","items":[...]}`，每个对象补 `kind`、`apiVersion`，与上游 `kubectl get -o json` 形状一致，`.items[]` 写法可直接用。
- Secret 判定：`kind` 小写后含 `secret` 即 `RESOURCE_NOT_ALLOWED`（覆盖大小写、单数、带组后缀）；表外名字报 `UNKNOWN_KIND`（`INVALID_PARAMS`）。`kind` 区分大小写精确匹配。
- 资源表：A1 清单去掉子资源、`events.k8s.io`、`metrics.k8s.io`、`extensions` 组与 `podsecuritypolicies`，同名优先核心组、其次当前在服的组（deployments 等走 `apps/v1`，HPA 走 `autoscaling/v2`）；表进档位版本哈希。
- 事件时间取第一个可用的 `lastTimestamp`、`eventTime`、`series.lastObservedTime`、`firstTimestamp`、`metadata.creationTimestamp`；无时间的丢弃并计数；来源时间取保留事件首尾（与 A6-6 一致），其他资源留空。
- 视图字段：`total_items_processed`（与 A6 同名同义）、`pages_read`、`scope`、`notes`、`cannot_prove`；计数工具另有 `matches_found`，并用 `row_unit="matches"` 出 `backend_matches_returned` 等计数。
- 启动失败固定码（worker 构造执行器时以 `SystemExit` 退出）：缺变量 `K8S_CONFIG_MISSING`；地址非 https、namespace 非 DNS 标签、CA 文件不可读 `K8S_CONFIG_INVALID`；找不到 jq `JQ_NOT_FOUND`。缺 `k8s-ro` 凭据在传输层报 `CREDENTIAL_REF_UNKNOWN`（同 otel-demo 传输），不发请求。
- lab 闸门新码 `KUBERNETES_NOT_LAB`：扩展档位必须有 lab 主机的 API 地址；其他档位只在设了该变量时检查。
- `OPSPILOT_K8S_NAMESPACE` 只作提示，放进 selector 与视图 `scope.application_namespace`，不收窄查询（R17）。
- Dockerfile 在 runtime 阶段加一行装 `jq`（未钉版本）。

偏离与已知限制：

- 已裁决（编排者 2026-11-13）：集群工具操作记录的 `credential_ref` 按 A2-1 仍写目标的 `otel-demo-ro`，改合同测试，实现不动。
- 已裁决（编排者 2026-11-13）：合同测试的 CA 路径改用 `tmp_path` 文件，实现保留配置期 CA 可读检查；两个新原因码归 `error`、去掉 jq 编译预检均接受。

- CA 文件内容在配置期只查可读；载入失败时 TLS 上下文没有任何信任根，每次握手失败报 `SOURCE_UNAVAILABLE`，不回落到系统根证书。没在启动时拒绝，是为了让替身测试用假 CA 文件也能构造执行器。
- jq 与 API 的错误文本不回显给模型（只有 HTTP 状态）；模型改表达式只能看原因码。
- 内存上限用 `preexec_fn` 设 `RLIMIT_AS`，macOS 上设不上就跳过；worker 多线程下 `preexec_fn` 只做一次系统调用。
- `read_evidence` 的模型可见描述是 `otel-demo` 字节，没写 Kubernetes 结果的行单位；超限结果仍可按 `results` / `preview` 切片读回。
- 本机未构建镜像，镜像里 jq 版本未确认；按开工说明，两处现有测试的改写归测试作者，在本分支上 `test_m1_tool_registry.py` 的拒绝断言按预期失败。

## 下一步

由实现会话接手：开 worktree，派测试作者与实现者，按 AGENTS.md 的功能 PR 流程走。
