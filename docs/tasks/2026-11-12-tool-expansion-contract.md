# 工具面拓展：决定表（r2）

- 修订：r1，用户 2026-11-12 裁决（口头，约 08:00–08:50 PT；同日下一会话复述后确认）。r2（2026-11-12）：集群对象只读查询（A2，#554）开工前预审，没有新的用户裁决项，追加 15 条可逆默认并改写 R6、R17，见文末。
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

仍未决定、不挡 A0：N1 Lite 快照如何映射到查询端点（探针 #571）；N2 现场版与 Lite 同号场景是否一致（未确认，不合并分数）；N3 Jaeger 能否做标签过滤、按 ID 取整条、操作发现（trace 工具 #556 开工前探针）；N6 工具 search/catalog（有运行数据再定）。

## 可逆默认（实现者自定）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| R1 | 用 `kubectl` 还是直接打 API | 直接 HTTPS GET 打 API server，Bearer token 加 CA 文件 | 复用现有 GET-only 传输；镜像不加 `kubectl`、不出现 kubeconfig |
| R2 | 扩展档位的组成 | 新档位，复用 `otel-demo` 的全部工具（字节不变）再加新工具 | 不动现有档位版本 |
| R3 | 凭据引用 | 每个后端独立 `credential_ref` 与 `OPSPILOT_K8S_*` 变量，不互相回退 | C3 §12 凭据分离 |
| R4 | token 形态 | `kubectl create token` 的有期限 token，放 lab 私有目录 | 不留长期 Secret |
| R5 | 集群身份的动词与范围 | ClusterRole 加 ClusterRoleBinding，规则照上游默认规则（无 `secrets`），只给 `get`、`list`，不含 `watch` | N4 裁决为集群级；工具不用 watch |
| R6 | 分页与资源解析 | 每页 500；资源名用静态表解析（r2 改写，见 A2-5、A2-9） | 没有真集群时 API 发现风险最高；Secret 与未授权资源自然不在表里 |
| R7 | 对象投影 | 去掉 `metadata.managedFields`，其余原样交给 jq | 纯噪声且体积大 |
| R8 | 日志条数 | 默认 100，硬上限 1000，超出标「不完整」与省略条数 | 同上游默认 |
| R9 | 多容器与上一次日志 | 全部容器，当前与上一次各取一次 | 照搬上游 |
| R10 | 模型给的正则 | Python `re`，不区分大小写，在请求期限内执行 | 照搬；防病态正则 |
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
| A2-14 | 「只接 lab 或受控快照」是否在代码里强制 | 不强制；`may_contain_secrets=True` 只是声明，由 lab 闸门与运行规则约束 | 照实宣称即可，不为此搭技术隔离 |
| A2-15 | 合并与关闭时机 | 代码 PR 凭替身 API 响应加合同测试即可提交；#554 留着，租机跑完真实 Run、有 trace 与 `summary.json` 后再关 | A2 不碰调查循环、报告校验与恢复路径 |
