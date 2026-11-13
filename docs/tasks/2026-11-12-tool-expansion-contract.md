# 工具面拓展：决定表（r3）

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
