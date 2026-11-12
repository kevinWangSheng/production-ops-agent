# 工具面拓展：决定表（r1）

- 修订：r1，用户 2026-11-12 裁决（口头，约 08:00–08:50 PT；同日下一会话复述后确认）。
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
| R6 | 分页与资源解析 | 每页 500；用 API 发现接口解析资源名 | 同上游；累计受 2 MiB 与 30 秒约束 |
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
| R17 | 模型传的 `namespace` | 不传时由网关填目标登记的 namespace；传别的 namespace 照常查询，不拒绝 | N4 裁决后集群内都在授权范围；预审原默认（不一致就拒绝）作废 |
