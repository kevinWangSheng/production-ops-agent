# 工具面拓展：决定表（r7）

- 修订：r1，用户 2026-11-12 裁决（口头，约 08:00–08:50 PT；同日下一会话复述后确认）。r2（2026-11-12）：集群对象只读查询（A2，#554）开工前预审，没有新的用户裁决项，追加 15 条可逆默认并改写 R6、R17，见文末。r3（2026-11-12）：A1、A3、A4、A5、A6、B2 开工前预审，追加各项可逆默认与 N3 结论；B2-1 由用户 2026-11-12 裁决为 a（对所有账号放开），其余没有新的用户裁决项，见文末。r4（2026-11-12）：ITBench-Lite 适配开工前预审，记入用户四条对齐与 16 条默认，L6 待用户裁决，见文末。r5（2026-11-13）：用户裁决 L6 = a（用仓库已有接收方），并确认 A2-14 维持默认（不在代码里强制）。
- 归属：[任务记录](2026-11-12-tool-expansion.md)。选项与代价的全文在仓库外的预审稿（Kubernetes 工具预审第 2 版，2026-11-11），本表只留裁决与落点。
- 预审的推荐按「单 namespace 只读」写成；N4 裁决为整个集群只读后，受影响的可逆默认已在下表改写，A1、A2 各自的预审按集群级核对。

## 用户裁决

| 编号 | 问题 | 裁决 | 落点 |
|---|---|---|---|
| X1 | 用户标注「不修改现有产品安全合同」是否挡住 SPEC 与 C3 的开放 | 不挡。SPEC「产品不持有 Kubernetes 凭据」直接改；PRODUCT-CONSTRAINTS 按 Q8、D3、Q3 改三处 | SPEC 2026-11-12 段 |
| Q1 | 产品是否持有 Kubernetes 凭据 | 扩展档位持有一份只读集群凭据；A1 的拒绝取证挂为扩展档位证据 | SPEC 2026-11-12 段（1）；C3 §3 |
| N4 | 只读范围 | 整个集群只读，含 nodes、事件、kube-system；Secret 不可读。不走预审推荐的单 namespace | SPEC（1）；C3 §8 |
| Q2 | 模型写 jq 表达式 | 允许；独立子进程、清空环境变量、超时、输出上限、不加载外部模块 | C3 §8 |
| Q3 / N5 | 可能含秘密的数据源能否登记 | 能。脱敏不作为加工具的前提；返回仍先过现有脱敏；裸密钥为已写明的残余风险；只接合成 lab 或受控快照。Pod 环境变量值的脱敏不先做（#574） | PRODUCT-CONSTRAINTS「Runtime and human control」；C3 §8 |
| Q4 | 集群当前状态没有事故时间窗 | 时间记为查询时刻，`cannot_prove` 写明不能证明事故窗内状态 | C3 §8 |
| Q5 | 注入故障的开关 ConfigMap 是否可读 | 可读并披露；开关类场景标出，区分度不够就换场景，不改授权 | C3 §8；SPEC「Verification and delivery」 |
| Q6 | Chaos Mesh 等自定义资源 | 现在不授权，留到现场版预审（#564） | C3 §8 |
| Q7 / D1 | Helm 发布记录存在 Secret 里 | 先不读；变更历史来自 ReplicaSet 与事件，Helm 变更历史（#563）降级 | C3 §8 |
| Q8 / D2 | 新数据源是否逐个报批 | 去掉逐个报批。合成 lab 或受控快照内新增的只读数据源直接按数据流合同进入所选模型，在任务记录登记返回什么、发往哪里。新增接收方、lab 以外的数据、外部服务仍由用户决定 | PRODUCT-CONSTRAINTS「Data flow contract」；C3 §12 |
| D3 | 模型自写 SQL | 不在策略层禁止，从排除项拿掉。只读由工具保证：数据库只读账号，语句检查作第二道。数据库只读查询（#570）仍排第三批，lab 没有 SQL 数据库。任意 shell 与云命令照旧排除 | PRODUCT-CONSTRAINTS「Explicit exclusions」；C3 §8 |

仍未决定、不挡 A0：N1 Lite 快照如何映射到查询端点（探针 #571）；N2 现场版与 Lite 同号场景是否一致（未确认，不合并分数）；N3 已有结论（读 Jaeger v1.53.0 源码，见 r3「N3」；未在真实实例取样的项照列）；N6 工具 search/catalog（有运行数据再定）。

## 可逆默认（实现者自定）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| R1 | 用 `kubectl` 还是直接打 API | 直接 HTTPS GET 打 API server，Bearer token 加 CA 文件 | 复用现有 GET-only 传输；镜像不加 `kubectl`、不出现 kubeconfig |
| R2 | 扩展档位的组成 | 新档位，复用 `otel-demo` 的全部工具（字节不变）再加新工具 | 不动现有档位版本 |
| R3 | 凭据引用 | 每个后端独立 `credential_ref` 与 `OPSPILOT_K8S_*` 变量，不互相回退 | C3 §12 凭据分离 |
| R4 | token 形态 | `kubectl create token` 的有期限 token，放 lab 私有目录 | 不留长期 Secret |
| R5 | 集群身份的动词与范围 | ClusterRole 加 ClusterRoleBinding，规则照上游默认规则（无 `secrets`），只给 `get`、`list`，不含 `watch` | N4 裁决为集群级；工具不用 watch。C3 第 8 节写的「get、list、watch、logs」是上限，扩展档位按最小授权不给 watch，不冲突 |
| R6 | 分页与资源解析 | 每页 500；资源名用静态表解析（r2 改写，见 A2-5、A2-9） | 没有真集群时 API 发现风险最高；Secret 与未授权资源自然不在表里 |
| R7 | 对象投影 | 去掉 `metadata.managedFields`，其余原样交给 jq | 纯噪声且体积大 |
| R8 | 日志条数 | 默认 100，硬上限 1000，超出标「不完整」与省略条数 | 同上游默认 |
| R9 | 多容器与上一次日志 | 全部容器，当前与上一次各取一次 | 照搬上游 |
| R10 | 模型给的日志过滤正则 | Python `re`，不区分大小写，在请求期限内执行（r3：只管 Pod 日志；按列查询的过滤照上游区分大小写，见 A6-4；执行方式见 A3 的正则子进程） | 照搬；防病态正则 |
| R11 | 错误分类 | 401/403 记为拒绝并可见，404 记为无数据或参数错误 | 沿用现有五类结果 |
| R12 | Kubernetes 调查指导 | 搬上游 6 条，去掉依赖 `kubectl_describe` 的措辞，只在扩展档位生效 | 只动扩展档位的提示词版本 |
| R13 | 已公布结果的标注 | 结果页与作品集加一句「两家均无 Kubernetes 与日志工具，基础档位」 | 照实宣称 |
| R14 | 覆盖页的 `change_history` | 阶段 A 仍标未集成 | 等 Helm 变更历史（#563） |
| R15 | Jaeger 新工具命名 | 沿用 `traces_` 前缀，参数名尽量取上游 Tempo 工具的 | 上游没有 Jaeger 工具集 |
| R16 | `kubernetes_tabular_query` 列语法 | 支持上游文档写明的 `HEADER:PATH` 子集 | 不引入 `kubectl` |
| R17 | 模型传的 `namespace` | 不传就是整个集群，同上游；传了就只查那个 namespace（r2 改写，见 A2-7） | N4 裁决后集群内都在授权范围；r1 的写法让模型没法表达「查所有 namespace」 |

## r2：集群对象只读查询（A2）的可逆默认

预审者未参与实现，对照 `main` `74699fb1` 的代码核对；全量探针经 CI 跑过（草稿 PR #576，已关闭）：扣除一条按 Q3 预期失败的测试，0 失败，`otel-demo` 档位字节不变。完整范围与接口见 [A2 任务记录](2026-11-12-tool-expansion-a2.md)。

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A2-1 | 一个 Run 只绑定一个目标，执行器要求工具来源等于目标来源 | Kubernetes 工具登记在现有 otel-demo 目标下，`source="otel-demo"`；API 地址放 selector 的 `kubernetes_endpoint`；token 用 Kubernetes 传输自己的凭据槽 `k8s-ro` | 执行器与调查循环不改；代价是证据的 `source` 写 `otel-demo`，靠工具名区分后端 |
| A2-2 | 档位名与版本 | 档位 `otel-demo-ext`，`tool_schema_revision` 为 `otel-demo-ext-<hash>`；工具面是 `otel-demo` 全部工具加 2 个新工具；`kubernetes_state` 从未集成移到可用 | 名字不绑死 Kubernetes，A3–A6 也进这个档位 |
| A2-3 | web 也会选档位 | Kubernetes 配置只在 worker 读；web 不需要任何 `OPSPILOT_K8S_*`；worker 缺配置时启动失败并报固定代码 | 集群凭据只在 worker 里 |
| A2-4 | 环境变量 | `OPSPILOT_K8S_API_URL`、`OPSPILOT_K8S_TOKEN`、`OPSPILOT_K8S_CA_FILE`、`OPSPILOT_K8S_NAMESPACE`（默认 `otel-demo`） | 都在只读安全验收允许的前缀里 |
| A2-5 | 资源名解析 | 静态表：复数资源名到 API 路径与是否按 namespace，内容取上游默认 ClusterRole 规则；`kind` 精确匹配复数名，同名多组时优先核心组；表外名字在发请求前拒绝 | 见 R6；要加资源就改表、升版本 |
| A2-6 | 问 Secret 怎么拒绝 | 加两个固定原因码：`RESOURCE_NOT_ALLOWED`（问的是 secrets，含大小写与单数变体，不发请求）与 `SOURCE_FORBIDDEN`（API 返回 401 或 403）；表外其他名字记 `INVALID_PARAMS` | 拒绝要让模型看得见；探针证实加这两个码不破坏现有测试 |
| A2-7 | `namespace` 省略 | 整个集群；传了就校验是 DNS 标签；集群级资源忽略 namespace 并在 notes 写明 | 见 R17 |
| A2-8 | jq 怎么跑 | runtime 镜像装 `jq`；参数列表启动不经 shell；环境变量为空；工作目录与模块目录都是空目录；表达式含 `import`、`include` 就拒绝；最长 2000 字符；超时用本次调用剩余时间；输出超 2 MiB 杀掉并报 `RESULT_TOO_LARGE`；内存上限 512 MiB；编译错误报 `INVALID_PARAMS`；worker 启动时找不到 jq 就失败 | Q2 要求的独立子进程；镜像里 jq 的版本未确认 |
| A2-9 | 分页与累计上限 | 每页 500 翻到底，合并后只跑一次 jq；原始读取累计上限 4 MiB，超了整条报 `RESULT_TOO_LARGE`，不返回部分结果；翻页与 jq 共用 30 秒；每个对象去掉 `managedFields` | 部分结果会让计数出错；上游每页跑一次 jq，聚合会算错 |
| A2-10 | 返回形态与计数 | jq 工具返回 `{"results":[...]}`；计数工具返回前 10 条预览（每条截到 200 字符）与匹配总数；处理条数、范围、备注放进视图字段 | 上游返回的键照搬，计数沿用现有「单位、显示、省略」规则 |
| A2-11 | 时间 | 对象：来源时间字段留空，观察时间是执行器记下的查询时刻，`cannot_prove` 写明不能证明事故窗内状态。事件：跑 jq 前只留授权时间框内的，丢掉的条数写进 notes | 若把查询时刻报成来源时间，会晚于授权框结束而被执行器拒绝 |
| A2-12 | 错误分类 | 400、422、404 报 `INVALID_PARAMS`；401、403 报 `SOURCE_FORBIDDEN`；410 报 `SOURCE_ERROR`；429 与 5xx 报 `SOURCE_UNAVAILABLE`；不跟重定向、不走代理 | 全部落在现有固定结果类 |
| A2-13 | LangSmith 的 lab 闸门 | 允许名单加 `otel-demo-ext`；加一项检查，Kubernetes API 地址必须是 lab 主机 | 内容导出只限 lab；worker 实际用的 API 地址未确认，等集群取证（A1） |
| A2-14 | 「只接 lab 或受控快照」是否在代码里强制 | 不强制；`may_contain_secrets=True` 只是声明，由 lab 闸门与运行规则约束 | 照实宣称即可，不为此搭技术隔离 （用户 2026-11-13 确认） |
| A2-15 | 合并与关闭时机 | 代码 PR 凭替身 API 响应加合同测试即可提交；#554 在代码 PR 合并时关闭；租机后的真实 Run、trace 与 `summary.json` 归租机集中取证（#580，2026-11-12 拆 ticket 时改） | A2 不碰调查循环、报告校验与恢复路径 |

## r3：A1、A3、A4、A5、A6、B2 开工前预审

三份预审在仓库外（A1；A3 与 A6；A4、A5 与 B2，均 2026-11-12），预审者未参与实现，对照 `main` `bda337b9` 与上游 HolmesGPT `a0009195` 核对。A2 尚未实现，下文引 A2 的都是 r2 的约定，不是代码。各项范围与接口见各自开工说明：[A1](2026-11-12-tool-expansion-a1.md)、[A3](2026-11-12-tool-expansion-a3.md)、[A4](2026-11-12-tool-expansion-a4.md)、[A5](2026-11-12-tool-expansion-a5.md)、[A6](2026-11-12-tool-expansion-a6.md)、[B2](2026-11-12-tool-expansion-b2.md)。

- A1、A3、A4、A5、A6 没有需要用户裁决的项。
- **B2-1 已由用户裁决为 a**（2026-11-12），见下方 B2 小节。
- A1 对「上游默认规则」取窄读法：只用上游模板里不带条件的规则，上游默认打开的自定义资源授权（Argo、Flux、Istio 等）、`monitoring.coreos.com` 与 openshift 块都不搬，与 Q6 一致；lab 没装这些 CRD，对行为无影响。
- 全量探针都未跑（A3、A6 与 A4、A5、B2 两份预审均受「不开 PR、不跑全量 pytest」约束）。须在 A2 合并后各开草稿 PR 经 CI 补跑，结果补进本节。预计：A3、A4、A5、A6 只加扩展档位，0 失败；B2 预计失败即 B2-1 列出的 4 个钉 403 的测试文件。
  - A4（2026-11-13，探针骨架 `a035c1da`，草稿 PR #592）：全量 CI 1 failed / 11985 passed / 2550 skipped。唯一失败是 `tests/test_tool_expansion_a2_contract.py` 断言扩展档位工具集恰好等于 otel-demo 加两个 Kubernetes 工具，与 A2-2「A3–A6 也进这个档位」冲突；已在 A4 分支由测试作者改为超集断言（`e266ff22`）。
  - A5 已补跑（探针草稿 PR #591）：2 failed / 11984 passed / 2550 skipped。两个失败都是 A2 测试把扩展档位钉死为只多 A2 两个工具：`tests/test_tool_expansion_a2_contract.py` 的全集断言，与 `tests/test_554_kubernetes_core_impl.py:421` 的位置断言。A5 代码 PR #593 已把前者改为 ⊇ 形式，后者改为不依赖位置；`otel-demo` 字节不变。
  - B2（2026-11-13，探针骨架 `4df603ab`，草稿 PR #595）：全量 CI 5 failed / 12213 passed / 2550 skipped。5 个失败都是钉旧拒绝的测试：`tests/acceptance/test_396_proxy_contract.py` 的 f7 探针行、`tests/test_kind_lab_readonly_proxy.py` 的表单 POST 用例、`tests/test_399_isolation_impl.py` 1 条、`tests/acceptance/test_399_isolation.py` 2 条。后两个文件不在 B2-1 的影响面清单里，原因是 Holmes 隔离检查的放行集合直接取代理读路径（`scripts/holmes_baseline/isolation.py:148`）。5 个失败都已在代码 PR #597 由测试作者按 B2-1 改写。
  - A3（2026-11-13，探针骨架 `9384f5e6`，草稿 PR #596，已关闭）：全量 CI 0 failed / 12158 passed / 2550 skipped；只加扩展档位，`otel-demo` 字节不变。
  - A6（2026-11-13，探针骨架 `6469127c`，草稿 PR #599，已关闭）：全量 CI 0 failed / 12226 passed / 2550 skipped；只加扩展档位，`otel-demo` 字节不变。

### N3：lab 的 Jaeger 能做什么

lab 是 chart `opentelemetry-demo 0.37.8` 带的 Jaeger 1.53.0（all-in-one、内存存储 5000 条），产品经 NodePort 匿名直连查询端口，A4 不需要改 lab。依据是读 v1.53.0 源码（`http_handler.go`、`query_parser.go`、`memory.go`）：

- 支持：标签过滤（所有条件落在同一个 span 上，按字符串精确比较）、最短与最长时长过滤（比单个 span 的时长）、按 ID 取整条（无时间参数，查不到 404）、列服务、列某服务的操作。
- 不支持：标签名发现（无此接口）；不带 service 搜索（400）。
- 服务与操作清单没有时间范围，是 Jaeger 本次启动以来见过的；`/api/*` 文档写明为不稳定的内部接口，现有 `traces_search` 已依赖它。
- 未在真实实例取样（未确认）：标签值字符串化的样子与 OTel 状态码类标签在 lab 的类型；一条 checkout 整条 trace 的 span 数与投影后 token 数；404 响应体形态；`errors` 字段是否出现；5000 条在 lab 负载下覆盖多久。

### A1：lab 只读集群身份

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A1-1 | 身份建在哪 | 新 namespace `opspilot-readonly`，SA `opspilot-investigator`，ClusterRole 与绑定 `opspilot-investigator-readonly`；清单 `scripts/kind_lab/opspilot-readonly-rbac.yaml`，`up` 末尾 apply | 身份是集群级的，不属于应用 namespace；ITBench 换 namespace 也能用 |
| A1-2 | 规则清单 | 上游模板不带条件的规则，只留 get、list，去掉自定义资源、`monitoring.coreos.com`、openshift 三块；清单见 A1 预审 | 窄读法同时满足 SPEC 与 Q6；上游本身无 secrets、exec、proxy、token |
| A1-3 | token 有效期与发放 | `kubectl create token --duration=24h`，不回显；`up` 每次重发，另加 `k8s-token` 子命令；过期时间写进 `k8s-token.json`（不放 token） | 一次租机够用；过期后报 `SOURCE_FORBIDDEN`；API server 最长期限未确认 |
| A1-4 | 文件落点 | `LAB_DIR` 下 `k8s-ca.crt`、`k8s-investigator.env`（600）、`k8s-token.json`，用 `_private_write` | 同 `prometheus-<role>.env`；`LAB_DIR` 跟检出目录走，租机上在主检出跑或软链 |
| A1-5 | CA 来源 | 从 lab kubeconfig 的 `certificate-authority-data` 解码 | 不需额外请求、不碰 Secret |
| A1-6 | worker 用的地址 | 宿主进程用 kubeconfig 的 `https://127.0.0.1:<端口>`（每次重读）；控制侧容器用 `https://{NODE_CONTAINER}:6443`；同一函数生成 | 端口每次随机；容器形态证书名未在 lab 验证 |
| A1-7 | 交给容器里的 worker | 入口脚本放行 `OPSPILOT_K8S_*`；lab 覆盖 compose 把 CA 作为 secret 只挂给 worker；`control-secrets` 重发时替换 token 与地址两行并重启 worker | 不改会被入口脚本悄悄跳过；不进基础 compose 以免 fixture 安装坏（未验证） |
| A1-8 | lab 闸门 | 不用改；A2 新加的 API 地址检查放进 `check_lab_target` | 回环地址能过 `_is_lab_host`，容器走 outbox 不查数据源 |
| A1-9 | 探测工具 | `scripts/k8s_authz_probe.py`，标准库、不走代理不跟重定向；宿主与 worker 容器各跑一遍；写类探测加 `dryRun=All` | 先授权再执行，写错也不改 lab |
| A1-10 | 证据 | `docs/evidence/a1-cluster-identity/`：`probe.json`、`rules.json`、`run.md`、清单 sha256、过期时间；token 不入库 | 格式同 F7 授权探测 |

A1-2 的规则清单（动词都是 get、list）：核心组 configmaps、daemonsets、deployments、endpoints、events、namespaces、nodes、persistentvolumes、persistentvolumeclaims、pods、pods/log、pods/status、replicasets、replicationcontrollers、resourcequotas、services、serviceaccounts；`apps` daemonsets、deployments、deployments/scale、replicasets、replicasets/scale、statefulsets；`extensions` daemonsets、deployments、deployments/scale、ingresses、replicasets、replicasets/scale、replicationcontrollers/scale；`batch` cronjobs、jobs；`autoscaling` horizontalpodautoscalers；`events.k8s.io` events；`storage.k8s.io` storageclasses；`metrics.k8s.io` pods、nodes；`networking.k8s.io` ingresses、networkpolicies；`policy` poddisruptionbudgets、podsecuritypolicies；`rbac.authorization.k8s.io` clusterroles、clusterrolebindings、roles、rolebindings；`apiregistration.k8s.io` apiservices；`apiextensions.k8s.io` customresourcedefinitions（只是定义，不是自定义资源本身）；`admissionregistration.k8s.io` mutatingwebhookconfigurations、validatingwebhookconfigurations。

A2-5 的静态资源表比 A1 清单窄；A1 与 A2 谁后合并，谁加一条测试：静态表是 A1 清单的子集。

### A3：Pod 日志

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A3-1 | 时间参数 | `start_time`、`end_time` 只收绝对 RFC3339，相对秒数拒绝；复用 `_query_window` 规则 | C3 §8 要求绝对窗，与其他工具一致 |
| A3-2 | 请求与计数 | 先取 Pod 拿容器名，每容器当前与 `previous` 各一次；1+2C 次 HTTP 记 1 个 operation | API 无 all-containers；计数同 A2-9 |
| A3-3 | 窗终点与读量 | 本地丢掉晚于窗终点的行；所有流共用 4 MiB，读满保留已读、标不完整并提示后移起点 | 部分日志不会算错，比整条拒绝有用 |
| A3-4 | 流取不到 | 单流 400 记无数据并写 notes；401/403 `SOURCE_FORBIDDEN`；429/5xx `SOURCE_UNAVAILABLE`；全空 `no_data` | 一次失败不连累另一次；CrashLoop 时上一次流最关键 |
| A3-5 | Pod 不存在 | 404 报 `INVALID_PARAMS`，固定码 `POD_NOT_FOUND` | 与 A2-12 一致 |
| A3-6 | 路径注入 | namespace 为 DNS 标签、pod 为 DNS 子域名、容器名 URL 编码；不合法不发请求 | 都拼进 API 路径 |
| A3-7 | 正则 | 窗内筛选后跑 `filter`、`exclude_filter`；独立子进程，Python `re` 不区分大小写，最长 2000 字符，编不过退化为子串；超时结果类与 A2 jq 超时相同 | 进程内 `re` 停不下来，依赖里无 re2 |
| A3-8 | 条数 | `limit` 默认 100、1–1000；`row_unit="lines"`，截了行或读满标不完整；读取数、筛掉数等进 `view_fields` | R8；沿用 traces 计数先例 |
| A3-9 | 行形态与来源时间 | 每行带时间戳、容器、实例、文本；无时间戳的行丢弃计入 notes；来源时间取保留行首尾 | 来源时间须来自数据，不能证明在框内的不留 |
| A3-10 | 超长行 | 每行截到 2000 字符并标出 | 防一行巨型 JSON 让视图整条被拒 |
| A3-11 | 登记 | verb `logs`，`source="otel-demo"`，`may_contain_secrets=True`；扩展档位 `pod_logs` 移到可用 | 与 A2 一致，`otel-demo` 不动 |

### A4：trace 扩展

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A4-1 | 工具 | `traces_search_by_tags`、`traces_query_trace_by_id`、`traces_list_services`、`traces_list_operations`；`traces_search` 不动 | 对上 Tempo 两个工具；值发现在 Jaeger 只能落到服务与操作 |
| A4-2 | 搜索参数 | service 必填，加 tags、operation、时长、limit、start、end；tags 用 logfmt 串，解析失败报 `INVALID_TAGS` | Jaeger 要求 service；`ParameterSpec` 不支持 object |
| A4-3 | service 校验 | 非空、≤200、无控制字符，不用固定枚举 | 枚举是 otel-demo 写死的；空结果提示核对服务名 |
| A4-4 | 时长 | Go 时长格式，max ≥ min；描述写明比单个 span | 语义与 Tempo 不同须让模型看到 |
| A4-5 | 条数 | 默认 20、无上限，满了标不完整；超出返回最新 N 条 | 沿用 B1 先例 |
| A4-6 | 搜索投影 | 调现有 `project_traces`，只换 query 记录；投影 id `otel-demo-ext-traces-v1` | 白名单不放宽，不动 otel-demo 字节 |
| A4-7 | 按 ID 的时间 | 省略时用整个授权框；ID 为 1–32 位十六进制；丢窗外 span 并计数 | 不丢会被 `WINDOW_OUT_OF_SCOPE` 整条拒绝 |
| A4-8 | 按 ID 的投影 | 同白名单，按起始时间排序不抽样；读 4 MiB、视图 25k 超了整条拒绝 | 「整条」指全部 span；体积未确认 |
| A4-9 | 按 ID 404 | 成功的空视图，说明 ring 里没有不证明不存在；400 报 `INVALID_PARAMS` | 404 在这里是「没数据」 |
| A4-10 | 服务与操作清单 | 无时间参数，来源时间留空；`cannot_prove` 写明只代表启动以来；操作名脱敏截 150 字符 | 清单不随 ring 淘汰 |
| A4-11 | 错误分类 | 400 `INVALID_PARAMS`（回显截短脱敏的错误）、5xx `SOURCE_UNAVAILABLE`；`errors` 非空标不完整 | 沿用现有传输 |
| A4-12 | 模块 | `opspilot/tools/jaeger_query.py`，复用 `otel_demo` 可导入函数不改它们 | 同 A2 独立模块做法 |

### A5：Prometheus 即时查询与发现

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A5-1 | 名字与参数 | `execute_prometheus_instant_query(query, time?)`、`get_series`、`get_all_labels`；不搬 description、timeout，超时固定 30 秒 | 名字照上游，超时由注册统一管 |
| A5-2 | 即时查询时刻 | 总是显式发 time，省略取查询窗终点；须在框内，回看区间不早于框起点 | 「现在」晚于授权框会被整条拒绝 |
| A5-3 | 即时查询上限 | 不发 limit，视图 25k 超了整条拒绝并给摘要与 topk 建议 | 与区间查询同口径 |
| A5-4 | 序列与标签条数 | 固定 limit=100，有 warnings 或恰好 100 条都标不完整 | v3.5 截断是否带 warning 未确认，两条都认 |
| A5-5 | 时间窗 | 沿用 `_query_window`，省略为 1 小时 | 与现有发现工具一致 |
| A5-6 | match | 复用 `_match_ok`；series 必填、labels 可选 | 现有规则 |
| A5-7 | 模块与传输 | 新模块 `prometheus_ext.py`；新工具名不进 `PROMETHEUS_TOOLS`；推荐给现有传输加「哪些工具走基本认证」构造参数 | 那个集合进 otel-demo 版本哈希 |
| A5-8 | 错误分类 | 同发现工具；即时查询另认 503 timeout 为超时 | 现有规则 |

### A6：按列查询集群对象

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| A6-1 | 取数 | 完全复用 A2（资源表、namespace、Secret 拒绝、分页、4 MiB、managedFields、错误、事件按框筛）；1 次调用 1 个 operation | 事件筛选不共用就会绕过授权框 |
| A6-2 | 列语法子集 | 字段名、`[n]`、`[*]`、可选 `{}`；缺字段 `<none>`；最多 20 列、2000 字符；禁 `'` `/` `;` 换行；失败报 `INVALID_COLUMNS` | R16，上游实际用到的部分 |
| A6-3 | 要不要 jq | 不要，Python 求值 | 子集小，省注入面 |
| A6-4 | filter_pattern | 匹配单元格拼成的行；区分大小写；跑在 A3-7 的正则子进程 | 照上游 `grep -E` |
| A6-5 | 返回与计数 | `{header, rows, count, scope, notes}`，行是单元格数组；不设条数上限，视图超限整条拒绝 | 键名照上游，模型不用自己切对齐空格 |
| A6-6 | 时间 | 同 A2-11；`kind=events` 时来源时间取保留事件首尾 | 共用 A2 规则 |

### B2：告警规则、抓取目标与指标元数据

用户裁决（2026-11-12）：

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B2-1 | lab 只读代理放行 `/api/v1/rules`、`/api/v1/metadata`、`/api/v1/targets` 三个 GET 时对谁放开；前两条现按 F14 的 U2「两边都没有，权限对齐」有意拒绝并钉在测试里 | a. 对所有账号放行（含 Holmes 基线账号）：两边再次对齐；代价是 Holmes 基线以后的 Run 比已公布的 F14 结果多两个能用的工具，旧结果按日期保留。b. 只对 investigator 与 lab 放行：已公布的对比条件不变；代价是 OpsPilot 比 Holmes 多权限，违反 U2 的理由 | a | 改写钉 403 的测试：`tests/acceptance/test_396_proxy_contract.py:67-72`（验收测试，按意图须改）、`tests/test_kind_lab_readonly_proxy.py:210,446`、`tests/test_f7_authz_probe.py:277`、`scripts/f7_authz_probe.py:57-60`；拒绝样例换成 `/api/v1/status/config` 这类仍拒路径 | a（用户 2026-11-12） |

可逆默认：

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| B2-2 | 规则查询参数 | type、rule_name、rule_group、file、match 原样转发；不搬 jq、max_depth；type 只认 alert、record | jq 子进程要等 A2；lab 规则很少，视图 25k 兜底 |
| B2-3 | 规则里的活跃告警 | 照返回，来源时间留空，`cannot_prove` 写只代表查询那一刻 | 同 Q4 |
| B2-4 | 指标元数据 | 固定 limit=100，满 100 标不完整；metric 名格式校验；无时间窗 | 照上游；接口标 experimental |
| B2-5 | 抓取目标 | 打 `targets?state=active`，按 `labels.job` 过滤，去掉 `discoveredLabels` | `scrapePool` 不一定等于 job；原始标签体积大 |
| B2-6 | 模块与凭据 | 进 `prometheus_ext.py`，凭据与错误分类同 A5；代码可先于 lab 改动合并 | 少一份传输代码 |

## r4：ITBench-Lite 适配开工前预审

范围与接口见 [ITBench-Lite 适配任务记录](2026-11-12-itbench-lite-adapter.md)。预审全文在仓库外。

用户 2026-11-12 对齐（均按推荐）：报告由评测侧确定性转换成答案格式，产品报告格式不改；官方 LLM judge 分与确定性实体匹配分都报；35 题全留分母，按数据覆盖分档并标出 S38、S18；第一轮只跑 OpsPilot，标为单边结果。

| 编号 | 问题 | 默认 | 类别 |
|---|---|---|---|
| L1 | 根因实体怎么抽 | 快照对象清单加服务名当词表，在 claim 与 summary 里按词边界匹配；Kind 取被引证据的工具参数，其次取 Service；规则写死并配单元测试 | 可逆默认 |
| L2 | 是否根因与排序 | summary 里第一个提到的实体算根因，其余为受影响；`rejected_hypothesis` 提到的标 false；结论为 inconclusive 时不标根因；结果注明「启发式」 | 可逆默认 |
| L3 | 传播链 | 只抽 summary 里显式的箭头写法，抽不到留空；官方传播链分标「受转换限制」 | 可逆默认 |
| L4 | reasoning 与 evidence 字段 | 用提到该实体的 claim 原文拼接，evidence 拼工具名与参数，截到 2000 字符 | 可逆默认 |
| L5 | 没有报告的 Run | 写合法的空数组，计 0 分，留在分母 | 可逆默认 |
| L6 | 官方 judge 的模型与接收方 | a. 仓库已有接收方（Z.ai、OpenAI、DeepSeek），写明与官方示例不同；b. 照官方示例经 OpenRouter 用 `gemini-2.5-pro`，新增数据接收方 | 用户 2026-11-13 裁决 a |
| L7 | 官方评测器怎么接 | 钉 `ITBench-Evaluations@14f026fc`，在仓库外的 venv 跑，不往仓库加依赖；主分取根因实体的 F1 与 pass@1 | 可逆默认 |
| L8 | 确定性实体匹配 | 自己实现：答案的 filter 正则、kind、aliases；单元测试用合成答案；namespace 排除名单同官方 | 可逆默认 |
| L9 | 答案隔离 | 加载器白名单只读指标、告警与四个 TSV；答案只在判分进程读；数据目录里有答案文件时拒绝启动 | 可逆默认 |
| L10 | 什么入库 | 每题分数、覆盖档位、`summary.json`、trace 链接；`agent_output.json` 与匹配明细放 git 忽略目录；issue 与 PR 不写答案 | 可逆默认 |
| L11 | 覆盖分档依据 | 只用两次普查（基于告警的代理起点），不用来自答案的列；「开关类」标注在判分后才贴 | 可逆默认 |
| L12 | 档位与提示词 | 用 `otel-demo-ext`；结果记 `prompt_revision` 与 `tool_schema_revision`；A2 前可用 `otel-demo` 冒烟 | 可逆默认 |
| L13 | 多容器 Pod 的日志 | 日志挂在与 Deployment 同名的容器上，没有同名挂第一个；其他容器返回空 | 可逆默认 |
| L14 | 假 server 的证书 | 启动时用 `openssl` 生成自签证书，不加 Python 依赖 | 可逆默认 |
| L15 | 每题几次、怎么重置 | 3 次；每次重新灌数据并重算平移量，用全新 PG；按题串行 | 可逆默认 |
| L16 | 回放 Prometheus 的认证 | 不校验，账号文件用占位值；驱动从参数读账号文件 | 可逆默认 |

探针遗留四点的默认：假 API server 算评测设施，放 `scripts/itbench_lite/`，不进产品镜像；回放服务全绑回环地址，lab 闸门（A2-13）不改；出现过的资源类没有对象时返回空列表，从没出现过的返回 503（落进 A2-12 的来源不可用）；`previous=true` 返回 400。告警入口：评测侧把 ITBench 告警转成 webhook v4，每题投一条入口告警，目标条目按题配置，不改产品代码。

## r6（2026-11-13）：#604/#605 开工前预审

对照 `main` `bc7ff9d0`，预审全文在仓库外。#604 是逐题回放与评测编排驱动，#605 是分档文件与词表生成器。L-D 全部按推荐执行，另有 lead 三条裁决（标「lead」）；L-V 除 L-V7 外按推荐执行。

### #604 驱动

| 编号 | 决定 | 类别 |
|---|---|---|
| L-D1 | web 与 worker 用宿主子进程（`opspilot.web serve`、`opspilot.worker_main`），告警走真实 HTTP 入口 | 默认 |
| L-D2 | 每次尝试用 `eval_live_lab.LocalPgLauncher` 建全新 PG，数据目录在 `tmp/itbench-lite/<轮次>/…/pg`；summary 冻结后按 pid 文件停库，目录留到整轮结束；驱动用 owner DSN 开只读事务读 | 默认（L15） |
| L-D3 | 固定可配端口，全部 127.0.0.1：PG 29110、Prometheus 29190、Jaeger 查询 29186、Jaeger OTLP/HTTP 29118、假 server 29143、web 29180；每次尝试前逐个查空闲，被占记基础设施失败、不换端口；web 与 worker 的健康监听设空串关闭 | 默认 |
| L-D4 | docker 起 `prom/prometheus:v3.5.0`（promtool 用同一镜像）与 `jaegertracing/all-in-one:1.53.0`，只发布 `127.0.0.1:端口`；Prometheus 不写抓取任务与规则；容器名 `itbl-<尝试id>-{prom,jaeger}`，按完整名字删除；summary 记镜像 digest | 默认 |
| L-D5 | 假 server 用子进程跑 CLI，读 stdout 那一行 JSON；每次尝试新 token（0600 文件）与证书目录；就绪判据是带 token 的 GET 返回 200 | 默认 |
| L-D6 | 快照每题按白名单名字复制一次到干净目录（跟随符号链接复制内容），3 次尝试共用只读副本，每次尝试前重新 `load_snapshot` 校验。偏离 #604 正文「每次尝试复制」的字面，为省读写，数据不变 | lead |
| L-D7 | 按 r4 原文：灌完立刻投，灌完到投递间隔超过 2 分钟作废；POST 前检查 `delivery_gap_ok` 与 `check_not_future`，不过就作废、不投递，POST 后用响应时刻再核一次。平移量在灌入前以尝试开始时刻为锚，数据末尾早于开始约 1 分钟；`--load-budget-seconds` 只作灌入耗时上限，超过记基础设施失败。时钟用主机 UTC 墙钟。lead 2026-11-13 按 r4 原文改正（原推荐「等到计划时刻再投」） | lead |
| L-D8 | 每个 trial 最多作废或因临时基础设施问题失败 2 次（共 3 次机会），超过记缺失、计 0、留在分母（L5）；确定性错误（`NoEntryAlert`、`SnapshotRowError`、`AnswerFilePresent`、入口告警缺匹配标签）不重试，3 个 trial 都记缺失并写原因 | 默认 |
| L-D9 | 投递后没建 Run（`TARGET_UNRESOLVED`/`TARGET_AMBIGUOUS`）是配置错误，停整轮；Run 结束但没有报告照 L5 计 0、不送 judge；报告校验不过按没有报告处理并记 `REPORT_INVALID_FOR_CONVERSION`；judge 失败只重跑判分，最多 2 次；驱动中途崩溃记 `interrupted`，算基础设施失败 | 默认（L5） |
| L-D10 | 模型与上限沿用产品默认，不设 `OPSPILOT_RUN_SECONDS`；兜底等待 7200 s + 10 分钟；`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=0`；`OPSPILOT_TRACE=lab`，项目 `opspilot-lab-itbench-lite-<日期>`，第一次 Run 前设长期保留；记录 `prompt_revision` 与 `tool_schema_revision`（L12） | 默认 |
| L-D11 | 目标条目键 `m0-otel-20260909`，`namespace=otel-demo`，`workload` = 入口告警的 `service_name`，`match.labels` = `default_match_labels`；`cluster_uid` 取快照里 kube-system 的 uid，没有用 `itbench-lite`。入口告警没有 `service_name` 时回退：match 用 `alertname`，workload 取 `service`、`pod`、`namespace` 中第一个存在的标签，并在产物里标记该题用了回退 | 默认 + lead（回退与标记） |
| L-D12 | 报告取结论步骤的 `report_content`；证据用 `investigation_evidence(...).observations` 映射成 `{"evidence_id","tool","query"}` | 默认 |
| L-D13 | summary 用 `lab_evidence.freeze`，内容照 F3：报告、投影、审计、版本、token、trace 链接，另加「尝试」块；不写任何来自答案的内容。「冻结一个给定 Run」写成可复用模块放 `scripts/` 下，接口是给 run id → 冻结 summary.json 与回读 trace 链接，A7 驱动复用 | lead |
| L-D14 | 每次尝试结束后起判分子进程，只传答案根目录与这次尝试的输出路径；子进程调 `official_eval.run`（每次尝试单独 work dir）与 `entity_match.score`，分数写进忽略目录，驱动只读回数值；默认每次都调 judge，`--no-judge` 推迟；另有汇总命令生成 `coverage.py` 的分数文件 | 默认（L9） |
| L-D15 | 官方 pass@1 = 各 trial `root_cause_entity_f1 == 1` 的均值；确定性分用 `pass`；确定性分的键加前缀 `det_` | 默认 |
| L-D16 | 入库：`docs/evidence/itbench-lite-sre-<日期>/` 下每次尝试的 `S<id>/trial-<n>/summary.json`、分数表、分档文件与明细、限制说明；其余（干净副本、PG、TSDB、日志、原始 ledger、词表、答案输出、匹配明细、judge 工作目录）在 `tmp/itbench-lite/<轮次>/` | 默认（L10） |
| L-D17 | 默认串行，端口、PG、LangSmith 项目与 lab 全部错开、健康监听关闭，使与 kind 阶段并行成为可能；每次尝试前记一次内存读数 | 默认 |
| L-D18 | summary 记 git 提交；正式轮次工作区不干净就拒绝启动，冒烟可加 `--allow-dirty` 并写进 summary | 默认 |
| L-D19 | 场景按编号排序，每题连续跑 3 个 trial；已有完整 summary 的 trial 跳过，可续跑 | 默认（L15） |
| L-D20 | 只对驱动自己启动的进程经 `Popen` 句柄先 SIGTERM、宽限后 SIGKILL 同一 PID；容器按完整名字 `docker rm -f`；逆序清理，任一步失败也清理到底再报错，清理后核对端口已释放 | 默认 |

### #605 生成器

| 编号 | 决定 | 类别 |
|---|---|---|
| L-V1 | 分档数据用 `load_snapshot` 从快照重新算；可选 `--census` 交叉核对，只许读不来自答案的列，禁止读 `mechanism` | 默认（L11） |
| L-V2 | 故障起点取 `alerts.select_entry_alert(final_alert_file)` 的 `activeAt`（未平移） | 默认 |
| L-V3 | 每类「结束时刻 − 故障起点」≥ 5 分钟为覆盖，0–5 分钟为部分，< 0 为不覆盖，五类各判一次 | 默认 |
| L-V4 | 两档：「指标+trace」与「故障窗无 trace」；每类覆盖另写进明细 | 默认（可逆） |
| L-V5 | 输出 `{"<id>": "<档位>"}`（正好 35 个 ID）与 `tiers_detail.json`，都入库 | 默认 |
| L-V6 | 词表收全量里出现过的 `metadata.name`（`answer.RESOURCE_KINDS` 内的资源类，去掉 secrets 与官方排除 namespace 的对象），加三处服务名并集 | 默认 |
| L-V7 | namespace 名与通用词（`kubernetes`、`default`）进不进词表：推荐去掉 Namespace 对象名并用固定停用表，规则在看答案前写死 | **用户裁决（2026-11-13）：按推荐**，去掉 Namespace 对象名并用固定停用表；2026-11-15 澄清：namespace 名按名字字符串排除，不论对象类型（#647 落实：Namespace 对象名 ∪ 各对象的 `metadata.namespace` 值，见 [#646 任务记录](2026-11-15-itbench-lite-root-cause.md)） |
| L-V8 | 每行一个名字，不区分大小写去重并排序；每题生成一次，放忽略目录 | 默认 |
| L-V9 | CLI：`python -m scripts.itbench_lite.vocabulary <干净副本> --out <文件>`；`python -m scripts.itbench_lite.tiers <副本根目录> --out tiers.json --detail tiers_detail.json` | 默认 |
| L-V10 | 流式读，不做缓存 | 默认 |

## r7（2026-11-13）：A7（#559）开工前预审

对照 `main` 的预审全文在仓库外。A7-8、A7-9、A7-12 由用户裁决（2026-11-13，均按推荐 a），其余为可逆默认。租机安排同日裁决：本机部分做完后一次跑完全部真实 Run（约 20–26 小时）。

| 编号 | 问题 | 选项（★推荐） | 裁决 |
|---|---|---|---|
| A7-1 | 指导放在哪一层 | ★a. 新的 L1a（纪律层）变体，只有扩展档位使用。依据：C3 第 5 节 L1a 含「调查方法与影响面纪律」，R12 写的是「只动扩展档位的提示词版本」。b. 放进 L3 的 `DATA_SOURCES_INSTRUCTION` 或工具描述，会改扩展档位的 `tool_schema_revision`，与 R12 的措辞不符 | 默认：a |
| A7-2 | 新变体的结构 | ★变体名 `replay-candidate-k8s`，段落为 `replay-candidate` 的全部段落，再在影响面段（`_IMPACT`）和报告契约槽位（`_REPORT_SLOT`）之间加一段 `kubernetes_guidance`（位置与 #310 一致）。`replay-candidate` 本身一个字节不改 | 默认 |
| A7-3 | 新段落标不标「工具专有」 | ★不标（`tool_specific=False`）。正文不出现工具名、参数名或投影字段名，只讲集群调查方法；`tests/test_instruction_discipline.py:463` 的合同测试因此不用改 | 默认 |
| A7-4 | 哪些条款进来 | ★上游 :73-78 六条按第 2 节改写，加上 #310 当时去掉的三个 K8s 片段（:56 读不到 Secret 就说无法核实、:58 检查应用日志、:69 原因词清单），约 8–9 句。去掉 kubectl/describe，不点工具名。不收 :24 命名空间受限段（N4）、:88 权限错误段（贴上游链接）、:9 多集群段、TodoWrite 段（属 C1 #565） | 默认（R12 本身就是可逆默认，扩充方式按「照搬上游」方向）；在简报里列出句子清单 |
| A7-5 | 改不改 `otel-demo` 档位 | 不改。补钉值：`otel-demo` 的 `prompt_revision == "prompt-replay-candidate-bef70649a3a8"`、`discipline_revision("replay-candidate") == "l1a-replay-candidate-9a45deb526fe"`、`otel-demo` 工具面的 `variant_id == "replay-candidate"`，再钉 `replay-candidate` 用现行 `REPORT_CONTRACT` 渲染结果的 sha256。现有「字节不变」测试只钉 `tool_schema_revision`，这是缺口 | 默认 |
| A7-6 | 提示词 revision 怎么变 | 只按内容哈希自动变化，不手写版本号。扩展档位得到 `prompt-replay-candidate-k8s-<哈希>` 和新的 `discipline_revision`；`tool_schema_revision` 不因 A7 变化。代码改两处：`opspilot/tools/otel_demo_ext.py:160` 改为 `investigation_versions(新变体)`，`:175` 的工具面 `variant_id` 换成新变体。在途扩展档位 Run 被重新领取时进入 `blocked(INCOMPATIBLE_STATE)`，按 C3 照常，目前没有部署中的扩展档位 Run | 默认 |
| A7-7 | #310 的通用条款是否带入 | ★不再带入：已在 `replay-candidate` 里（`discipline.py:85-113`、`:210-217`，2026-10-22 裁决，提交 `6e1c1bae`），新变体继承。#310 仍是 OPEN，由 lead 判断是否关闭 | 默认 |
| A7-8 | 「四类证据都被引用」怎么判定 | ★a. 取已发布、已通过校验的报告里每条结论的 `evidence_ids`，经 summary 的 `tool_requests` 找到工具名，再按代码常量分类：指标 = Prometheus 工具（`otel_demo.PROMETHEUS_TOOLS` ∪ `prometheus_ext` 工具，含 B2）；trace = `traces_search` ∪ `jaeger_query.TOOL_NAMES`；集群对象 = `kubernetes_jq_query`/`kubernetes_count`/`kubernetes_tabular_query`；日志 = `fetch_pod_logs`。只算 `status` 为 ok 的证据；从证据上下文继承来的不算；`read_evidence` 切片归到它来源工具的类别。任何结论类别（fact、hypothesis、counter_evidence 等）都算，另记录各类别的分布。b. 每类至少支撑一条 fact（更严）。c. 调用过就算，不要求引用（比 issue 的写法松，不取） | **用户裁决（2026-11-13）：a** |
| A7-9 | Run 用什么故障场景 | ★a. 现有的 flagd `paymentFailure` 100%（`scripts/kind_lab.py fault inject`，`kind_lab.py:154,2452`），触发 lab 里真实的 Prometheus 规则 `CheckoutPlaceOrderErrorRatioHigh`，经 Alertmanager 投递到工作台。F2/F3 都用过，不是按 benchmark 答案挑的。按 Q5 标为「开关类场景」。b. 新做一个 K8s 原生故障（OOM/limit、缩到 0、坏镜像），要新的注入器和告警规则，有量身定做的风险，工作量也更大。c. 换 `cartFailure`/`productCatalogFailure`，能否触发唯一那条 checkout 告警规则未确认 | **用户裁决（2026-11-13）：a** |
| A7-10 | 「全程无人工」的确定性判定 | 事故由 `alertmanager` 身份开出；Run 由告警接收自动排队；审计里没有追问、暂停、接管或人工 close（在冻结 summary 之前）；worker 在告警到达前已在运行（不像 F3 的 D1-a 那样由驱动放行） | 默认 |
| A7-11 | 2 次 Run 的前后对照口径 | 前一次用 A7 起点提交的扩展档位（`replay-candidate`），后一次用 A7 提交；同一 lab 会话、同一场景，中间恢复故障并冷却。确定性对照：引用了哪些类别、各类别调用次数、模型请求数、token、校验是否通过、工具失败次数。每边只有 1 次 Run，不下因果结论，不用 LLM judge。完成标志只要求后一次达到 | 默认 |
| A7-12 | 后一次没达到四类时怎么办 | ★a. 同一份代码最多补跑 2 次，全部照实记录（x/n），有一次达到即满足完成标志。b. 只跑一次，失败就保持 issue 开着，改提示词另开 PR。c. 现场改提示词直到通过：这就是量身定做，不取 | **用户裁决（2026-11-13）：a** |
| A7-13 | 驱动与证据收集 | 现有驱动都不合适：F3 驱动的公开接口已冻结，并且内置了发布和人工追问（`scripts/f3_competing_scenario.py:10-40`）；eval_live 由 harness 自己投递告警（`AlertIntake`），不是真实的 Alertmanager。默认新写一个薄的工程脚本：`kind_lab.py fault inject/restore`，用 `scripts/lab_evidence.py` 冻结 summary 和回读 trace，再加一个纯函数 `cited_categories(summary)` 并配单元测试（用合成的 summary 做输入） | 默认 |
| A7-14 | 与 A6、B2、ITBench-Lite 的先后 | 真实 Run 放在 A6（PR #600）和 B2（PR #597）合并之后，这样扩展档位的工具面是阶段 A 的最终面；summary 记录三个版本号。Lite 35 题（#571）放在 A7 合并之后，因为它记录的 `prompt_revision`（L12）要用最终值 | 默认 |
| A7-15 | 上游许可登记 | `docs/portfolio/upstream-attribution.md` 加上「Kubernetes 段改写自 `generic_ask.jinja2` :56/:58/:69/:73-78」，`third_party/holmesgpt/PROVENANCE` 的覆盖清单同步 | 默认 |
| A7-16 | 上限 | 用产品默认：每 Run 100 次模型请求的防循环上限、20 次工具操作、240 秒（`executor.py:96-97`），不为 A7 调整 | 默认（改上限属合同变更，不在本项） |
| A7-17 | trace 项目 | LangSmith 项目 `opspilot-lab-a7-main-path-<日期>`，开跑前设为长期保留 | 默认 |

附：A7-4 推荐的段落草稿（英文，供实现者参考，句子可调）

- K1（:73）When investigating Kubernetes workloads, walk the ownership chain, such as deployment to replica set to pod, and check both the objects' status and the application logs.
- K2（:74）When a pod crashed or an application reports errors, read that pod's status, its containers' last termination reason and exit code, and its events, and read its logs, including the previous container's logs after a restart.
- K3（:75）Do not stop at a surface state such as pending or not ready: say why, naming the resource that is short or the label or setting that does not match, and give the fix as a recommendation.
- K4（:76）When many pods are affected, checking up to three representative pods per workload is enough to conclude about the workload; report the finding against the workload and name an individual pod only when you looked at that pod.
- K5（:77，压缩）Eviction and OOM leave marks on the pod: an Evicted event from the kubelet, or OOMKilled as a container's last termination reason. A pod deleted and recreated by its own controller, shown by that controller's SuccessfulDelete and SuccessfulCreate events, a new pod name and a restart count of zero, was rolled out or rescheduled, not damaged; for a pod younger than the incident, check who created it and whether the same controller replaced its siblings at the same time before writing about it. A startup readiness-probe miss on such a pod is part of starting, not impact.
- K6（:78）When something is not working, inspect the owner workload and its pods for transient problems, check the related services and ingresses for misconfiguration, and read the application logs for runtime errors.
- K7（:69）Place a pod under affected only when you can name its own reason, such as Evicted, OOMKilled, BackOff, Failed, Unhealthy on a long-running pod, or an error log line; SuccessfulCreate, SuccessfulDelete, Scheduled, Started and a probe miss at startup are not impact on their own.
- K8（:56）Secrets cannot be read; when a conclusion depends on a value you could not read, say you could not verify it rather than guessing.
- K9（:58）A running or ready pod can still be failing; read its application logs before calling it healthy.

## r8（2026-11-16）：B/C 批次与评测

用户 2026-11-16 逐条裁决（grilling 两轮；预审材料在仓库外，见 [B/C 批次记录](2026-11-16-tool-expansion-bc.md)）。r1–r7 的 Q1–Q8、D1–D3、N4 继续有效，本节只修改 Q6、Q7/N4 两处（见 R8-18、R8-19）。

| 编号 | 问题 | 裁决 |
|---|---|---|
| R8-0 | 验收题从哪来、在哪跑 | 用现成公开基准，不自己出计分题；直接在租用服务器上搭题目环境（需要 K8s 时起 kind，照上游 CI 版本），不复用 otel-demo kind lab |
| R8-1 | 结果怎么对外说 | 两轨、分数不合并：中立第三方（ITBench-Lite、SREGym）与竞品自评集（「HolmesGPT 公开评测集」，写明为对方自出题）；跑题发现的评测缺陷单独记录，对外提 issue 另需授权 |
| R8-2 | 题目入口 | 题面原文经网页人工提问，不编告警 |
| R8-3 | 判分 | 上游 judge 提示词原样，judge 用已有接收方，二值，注明 judge 与上游不同 |
| R8-4 | Holmes 对照 | 同题同模型，只在最后一次集中跑时做 |
| R8-5 | 遍数 | 每题 3 遍，摇摆的题单独补跑 |
| R8-6 | 防照答案调 | 写工具与提示词的 Agent 不读预期结论；跑完后 lead 在隔离上下文分析；只靠规则约束 |
| R8-7 | 部署权限 | 题目环境由评测脚本用工程师凭据部署；产品只持只读身份 |
| R8-8 | 数据流 | 租机上由公开基准夹具搭的环境按合成 lab 处理，新数据源在任务记录登记；不新增接收方 |
| R8-9 | 工具完成标准 | 代码与合同测试通过、独立审查；真实 Run 调用了工具且报告引用其证据；通过率照实报告，无分数门槛 |
| R8-10 | 档位 | 全部进扩展档位，`otel-demo` 字节不变 |
| R8-11 | 租机 | 与 #646 后的提示词改动、F3 重跑（#664）合并一次租机；提示词改动很急时先单独租 |
| R8-12 | B1 后端 | 正式评测 Elasticsearch 8，工具只用两家共有读接口（搜索、映射、索引列表）；OpenSearch 冒烟 2 题 |
| R8-13 | SuggestSkills 硬断言（6 题） | 去掉该断言只判主回答并披露；Holmes 对照同口径 |
| R8-14 | C2 知识入库 | 走现有审核流程，来源标「HolmesGPT 评测夹具」 |
| R8-15 | Holmes 工具面 | 官方原样配置，结果页一行说明其工具更多 |
| R8-16 | C6 | 做；评测时给题目数据库加只读账号 |
| R8-17 | 中立第三方现场题 | SREGym Lite 17 题，只跑诊断段，用其自带判分、judge 指向已有接收方；OpsPilot 只用自己的只读工具 |
| R8-18 | B4 Helm（改 Q7/N4） | 做。扩展档位、合成环境内可读 `type=helm.sh/release.v1` 的 Secret；在工具内解码，只返回发布历史元数据（revision、状态、chart、版本、时间、说明），values 与 manifest 不出工具；RBAC 不能按类型授权，故身份加 `secrets` 的 `list`（无 `get`、`watch`），网关只放行带 `fieldSelector=type=helm.sh/release.v1` 的 `list`，其余 Secret 请求照旧拒绝且不发请求；残余风险（身份可列全部 Secret，只靠网关）在 B4 记录与宣称里写明 |
| R8-19 | C4 ArgoCD（改 Q6） | 做。扩展档位只读 `argoproj.io` 的 Application 类资源；其他自定义资源照旧不授权 |
| R8-20 | C3 Kafka | 做，自建功能验收（不计分），SREGym Kafka 题照常计分；lab Kafka 不可用时在租机上自装 |
| R8-21 | B3 | 做，SREGym 相关题加直接取样验收 |
| R8-22 | 顺序 | B1 → C5 → B3 → C2 → C1 → C6 → C3 → B4 → C4；一工具一 PR，全部合并后集中租机（#669） |
| R8-23 | ITBench-AA | 这一批不跑；下次跑 ITBench-Lite 时核实判分并补多出的 5 题 |
| R8-24 | 无公开题的项 | 自建场景排队尾，不推迟，issue 保持开启 |
| R8-25 | 合并前的真实后端集成测试（补 R8-9） | 每个数据源工具合并前，起真实后端容器（Elasticsearch 8、OpenSearch、Loki、VictoriaLogs、Postgres/MySQL、Kafka 等），写入合成数据后用工具真实查询，断言返回、截断、错误分类与只读拒绝；不调模型、不跑题；本机内存不够时放 CI 服务容器 |
| R8-26 | 提前短租冒烟 | B1 与 #666 合并后租一次短机（约 3–4 h，估计）：HolmesGPT 评测集 2–3 道 ES 题、SREGym Lite 1–2 题各 1 遍，真实跑通环境搭建、提问入口、报告转文本、judge 判分；问题回本机修（#671）；与 #646 后提示词改动或 F3 重跑时间重合则合并租机 |
| R8-27 | 集中租机的检查点 | #669 先对每个工具冒烟 1–2 题，确认工具被调用、报告引用其证据、判分正常，过检查点再开全量；冒烟中的问题按 #616 的分阶段规则处理 |

Agent 自定（可逆）：去掉每 Run 工具调用次数旧上限（#500）；核实 worker 并行 Run；题目分诊、端口冲突分批、相对时间题每轮重新部署；通用 target 登记方式；报告转文本规则。

## r9（2026-11-16）：B1（#560）开工前预审

预审者未参与实现，对照 `main` `2f7ec758`、r8 与上游 `a0009195`，全文在仓库外（`~/.agent/handoffs/opspilot-b1-prereview-2026-11-16.md`）。B1-U1、B1-U2 由用户裁决（2026-11-16），其余为可逆默认。全量验收探针未跑（本机 swap 紧），开工后由草稿 PR 交 CI。

| 编号 | 问题 | 裁决 |
|---|---|---|
| B1-U1 | 文档没有时间字段时怎么办 | **用户裁决（修订版）**：时间过滤由工具强制，模型不能关闭。工具先看索引映射：有时间字段（默认 `@timestamp`）的索引一律按事故时间窗过滤；只有映射里根本没有时间字段的索引不加过滤，来源时间留空，`cannot_prove` 写明不能证明属于事故时间窗（Q4 例外扩到「无时间字段的索引」）。C3 §8 随 B1 PR 加一句。预审原推荐的「模型可传 `time_field="none"`」不采用 |
| B1-U2 | 没有 ES 的环境是否显示 ES 工具 | **用户裁决：a**。扩展档位工具面按已配置后端决定：设了 `OPSPILOT_ES_URL` 才加入三个工具、`data_sources.available` 才列 `elasticsearch`、`tool_schema_revision` 才变化；未设时字节不变。B/C 其余 8 个工具沿用同一规则 |

可逆默认（已按 B1-U1 修订版改写）：

| 编号 | 问题 | 默认 | 依据 |
|---|---|---|---|
| B1-1 | 工具清单 | 照搬上游 `elasticsearch/data` 的三个工具，名字不改：`elasticsearch_search`、`elasticsearch_mappings`、`elasticsearch_list_indices`。`elasticsearch/cluster` 的 5 个工具（cat、health、index_stats、allocation_explain、nodes_stats）不搬 | R8-12；上游 `elasticsearch.py:888-930`【核实】；评测题单已经排除了需要 cluster 组的 4 题 |
| B1-2 | 参数 | search：`index`（必填）、`query`、`aggregations`、`sort`、`source`（都是 JSON 字符串）、`size`（默认 100，范围 0–500）、`from_offset`（from+size ≤ 10000）、`profile`，另加 `start_time`/`end_time`（只收 RFC3339，规则同 A3-1）和 `time_field`（选映射里存在的时间字段，默认 `@timestamp`；不能用来关闭过滤，见 B1-U1）。list_indices：`pattern`、`sort`、`columns`（白名单）、`health`/`bytes`/`expand_wildcards`（枚举）、`pri`、`jq`。mappings：`index`、`jq` | `ParameterSpec` 只支持 string/integer/number/boolean（`registry.py:264`）【核实】，与 A4-2 用 logfmt 字符串的先例一致；`jq` 复用 A2-8 的子进程（`kubernetes_core.py:884`） |
| B1-3 | 只读保证 | 方法和路径都由工具写死，模型只能提供经过校验的索引名：`GET /_cat/indices/{p}?format=json&h=&s=…`、`GET /{index}/_mapping`、`POST /{index}/_search`（不带 query string）。索引名按逗号拆开，每段只允许 `[a-z0-9._*-]`，可以有一个前导 `-`（表示排除）；`/`、`?`、`#`、`:`（跨集群）、`..`、空格以及前导 `_`（`_all` 除外）都在发请求前拒绝；拼进路径前做 URL 编码。不跟重定向、不走代理（照 `kubernetes_opener`，`kubernetes_core.py:443`）。`_search` 是产品代码里的第一个 POST【核实：`opspilot/` 下没有别的 POST】，用合同测试钉住「只有这一条路径可以 POST」 | C3 §8「只读由工具保证」；身份是第二道防线（B1-5） |
| B1-4 | 认证与凭据 | 凭据槽 `es-ro`，与 `otel-demo-ro`、`k8s-ro` 分开，不互相回退。环境变量：`OPSPILOT_ES_URL`、`OPSPILOT_ES_API_KEY`，或 `OPSPILOT_ES_USERNAME` 加 `OPSPILOT_ES_PASSWORD`，HTTPS 时用 `OPSPILOT_ES_CA_FILE`。凭据校验只在设了 `OPSPILOT_ES_URL` 时进行：此时 ApiKey 和 basic 只能二选一，两个都设或都不设时 worker 启动报 `ES_CONFIG_INVALID`/`ES_CONFIG_MISSING`；没设 `OPSPILOT_ES_URL` 时不校验 ES 凭据，扩展档位照常启动、不含 ES 工具（B1-U2）；只设了 `OPSPILOT_ES_*` 凭据而没有 URL 时启动报 `ES_CONFIG_INVALID`，避免凭据被静默忽略。只在 worker 读凭据；入口脚本放行 `OPSPILOT_ES_*`（`docker-entrypoint.sh:55-63`）；trace 侧已按后缀 `_API_KEY`/`_PASSWORD` 擦除值（`tracing.py:143,306`）【核实】 | R3；C3 §12 凭据分离 |
| B1-5 | 只读身份 | 由评测脚本用工程师凭据创建，产品不持有写权限。ES 用 API key，权限是 `cluster:[monitor]`，索引 `*` 给 `read`、`view_index_metadata`、`monitor`；OpenSearch 用内置角色 `readall_and_monitor` 的内部用户（basic）。API 实际需要哪些权限【推断，按文档】，集成测试里实测 | R8-7；`_cat/indices` 需要 monitor |
| B1-6 | 查询 DSL 的限制 | `query`、`aggregations`、`sort`、`source` 都是 JSON 字符串，各自最长 8000 字符，解析失败报 `INVALID_PARAMS`。递归检查键名，出现 `script`、`script_score`、`script_fields`、`scripted_metric`、`runtime_mappings`、`wrapper`、`global`（这个聚合会绕过时间过滤）、`pit`、`scroll`、`stored_scripts` 之一就在发请求前拒绝（`INVALID_PARAMS`，notes 里写固定码 `QUERY_CONSTRUCT_NOT_ALLOWED`），不新增结果类。请求体固定加 `"timeout": "<本次调用剩余秒数>s"` 和 `track_total_hits: true` | C3 §8 不开放代码执行（painless 是代码）；比上游更紧（上游不做检查） |
| B1-7 | 时间 | 查询窗照 `_query_window`（省略时取框末尾的 1 小时，告警 Run 用它自带的默认窗）。来源时间不取参数，从数据里取：工具自动加两个隐藏聚合，对 `time_field` 取 min/max，然后从视图里去掉。没有给 sort 时默认按 `time_field` 降序，截断时保留最新的。list_indices 和 mappings 是当前状态，来源时间留空，`cannot_prove` 写「只代表查询时刻」 | Q4；A2-11；`TransportResponse` 的约定（`executor.py:404-416`） |
| B1-8 | 返回投影 | search 返回 `{hits:[{_index,_id,_source|fields,sort}], total:{value,relation}, aggregations, took, timed_out, shards:{total,failed}}`，单个字符串值截到 2000 字符并标出。原始读取上限 2 MiB（注册上限 `registry.py:79`），视图上限 25k token，超限整条报 `RESULT_TOO_LARGE`，并提示用 `source` 选列或减小 size。`total > 命中条数`、`timed_out`、`shards.failed > 0` 都标不完整并写原因。`row_unit`：search 为 `documents`，list 为 `indices` | A3-8、A3-10；A4-8 |
| B1-9 | 脱敏与登记 | 三个工具都设 `may_contain_secrets=True`，`source="otel-demo"`，挂在现有 target 下，地址放 selector `elasticsearch_endpoint`。search 的 verb 是 `query`，另外两个是 `get`。返回走执行器统一的 `redact_payload`（`executor.py:42`），裸密钥照 Q3 作为残余风险 | A2-1、A2-14 |
| B1-10 | 证据与 LangSmith | 证据照常进 PG。lab 闸门（`check_lab_target`，`tracing.py:183`）加一项：设了 `OPSPILOT_ES_URL` 就必须是 lab 主机，不是就报 `ELASTICSEARCH_NOT_LAB`。租机上 ES 跑在 docker 里、绑 127.0.0.1，能过闸门。在 B/C 任务记录登记这个数据源：返回什么，发往模型、PG、LangSmith | A2-13；C3 §12；R8-8 |
| B1-11 | ES 与 OpenSearch 的差异 | 只用两家都有的三个接口，不读 `version.distribution` 来分支；`hits.total` 两家都是对象（OpenSearch 2.x）【推断】；`fields` 参数两家都支持（ES 7.10 引入）【推断】；OpenSearch 没有 ApiKey，只用 basic【核实：上游配置里只有 basic/ApiKey/mTLS 三种】 | R8-12 |
| B1-12 | 错误分类 | 400（`parsing_exception`、`search_phase_execution_exception`）报 `INVALID_PARAMS`，回显 `error.type` 和截短、脱敏后的 `reason`；404 `index_not_found` 报 `INVALID_PARAMS`，固定码 `INDEX_NOT_FOUND`；通配符没匹配到索引时 ES 返回 200 加 0 条，算 `no_data`；401/403 报 `SOURCE_FORBIDDEN`；408、429、5xx 报 `SOURCE_UNAVAILABLE`；超过期限报超时 | A2-12、A3-5、A4-11 |
| B1-13 | 模块 | 新模块 `opspilot/tools/elasticsearch_logs.py` 加一个独立的 `ElasticsearchTransport`，按工具名路由（`otel_demo_ext.py:335`）；HTTP 和期限沿用 `http_deadline.DeadlineHandlers` | A4-12 |
| B1-14 | 合同测试（由不参与实现的 Agent 写） | ① 只有 `_search` 能 POST，路径和 query string 固定；② 索引名注入（`/`、`?`、`:`、`..`、前导 `_`）在发请求前被拒；③ 禁用的 DSL 构造在任意嵌套层都被拒；④ 映射有时间字段时时间过滤一定被加上、模型无法关闭，`global` 聚合被拒；映射里没有时间字段的索引不加过滤、来源时间为空、`cannot_prove` 写明不能证明属于事故窗（B1-U1）；⑤ size 和 from 越界被拒；⑥ 2 MiB、25k 超限整条拒绝；不完整的三种原因；⑦ 错误分类表；⑧ `es-ro` 与其他凭据槽不互相回退，凭据不出现在视图、notes 和 trace 里；⑨ 配置缺失或冲突的启动码，以及 web 不读凭据；⑩ `otel-demo` 字节不变，没有 ES 配置时扩展档位字节也不变（取决于 U2）；⑪ lab 闸门 | 参照 `tests/test_tool_expansion_a3/a5/b2_contract.py` |
| B1-15 | 本机替身 | 合同测试用本地 `http.server` 替身返回 ES 风格的 JSON，不需要容器 | A2-15 先例 |
| B1-16 | 在哪里跑 | 放 CI，新开一个 job `search-backends`（`ubuntu-24.04`，`timeout-minutes: 15`），用 service 容器，写法照 `ci.yml:111-128` 的 m0-postgres（镜像钉 digest，限 `--cpus`/`--memory`，配健康检查）。本机 swap 常满，不在本机跑；本机可以用 `-m` 标记跳过 | R8-25 |
| B1-17 | 镜像 | `docker.elastic.co/elasticsearch/elasticsearch:8.<最新 8.x>` 和 `opensearchproject/opensearch:<最新 2.x>`，开工时查当前最新补丁版本并钉 digest【未确认具体版本】。限额：ES `ES_JAVA_OPTS=-Xms512m -Xmx512m`、`--memory=1500m`；OpenSearch 同样设置 | R8-25 |
| B1-18 | ES 安全配置 | `discovery.type=single-node`、`xpack.security.enabled=true`、`xpack.security.http.ssl.enabled=false`（用 HTTP，省去从 service 容器取 CA 的步骤）、`xpack.security.authc.api_key.enabled=true`【推断：8.x 默认就开】、`ELASTIC_PASSWORD` 用一个明显的低熵测试串，加入 gitleaks 允许名单（记忆里的误报教训）。测试夹具用 elastic 账号创建 B1-5 的只读 API key，工具只用这把 key。HTTPS 加 CA 的路径由合同测试替身覆盖 | R8-25 |
| B1-19 | OpenSearch | `DISABLE_SECURITY_PLUGIN=true`，只验证三个接口的兼容性；basic 认证的路径在 ES 上验证（ES 也支持 basic）。OpenSearch 的只读角色留到 #671 冒烟时实测。B1-21 里的认证与权限断言（401、403、只读 key 直接写入被拒）只对 Elasticsearch 跑；OpenSearch 在 CI 只断言三个接口返回与投影一致，它的 basic 认证与 `readall_and_monitor` 只读拒绝由 #671 冒烟补上，冒烟记录里单列这一项 | R8-25 |
| B1-20 | 合成数据 | 夹具用 `_bulk` 加 `refresh=wait_for` 灌入：`logs-app-a`、`logs-app-b`（带 `@timestamp`，时间相对 now，含窗内、窗外、未来时刻各一部分，多个 service 和 level，一行 5000 字符的长消息，一条含 `password=...` 的消息）、`inventory`（没有时间字段）、600 条的大索引（用来验证截断），另建一个映射较大的索引 | R8-25 |
| B1-21 | 断言清单 | 三个工具都能返回真实数据；时间过滤和来源时间的首尾正确；映射里没有时间字段的 `inventory` 索引不加过滤、来源时间为空（B1-U1），带时间字段的索引无法绕过过滤；size 截断和 total 不完整；2 MiB 或视图超限拒绝；长行截断；脱敏标记；聚合正确；错误分类用真实响应验证（不存在的索引返回 404、DSL 写错返回 400、用错 key 返回 401、key 没有某索引权限返回 403）；只读拒绝分两层：禁用构造不发请求（ES 侧文档数和索引数不变）；只读 key 直接 `PUT`/`DELETE`/`_bulk` 返回 403（证明身份层）。不调模型、不跑题 | R8-25 |
| B1-22 | CI 耗时 | ES 和 OpenSearch 启动各约 30–60 秒，两者并行；测试约 1–2 分钟；这个 job 合计约 4–6 分钟，与现有 job 并行跑【估计】 | R8-25 |

交给 #671 短租冒烟（R8-26）：

- worker 环境变量：`OPSPILOT_TOOL_PROFILE=otel-demo-ext`；`OPSPILOT_ES_URL=http://127.0.0.1:<端口>`；`OPSPILOT_ES_API_KEY`（OpenSearch 用 `OPSPILOT_ES_USERNAME`/`OPSPILOT_ES_PASSWORD`）；HTTPS 时加 `OPSPILOT_ES_CA_FILE`。仍需要 `OPSPILOT_K8S_*` 和 Prometheus/Jaeger 地址，因为扩展档位的 worker 缺 K8s 配置就不启动（`profiles.py:99-121`）；269 等题本身也要 kind。
- 一个脚本：用工程师凭据创建只读 API key 或 OpenSearch 只读用户（B1-5），写到 `LAB_DIR` 下权限为 600 的文件里，不回显；上游评测题的 `before_test` 用的是 `ELASTICSEARCH_URL`/`ELASTICSEARCH_API_KEY`（工程师 key），产品用的是只读 key，两者分开。
- 冒烟需要的证据：至少一次真实 Run 调用了 `elasticsearch_*`，已发布的报告引用了它的证据；附 LangSmith trace 链接和冻结的 `summary.json`（lab 闸门通过，项目名以 `opspilot-lab-` 开头）；OpenSearch 跑 2 题（R8-12）。这些证据也满足 R8-9 的完成标准。

## r10（2026-11-18）：C5（#569）与 #666 预审裁决

预审者未参与实现，C5 对照 `main` `dde6fedd` 与上游 `a0009195`，全文在仓库外（`~/.agent/handoffs/opspilot-c5-prereview-2026-11-18.md`）；#666 见 [#666 任务记录](2026-11-18-holmes-evals-666.md)（预审全文 `~/.agent/handoffs/opspilot-666-prereview-2026-11-18.md`）。两名预审者各误见一行 C5 题的 `expected_output`（其一来自上游未加载的散落文件 `102a/test_case copy.yaml`），已在预审头部披露，两人此后不担任 C5 的实现者与测试作者。全量验收探针未跑（本机 swap 满），交各 PR 的 CI。

用户裁决（2026-11-18）：

| 编号 | 问题 | 裁决 |
|---|---|---|
| C5-U1 | Loki、VictoriaLogs 默认不做认证，产品的只读身份从哪来 | **A**：产品照上游接收地址加凭据（直连或经 Grafana 数据源代理；Basic、bearer 或请求头），不为评测改产品。CI 与租机上用 nginx 充当网关：要求登录，只放行工具用到的 GET 路径，其余 403；产品只持代理的只读账号；CI 证明经该账号写入被拒、数据条数不变。上游本身不保证只读（Loki 无用户体系，认证靠前置网关）；每个数据源一个凭据槽沿用 C3 §12，运维用统一网关时可把同一只读凭据配进各槽。备选 B（Grafana Viewer token 作统一入口）未选：Viewer 经数据源代理能否写入未核实 |
| C5-U2 / R10-1 | 固定时间题的题面与数据（C5 的 100a、101、259_loki；同样适用于 #666 的 Elasticsearch 固定时间题） | 允许改写题目时间：数据与题面里的日期、时间按**同一平移量**由评测脚本机械改写，规则只看数据与题面、不看答案；送 judge 前按 #666 PR-A 的反平移还原报告里的时间；结果页披露。答案隔离（R8-6）不变。R8-2「题面原文」据此放宽为「题面原文，时间按平移规则改写」 |
| H-U1 | 10 道多集群题的「本地集群名」 | **放进调查提示词，参考上游**（#722）：新增配置 `OPSPILOT_CLUSTER_NAME`，设了才在提示词里加多集群提醒（对标上游 `generic_ask.jinja2:7-17`），未设时提示词字节不变。产品改动单独一个 PR，开工前预审；#666 不改题面，部署时给 worker 设集群名 |

C5 可逆默认（Agent 自定，理由见预审全文）：C5-0 租户由运维配置，模型不选租户；C5-1 拆两个 PR，C5a Loki 先（7 题）、C5b VictoriaLogs 后（2 题）；C5-2 工具名照上游（Loki 只有 `grafana_loki_query`，VictoriaLogs 五个）；C5-3 只发 GET、路径写死、不新增 POST；C5-4 凭据槽 `loki-ro`、`vlogs-ro` 与 `OPSPILOT_LOKI_*`、`OPSPILOT_VICTORIALOGS_*`；C5-5 lab 闸门加 `LOKI_NOT_LAB`、`VICTORIALOGS_NOT_LAB`；C5-6 LogQL 拒 `offset`、拒超出查询窗的范围选择器、求值起点后推（照 PromQL 规则）；C5-7 LogsQL fail-closed 清单（`options(`、`join`、`union`、`stream_context`、子查询）待 CI 实测再放；C5-8 参数 `start`/`end` 走 `_query_window`；C5-9 元数据类返回的来源时间为查询窗；C5-10 投影与上限沿用 B1；C5-11 错误分类；C5-12 只改工具描述、不改调查提示词；C5-13 新模块 `loki_logs.py`、`victorialogs_logs.py`；C5-14 扩展档位按已配置后端集合计算，无后端与仅 ES 的 revision 字节不变；C5-15 合同测试清单；C5-16 至 C5-21 R8-25 集成测试（CI job `log-backends`、Loki 2.9.0 与最新 3.x、VictoriaLogs、nginx 只读网关、合成数据与断言清单）。

## r11（2026-11-19）：B3、C1、C2、C3、C4、C6、B4 与 #667 预审裁决

7 份预审由未参与实现的 Agent 完成，全文在仓库外：`~/.agent/handoffs/opspilot-{b3,c1,c2,c3,c6,b4c4,667}-prereview-2026-11-19.md`（各稿的「需用户裁决」表与「可逆默认」表）。用户 2026-11-19 PT 对 12 个待裁决项全部选推荐（原话「yes for all with recommend」）。r8–r10 继续有效，本节只修改 R8-14、修正 R8-18 措辞（见文末「与旧裁决的修改」）；C3 修订随各工具 PR。

| 编号 | 问题 | 裁决要点 |
|---|---|---|
| C6-U1 | 配进来的数据库账号其实有写权限怎么办 | 每次连接后、执行模型语句前，工具用写死的语句核对当前账号权限（白名单），不像只读就整条报 `SQL_ACCOUNT_NOT_READ_ONLY`，语句不发；评测脚本按预审授权清单建只读账号 |
| B3-U1 | 实时用量的采样时刻总晚于授权框末尾，能否当事实引用 | 归「当前状态类」：来源时间留空，每行带采样时刻与窗口，notes 写比框末尾晚多少，`cannot_prove` 写不能证明事故窗内的用量；C3 §8 那一条补半句「实时用量同属此类」 |
| C3-U1 | 计分的 SREGym Kafka 题没有认证，只读身份从哪来 | 该题匿名连，只读只靠工具层（只用管理客户端的 8 个只读调用、关自动建主题），结果页照实披露；CI 与自建场景用 SASL 账号加只给 Describe 的 ACL 证明两层只读 |
| C3-U2 | 自建 Kafka 功能验收用什么场景 | 一个场景：租机单节点 Kafka，`orders` 主题、停住的消费组 `order-processor` 滞后已知值（如 1100），另一个追平的组；网页问「订单主题的消费滞后是多少」，证据里有滞后数与组名算过；跑 1 遍 |
| C2-U1 | 上游 runbook 在现有知识库里没地方放 | 新增知识来源「导入的 runbook」：登录用户提交，独立的批准、驳回、撤销（记审核人、时间、原因，批准版本不可改，撤销后取不到），新迁移；复盘知识与其读取规则不变；C3 §10 随 C2 PR 修订；F13 第 2–3 步、F7 第 2 步须在新来源上重证。**修改 R8-14**，见下 |
| C2-U2 | 模型怎么知道有哪些 runbook | 照上游：建 Run 时把本目标已批准的 runbook 目录写进输入快照（新输入版本），作为一条不可信数据消息发给模型；只在 C2 开关打开时出现，开关关时提示词、输入版本、revision 字节不变 |
| B4C4-U1 | Helm 与 ArgoCD 历史要不要按事故时间框裁剪 | 不裁剪，按对象当前状态返回全部保留的历史，每条带自己的时间；来源时间留空，`cannot_prove` 写明框外条目也列出 |
| B4-残余风险 | R8-18 的残余风险怎么写、怎么收住 | 措辞改为「身份可读取合成环境内全部 Secret 的内容，只靠网关收窄到 Helm 记录」；只在扩展档位且 K8s 地址是 lab 主机（闸门 `HELM_NOT_LAB`）；网关解码后查询参数键集合固定、每键恰一次、值全等；CI 用同一 token 直接 list secrets 证明风险存在。**修正 R8-18 措辞**，见下 |
| C1-U1 | 任务清单工具要不要绕开只读网关 | 不经网关，由调查循环直接处理（串行、并行、故障补跑三处），结果存同一步的工具结果、受同一控制检查；不产生证据编号、报告不能引用；C3 §8 末尾加一句「调查行为工具」 |
| C1-U2 | 开关对照做多大 | 小对照：主跑照上游（283 开、其余关），另加 283 关着 3 遍、4 道 `hard` 题开着 3 遍，共多 15 次；单独成表、不设门槛，产品默认开不开看表后另开 issue |
| S-U1 | SREGym 没有题面原文怎么提问 | 固定模板，只填 `/get_app` 给的应用名、namespace、简介，句子取自上游 claudecode 驱动诊断段原话；模板钉 sha256，结果页贴全文；R8-2 在本轨读作「固定模板加上游三个字段」 |
| S-U2 | OpsPilot 走哪条路读 SREGym 集群 | 评测脚本另起一份 SREGym 同款过滤代理，上游凭据换成只读身份，OpsPilot 连这个代理（预审稿选项 c，即推荐项）；冒烟时一小时内跑不通就退到直连只读身份并披露可见范围差异；不走 SREGym 原代理 |

### 与旧裁决的修改

- **修改 R8-14**（C2-U1）：R8-14 要求上游 runbook「走现有审核流程入库」，但现有审核流程只收已批准复盘来源（`opspilot/migrations/versions/0008_postmortem_knowledge.py:339,348,556`：`source_postmortem_id` NOT NULL、外键指向复盘提议、触发器要求来源复盘版本已批准）。改为新增「导入的 runbook」来源与独立审核流程，来源仍标「HolmesGPT 评测夹具」；C3 §10 随 C2 PR 修订。r8 表原文不改。
- **修正 R8-18 措辞**（B4 残余风险）：Kubernetes 的 `list` 返回对象全文，原文「身份可列全部 Secret」改为「身份可读取合成环境内全部 Secret 的内容，只靠网关收窄到 Helm 记录」；加 `HELM_NOT_LAB` 闸门；网关解码后键集合固定、每键恰一次、值全等；CI 用同一 token 直接 list secrets 证明风险存在。r8 表原文不改。

### 可逆默认（Agent 自定，理由见预审全文）

- **B3**：B3-1 开关 `OPSPILOT_K8S_LIVE_METRICS=1`，按已配置后端集合算 revision；B3-2 工具名照上游 `kubectl_top_pods`、`kubectl_top_nodes`；B3-3 Pod 工具加可选 `namespace`；B3-4 只 GET、路径写死（metrics.k8s.io 与节点 allocatable）；B3-5 Pod 行含容器明细、节点行含百分比；B3-6 按 K8s 数量语法换算毫核与字节，解析失败 `MALFORMED_RESULT`；B3-7 来源时间与 `data_as_of` 留空，加 `sampled_at`；B3-8 独立错误映射（无 metrics-server 报 `SOURCE_ERROR`）；B3-9 新模块 `kubernetes_live_metrics.py`；B3-10 只改工具描述；B3-11 合同测试清单；B3-12 kind lab 装 vendored metrics-server。
- **C1**：C1-0 结果不是证据（无 `evidence_id`，不可引用）；C1-1 工具名 `TodoWrite`、参数 `todos`；C1-2 部署配置 `OPSPILOT_TODO_WRITE=on`；C1-3 提示词变体按已开段组合；C1-4 打开时才加的 `TODO_GUIDANCE` 段；C1-5 校验与上限（50 项等）；C1-6 纯函数生成结果，三处调用点接入；C1-7 返回内容；C1-8 工具描述改写自上游；C1-9 不计入查询数与工具调用数；C1-10 工作台显示为「Plan updated」行；C1-11 新 Run 从空清单开始；C1-12 只回复与最后一轮不带；C1-13 新模块 `investigation/todo.py`；C1-14 #666 驱动按 `enable_todo` 分批。
- **C2**：C2-0 一个工具 `fetch_skill(skill_id)`；C2-1 开关 `OPSPILOT_KNOWLEDGE_TOOL=on`，只在扩展档位；C2-2 能取当前有效的导入 runbook 与复盘知识；C2-3 只取本 Run 目标下的知识；C2-4 名字规范化照上游；C2-5 目录上限（50 条、8 KB）；C2-6 拒绝与「取不到」时列出可取名字；C2-7 证据投影含版本与内容哈希；C2-8 `citable_as_fact: false`；C2-9 工具描述；C2-10 正文最长 32 KiB；C2-11 新表与复盘知识分开、审核动作沿用 F13 请求与审计；C2-12 评测逐题导入、跑完撤销；C2-13 导入脚本在 #666 侧；C2-14 合同测试清单。
- **C3**：C3-1 依赖 `confluent-kafka==2.16.1`（运行时组 `m0`）；C3-2 搬上游 8 个工具、名字不改；C3-3 只用 `AdminClient` 的 8 个只读方法，不读消息；C3-4 `OPSPILOT_KAFKA_*` 配置与凭据槽；C3-5 有认证环境用 SASL 加 Describe ACL；C3-6 lab 闸门加 Kafka 项；C3-7 滞后算法与投影；C3-8 主题名、组名校验与返回上限；C3-9 每次调用一个短命客户端；C3-10 `KafkaError` 错误分类；C3-11 只改工具描述；C3-12 新模块 `kafka_admin.py`；C3-13 一个 PR；C3-14 合同测试清单。
- **C6**：C6-0 覆盖 7 题的 Postgres、MySQL、MariaDB、ClickHouse、SQL Server、MongoDB；C6-1 拆 C6a/C6b/C6c 三个 PR；C6-2 用上游默认工具名；C6-3 自写分词器做 SQL 语句检查；C6-4 会话只读事务（能做的方言）；C6-5 Mongo 各层拒 `$out`、`$merge` 与服务端 JS；C6-6 30 秒总期限与库侧语句超时；C6-7 `OPSPILOT_SQL_*`、`OPSPILOT_MONGODB_*` 配置，URL 不许带凭据；C6-8 与 OpsPilot 业务库三层隔离（`SQL_TARGETS_OPSPILOT_DB`）；C6-9 lab 闸门 `SQL_NOT_LAB`、`MONGODB_NOT_LAB`；C6-10 行对象投影与上限；C6-11 目录查询按方言写死；C6-12 驱动依赖各在引入它的 PR 里加；C6-13 扩展档位按后端集合算；C6-14 错误分类；C6-15 只改工具描述；C6-16 新模块 `sql_query.py`、`mongodb_query.py`；C6-17 合同测试清单。
- **B4**：B4-1 `helm_list`、`helm_history`、`helm_status`；B4-2 开关 `OPSPILOT_HELM_RELEASES=on`；B4-3 合成环境靠 lab 主机闸门；B4-4 不改现有只读 RBAC，另加 Helm 追加 ClusterRole；B4-5 网关放在唯一出口 `KubernetesClient.get`；B4-6 查询参数恰好一个、完全相等的理由；B4-7 URL 全由代码拼；B4-8 两层 base64 解码与输出白名单；B4-9 体积上限与专用读取循环；B4-10 坏条目丢弃并计数；B4-11 时间按 B4C4-U1；B4-12 错误分类；B4-13 残余风险原文（见上）；B4-14 覆盖页 `change_history`；B4-15 新模块 `helm_releases.py`；B4-16 工具描述。
- **C4**：C4-1 四个 `argocd_app_*` 工具；C4-2 开关 `OPSPILOT_ARGOCD=on`；C4-3 只读 `applications`，不含 `applicationsets`、`appprojects`；C4-4 另加 ArgoCD 追加 ClusterRole；C4-5 不进通用 `RESOURCES` 表；C4-6 白名单投影剔除 values 等敏感字段；C4-7 时间按 B4C4-U1；C4-8 不做 diff；C4-9 CRD 未装报 `SOURCE_ERROR`；C4-10 新模块 `argocd_apps.py`；C4-11 不加 lab-only 闸门。
- **#667 SREGym**：S-1 钉上游版本并启动时核对；S-2 回合脚本逐次调用上游入口；S-3 题目 ID 不进产品；S-4 只读身份在建集群后、首跑前建好；S-5 四层只读边界检查；S-6 单次尝试用上游默认 1800 秒；S-7 计分口径复用 #666，无报告提交空串；S-8 上游输出与判分理由留 `tmp/`，summary 按白名单取；S-9 不用上游可调旋钮；S-10 报告转文本复用 #666；S-11 沿用扩展档位唯一目标；S-12 指标、链路、日志经 port-forward 直连；S-13 `--internet-access open`；S-14 显式 `KUBECONFIG` 防指错集群；S-15 端口分配；S-16 结果页披露清单。
