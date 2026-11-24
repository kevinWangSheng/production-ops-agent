# 合同预审 D：#197 #124 #134 #159 #391

- 预审者：只读 Agent，未参与实现；基线 `main` `dda03323`（2026-11-24）。
- 依据：AGENTS.md、`docs/tasks/README.md`「合同预审」、PRODUCT-CONSTRAINTS.md、C3 §3/§4/§10/§11、五个 issue 正文（均无评论）、真实代码与 Compose/kind 配置。
- **全量探针未执行**：宿主 swap 16.2G/17.4G 已用（`sysctl vm.swapusage`），lead 指令禁止跑 pytest。下面按 grep 列出钉值文件，进入实施前必须在临时 worktree 补跑 `tests/acceptance` 探针，失败清单追加进决定表。
- 背景约束（已裁决，不重问）：post-M3 方向是「拓宽数据面、只保主路径、对标 HolmesGPT，不为人工介入等边角 case 投入」；可以照实披露已知限制；C3 实现细节由 Agent 自定。

---

## 总览与推荐

| Issue | 推荐 | 一句话理由 | 若做：PR 与规模 | 真实运行 |
|---|---|---|---|---|
| #197 Controller/Gateway 拆分 | **先关闭，作为已披露限制；C3 §3 改写成现状**（见 Q1）。要演示安全设计时再做最小版 Gateway | 上游 HolmesGPT 也是单进程持有工具凭据；完整拆分是 L+ 级拓扑改动，换不来评测分数；限制已写在 limitations/architecture/threat-model | 最小版（只拆 Gateway）：3 个 PR（L+M+S）；完整版（加 Controller API）再加 4 个以上 | 最小版需要：kind 上重装 + 1 次真实 Run + 从 worker 容器做不可达探测 |
| #124 接管后交还 | **关闭，作为已披露限制**；顺手修一处文案与行为不符（Agent 自定） | 正是「人工介入边角 case」；当前的出路（关闭，新告警开新事故）可以用 | C3 文字 1 个 PR（S，用户门）+ 实现 1 个 PR（M） | 若做：1 次有界真实 Run（碰到观察撤销） |
| #134 pod 重启信号 | **关闭，作为已披露限制** | 改 checkout profile 会让发布校准失效（必须重做 U2 五类场景校准，属用户门）；Deployment 级 `min_over_time` 已覆盖大部分重启 | 2–3 个 PR（M 各一）+ 可能的 U2 重校准 | 需要：kind 上 pod 重启场景 + F6 复跑；若改同一 profile，还要重跑 U2 |
| #159 Observer trace | **关闭，作为已披露限制**，把 Observer 路径的证据口径写清（Q-159-1，碰 AGENTS.md 属用户门） | 已有 `/metrics`、冻结 `summary.json` 与离线重放；补 span 要给 Observer 新增一把 LangSmith key，还要绕开独立性安全测试 | 1 个 PR（M） | 需要：1 次 kind 恢复观察 |
| #391 发布校准钉死 cluster_uid | **关闭，作为已披露限制**，并把「换集群 = 一个用户门 PR 加一条校准条目」写成既定流程（Q-391-1） | F9 `passes` 已是 true，issue 写的阻塞已经不成立；不改代码就有出路 | 不改代码：每个集群 1 个 S 级用户门 PR；改匹配规则：1 个 M 级 PR | 加条目不需要；改规则要 1 次发布观察真实运行 |

---

## #197 C3 §3 完整角色隔离

### 已核查事实

- C3 §3 表格要求 Controller 是唯一业务写入口、Tool Gateway 持有目标只读凭据，「隔离必须落实到凭据、数据库权限和网络可达性。仅拆成不同进程，不算完成」（`technical-proposal-2026-09-07.md:52-62`）。C3 §3:64 已把「凭据由 worker 进程内执行器持有」写成已知差距。
- C3 §10 已经让 web 进程充当一部分 Controller：#184 的自动授权「由 Controller 侧可重放任务（暂在 web 进程内循环，`opspilot_web` 角色）」（`:359`）；发布自动授权同样在 web 里跑（`docs/development.md:135`）。
- worker 直接写库：`worker.py:63-229` 调用 `lease_current / rebuild / abandon / commit_tool / publish / settle_answered / claim`；worker 进程里还跑 postmortem、outbox、Chat 线程（`compose.yaml` worker 段的连接池注释）。数据库侧防线是 0011 的显式授权加角色守卫触发器（`0011_runtime_roles.py:332-375`）。
- 凭据：`worker_env` 同时装着模型 key、LangSmith key 和全部目标只读凭据（`compose.yaml` worker 段；`tools/profiles.py:1-80` 列出 K8s/ES/Loki/VictoriaLogs/SQL/Kafka/MongoDB/Redis 的凭据变量，「只有 worker 读」）。web 只读地址、不读凭据（`profiles.py:254-288`）。
- 网络：所有 Compose 文件都没有 `networks:`（grep 为空），全部服务在同一个默认网络。kind 实验环境里 `connect-control` 把 web/worker/observer 三个容器都接进 `kind` 网络（`docs/development.md:197`）。worker 必须能出公网访问模型（`compose.drill-egress.yaml:1-11`，DeepSeek）。
- 代码已有可用的切口：`ReadOnlyTransport.fetch(TransportRequest) -> TransportResponse`（`tools/executor.py:491-494`），`TransportRequest` 的注释是「Everything the transport needs, and nothing secret」，凭据只以不透明的 `credential_ref` 出现（`:343-375`）。有 20 多个 Transport 实现（argocd、ES×2、helm、kafka、k8s×4、loki、mongo、otel、prometheus_ext、redis、sql、victorialogs、jaeger）。其中 `KnowledgeTransport`、`EvidenceReadTransport` 读的是 OpsPilot 自己的库，不是目标系统，拆分时要留在 worker。
- 进程内子进程：`kubernetes_core.py:1067` 起 `jq`、`_regex_child.py` 跑正则子进程。它们是在 transport 里还是在 executor 里处理已取回的字节：**未确认**。
- lab trace 证明 `check_lab_target` 读 worker 环境里的各目标地址（`tracing.py:183-243`）。拆分后 worker 仍要拿到地址（不带凭据），和 web 现在一样。
- Observer 独立性安全测试禁止 Observer import `opspilot.tools`（`tests/safety/test_safety_recovery_authority.py:187-197`）。Gateway 如果放进 Observer 镜像入口，要注意这条约束。
- 已裁决：#169 U1/U5/U10 只做增量隔离（`docs/tasks/2026-10-11-m1-pg-runtime-roles.md:12,26`）；F7 U2-a「F7 `passes` 不要求 #197」（`docs/tasks/2026-10-15-f7-contract.md:63`）；F3 U9-a 继承这一差距；F9 第 1 步「照现状打包、不扩权」（`2026-10-27-m3-f9-delivery-contract.md:26`）。对外文档已披露：`docs/portfolio/limitations.md:57`、`architecture.md:45`、`threat-model.md:31`。
- 上游：HolmesGPT 在进程内用 subprocess 执行工具（`docs/research/upstream-led-project-plan-2026-09-06.md:21`，tools.py）。它是否在同一进程持有所有 toolset 凭据：**未确认**（仓库文档没有直接写）。

### 必答类别

1. 时间：Gateway 单次调用超时 = `TransportRequest.timeout_seconds` 加固定余量；deadline 仍由 executor 计算（`http_deadline.py`）。Agent 自定。
2. 计数与口径：预算与用量计数留在 worker executor 和 PG，Gateway 不另记。可逆默认。
3. 状态转移：Gateway 无状态，不碰 Run/Incident 状态。
4. 主体与权限：**用户门**（Q-197-2/3/4）。
5. 数据合同：不需要迁移；新增 `gateway_env` secret 和一个 Compose 服务。部署顺序：先 gateway，后 worker。
6. 旧数据兼容：证据格式不变（TransportResponse 原样序列化），旧证据不受影响。
7. 并发与水位：Gateway 并发上限 ≥ worker 容量（当前为 2）。Agent 自定。
8. 外部调用失败：Gateway 不可达时映射为 `TransportUnavailable`，走现有的 unavailable 结果分类，不新增状态。
9. 验收投影：F7/F9 `passes` 不翻；`tests/acceptance/test_392_compose_contract.py` 钉了服务列表（`:94-110`），需要全新上下文的作者增补断言；F9 第 1、4 步要在新拓扑上重新取证。

### 需用户裁决

| 编号 | 问题（行为语言） | 推荐 | 取舍 | 兜底 |
|---|---|---|---|---|
| Q-197-1 | 这一项现在做不做？选项：(a) 不拆，关掉 #197，把 C3 §3 改写成现状：「web 进程充当 Controller，处理控制与授权写入；worker 只在租约和代际栅栏下写自己 Run 的步骤、证据和结论，由数据库授权和守卫触发器兜底；目标凭据在 worker 里，网络不隔离，这是已知偏离」；(b) 只拆 Tool Gateway：目标凭据和目标网络都只给一个新的 gateway 进程，worker 的写库方式不变；(c) 完整拆分：(b) 再加 Controller API，worker 不再直连业务表 | **(a)** | (a) 零代码、说法诚实，但 C3 原条款降级；(b) 能演示「模型进程拿不到目标凭据」，代价 3 个 PR 和一次 kind 重装；(c) 是 L+ 级，要改 worker 全部写路径 | 用户没表态时按 (a)，issue 关闭时附 C3 文字 PR 的链接 |
| Q-197-2 | （选 b/c 时）Gateway 自己校验什么？(i) 只校验：请求的目标必须在 gateway 自己配置的目标清单里，动词只读，超时和大小有上限；代际、挂起、预算的复核留在 worker；(ii) 另外用自己的只读数据库角色复核控制代际和挂起 | **(i)** | (i) 改动小，但 C3「独立校验查询授权、预算」只部分满足，要写成偏离；(ii) 符合原文，但要新增一个 PG 角色和授权 | 按 (i)，在 C3 §3 写明偏离 |
| Q-197-3 | （选 b/c 时）网络隔离做到哪一层？只保证 kind 实验环境：只有 gateway 接进 `kind` 网络，worker 不接（Docker 默认隔离不同 bridge）；worker 仍能出公网访问模型；真实部署靠运维防火墙，文档写明 | **只做实验环境这一层，并写明** | 不做出口代理就成本低，但 worker 理论上仍能访问任何公网可达的目标 | 同推荐 |
| Q-197-4 | （选 b/c 时）升级时凭据怎么搬：worker 启动时发现 `worker_env` 里还有任何目标凭据变量就拒绝启动（fail closed），由 `install.sh upgrade` 和 `kind_lab.py control-secrets` 自动把它们挪进 `gateway_env` | **拒绝启动 + 安装器自动搬** | 防止凭据留在两处；代价是已有安装在升级时必须走安装器 | 只告警不拒绝（更宽松） |

### Agent 自定（可逆默认）

- RPC 形态：内部 Compose 网络上的 HTTP/JSON，body 用 base64，datetime 用 ISO。理由：和现有 health 监听器同一种技术栈，不引入新依赖。
- worker↔gateway 的认证：installer 生成一个共享 token 文件 secret。理由：现有 secret 都按同一模式处理。
- Gateway 复用产品镜像，新命令 `python -m opspilot.tools.gateway`，带 9464 health；`KnowledgeTransport` 和 `EvidenceReadTransport` 留在 worker。
- jq/正则子进程：跟着它所在的 transport 走（具体位置未确认，实施时先核实）。

### PR 拆分（若选 b）

1. `feat: tool gateway process`（L）：gateway 服务端、`RemoteTransport` 客户端、序列化往返测试、fail-closed 启动检查。
2. `chore: compose/installer topology`（M）：新增服务和 `gateway_env`、networks、`control-secrets` 和 `connect-control` 改为接 gateway、`compose.lab.yaml`。
3. `docs+evidence`（S–M）：kind 重装、1 次真实 Run、从 worker 容器探测目标不可达（复用 `scripts/k8s_authz_probe.py` 的思路）、C3 §3 文字。

---

## #124 接管后交还自动调查（human_owned → automatic）

### 已核查事实

- 领域模型只有 automatic → human_owned 一条边：`domain/control.py:105-108`（接管时 `mode=human_owned`、`observation_authorized=False`）。`authorize_observation` 只在 human_owned 下可用，并且「不恢复自动调查」（`:109-117`）。
- 持久化：接管时代际 +1、撤销观察会话、当前 Run 改为 `waiting_human` 并收回租约（`persistence/controls.py:476-519`）。代码里没有任何地方把 `mode` 写回 automatic（grep `SET mode` 只命中 `mode='human_owned'`）。`new_run` 在 human_owned 且未关闭时被拒绝（`persistence/runs.py:67-70`），`claim` 也拒绝（`runs.py:199-201`）。
- C3 §10：「接管默认停止自动观察；人工可在 human_owned 状态下单独授权观察，但该授权不恢复 Agent 自动调查」（`:355`）；消息在「human_owned 下只记录、不开 Run」（`:359`）。
- 已裁决（2026-10-08，issue 正文）：先不加；以后需要时先补 C3 条款（版本递增、观察授权处理、是否新开 Run），再实现。
- **发现一处文案与行为不符（展示层，不阻塞）**：工作台在 Human-owned 状态的 ⋯ 菜单提供 `resume`（`web/incident_view.py:116`），提示语是「Put the Run back in the queue; it continues from its committed steps.」（`:137`）。但 human_owned 下的 resume 只解除主体暂停，不碰 Run（`controls.py:611-615`），也不会交还给 Agent。重设计稿把这个按钮叫「Resume automatic（resume）」（`docs/design/workbench-redesign-2026-11-17.md:166`），合同没有采纳这个说法。对未暂停的 human_owned 事故点 Resume，会推进代际但没有任何效果：**未实测**。
- 审计动作 `opspilot_controls.action` 没有 CHECK 约束（`0002_state_checks.py:15-18`），新增 `handback` 动作不需要迁移。
- 上游：重设计稿对照的 AWS DevOps Agent 功能表（`workbench-redesign-2026-11-17.md:291-308`）没有接管或所有权概念；AWS 怎么处理交还：**未确认**。HolmesGPT 是按单次请求或会话调查，没有事故所有权模型：**未确认**（仓库研究文档没有涉及）。

### 必答类别（做的话）

1. 时间：交还后新 Run 的 deadline 怎么算。推荐与 Re-investigate 相同；Re-investigate 的预算和 deadline 口径**未确认**，实施前核实。
2. 计数：累计预算不清零（C3 §4 挂起规则的同类原则）。
3. 状态转移：见 Q-124-2..5。
4. 主体：只有登录用户能交还，actor 是该用户；worker 不能触发。
5. 数据：不需要迁移（`mode` 列已允许 `automatic`；action 没有约束）。
6. 旧数据兼容：已是 human_owned 的事故可以直接交还；历史 Run 保持终态。
7. 并发：`expected_version` 条件更新；和接管、关闭、登记处置竞争时先提交者胜。
8. 外部调用：交还事务里不调用模型或数据源。
9. 验收：`tests/acceptance/test_workbench_conversation.py`、`tests/integration/test_m1_incident_control_postgres.py`、`test_f12_acceptance_postgres.py` 断言了「human_owned 不开 Run」，需要按新合同增补用例，不能削弱原有断言（交还之前仍然不开 Run）。

### 需用户裁决

| 编号 | 问题（行为语言） | 推荐 | 取舍 | 兜底 |
|---|---|---|---|---|
| Q-124-1 | 事故被人接管后，要不要提供「交还给 Agent」？ | **不提供**，关闭 #124，在 limitations 写一句：「接管不可撤回；要让 Agent 再查，关闭事故，新告警会开新事故」 | 不提供：零成本，符合「不做边角 case」；提供：多一个控制动作、C3 新条款，加 1 个 M 级 PR | 按「不提供」 |
| Q-124-2 | （提供时）交还按普通控制动作处理吗：带 `expected_version`、代际 +1、写审计 `handback` | **是** | 和其他控制动作一致 | 同推荐 |
| Q-124-3 | （提供时）人在接管期间单独授权的恢复观察，交还时怎么处理 | **在同一事务里撤销** | 和 C3 §10「更换 Run 不默认继承旧观察授权」一致；代价是正在跑的观察要重新授权 | 撤销 |
| Q-124-4 | （提供时）交还后 Agent 立刻开一个新 Run 接着查（`waiting_human` 的旧 Run 取消，新 Run 读到全部未读输入），还是只恢复自动模式、等人点 Re-investigate | **立刻开新 Run**（服务层把取消和开新 Run 放在一起做，和 Re-investigate 相同） | 立刻开：行为符合「交还」的直觉；只恢复模式：会多一个「已交还但什么都没发生」的状态 | 立刻开新 Run |
| Q-124-5 | （提供时）哪些生命周期允许交还 | **只允许 open/observing_recovery**；resolved/closed 先 Reopen；全局或目标挂起期间可以交还，但新 Run 要等挂起解除才会被领取 | 和 rebind 的规则（`controls.py` rebind 段 `REOPEN_REQUIRED`）一致 | 同推荐 |
| Q-124-6 | （提供时）交还后 Agent 发布的结论，能不能自动开恢复观察（#184） | **不能**：接管已经算「有人工处理」，这个事故以后都走人工登记 | 保守，不用改 D20 的判定；代价是交还的事故不会自动确认恢复 | 不能 |

### Agent 自定

- 不管 Q-124-1 怎么裁：把 human_owned 下 Resume 的提示语改成实际行为（「Lifts the pause only; the Agent stays stopped.」），或者在未暂停时隐藏这个按钮。理由：展示层，不改合同。
- 做的话：动作名 `handback`，按钮文案「Hand back to Agent」，放在 Human-owned 状态的主按钮位。

---

## #134 HealthProfile pod 级信号按 owner 链绑定

### 已核查事实

- 现在的 profile 删掉了 `pod_restarts_in_window`，改为 Deployment 级 `deployment_ready_replicas` 和 `deployment_available_replicas_min_in_window`。描述里写明「两次抓取之间（lab 是 60 s）来了又走的重启看不到」（`observer/profiles/otel-demo-checkout.json` 对应 signal 的 description）。
- 校验器是手写的 PromQL 词法分析器：要求每个向量选择器都带 `namespace` 和 workload 匹配，不带就拒绝（`observer/health_profile.py:188-234`，`_vector_selectors` 位于 `:872`，`_check_selectors_bound` 位于 `:1196`）。pod 级序列没有 `deployment` 标签，所以 `on()/group_left` join 会被直接拒绝；要支持 join，校验器得理解 join 的拓扑，这是 issue 前提 2。
- **代码里已有 owner 链的先例**：发布观察在代码里拼查询，用 `kube_replicaset_owner{…,owner_kind="Deployment",owner_name=<service>}` 和 `kube_pod_owner{…,owner_kind="ReplicaSet"}`，经 `and on(namespace,pod,uid)` 限定 pod，再按 UID 查询（`observer/release_evaluation.py:167-232`）。标签形状有探针证据（`docs/evidence/f11-pr2-telemetry-probe/run.md:9-10`）。KSM 已开 pods 和 replicasets 收集器（`scripts/kind_lab/kube-state-metrics-values.yaml:15-19`）。因此 issue 前提 1 的标签部分已有证据；`kube_pod_container_status_restarts_total` 在 join 后的序列形状：**未确认**。
- **关键耦合**：发布观察只接受信号集合完全等于 `_SUPPORTED_QUERIES` 的 profile（`release_evaluation.py:100-123`）。发布校准清单钉住了 `health_profile_revision = otel-demo-checkout@b72bbe2e30be` 和它的 sha（`release-calibrations.json`）。给 checkout profile 加信号会产生新 revision，之后的新发布都会被拒绝（`RELEASE_POLICY_UNSUPPORTED`/`UNCALIBRATED`），直到重做 U2 校准并通过用户门 PR 写入。2026-11-01 Q10 裁决也写过「会使 F6 profile 与 F11 U2 冻结校准失效」（`docs/tasks/2026-10-27-m2-exit-m3-opening-decision.md:138`）。另外，目标身份条目只有一个 `health_profile_id`（`schema.py:177-181`），恢复和发布共用。
- 钉值文件（grep）：`tests/f11_release_support.py:71-106`（profile revision 和 sha）。
- 已裁决：用户 2026-10-08 选方案 B（Deployment 级），#134 是方案 A 的后续。

### 需用户裁决

| 编号 | 问题（行为语言） | 推荐 | 取舍 | 兜底 |
|---|---|---|---|---|
| Q-134-1 | 恢复观察要不要把「窗口内 pod 重启次数」加回来？ | **不加**，关闭 #134，限制保留在 profile 描述里，并在 limitations 写一句 | 加的话，两次抓取之间的快速崩溃循环能被检出；代价是 checkout profile 换版本，发布校准作废，要在 kind 上重跑 U2 五类场景并过用户门 | 不加 |
| Q-134-2 | （加时）重启信号由谁拼 owner 链：(i) Observer 代码按固定模板拼（和发布观察同一套写法），profile 里只声明「这是工作负载 pod 的重启信号」；(ii) profile 作者手写 join，校验器做 AST 级拓扑证明 | **(i)** | (i) 不需要通用的 join 证明，复用已审过的模板；(ii) 更通用，但校验器要大改，审查面也大 | (i) |
| Q-134-3 | （加时）profile 怎么换版本：(i) 改 `otel-demo-checkout` 本身，并重做发布校准；(ii) 让目标身份分别指定恢复用和发布用的 profile（数据合同改动），发布继续用旧 revision | **(i)** | (i) 一套 profile，但要重跑 U2；(ii) 不用重跑，但目标身份文件格式和校验都要改 | (i) |
| Q-134-4 | （加时）覆盖率口径：新 pod 在窗口中途出现时样本少，这段时间算「信息不足 → unknown」，还是只要求当前 owner 下每个 pod 至少有 `minimum_samples` 个点 | **算 unknown**（保守，不会误判健康）；代价是每次换 pod 之后约一个评估窗口（300 s）内拿不到 healthy | 宽松口径会让刚重启的 pod 很快「看起来健康」 | unknown |
| Q-134-5 | （加时）健康阈值：当前 owner 链下所有 pod 在窗口内的 `increase(restarts)` 等于 0 | **是** | 简单；新 pod 的计数器从 0 开始，旧 pod 退出 owner 链后不再计入 | 同推荐 |

### Agent 自定

- 新信号名 `workload_pod_restarts_in_window`；查询模板放在 `observer/` 里与发布模板共用；`_SUPPORTED_QUERIES` 同步加一项。
- 真实运行：kind 上注入一次 crash-loop（工程 harness 权限）取证，再复跑 F6 第 1 步。

---

## #159 Observer 路径的 OTel span

### 已核查事实

- `opspilot/tracing.py` 有 off/outbox/lab 三种模式（`:8-25`）。lab 模式的证明 `check_lab_target` 读的是调查侧变量：`OPSPILOT_TOOL_PROFILE`、`OPSPILOT_OTEL_PROMETHEUS_URL` 等，还有 `LANGSMITH_*`（`:183-243`）。Observer 用的是 `OPSPILOT_OBSERVER_PROMETHEUS_URL`，两者对不上。
- **安全测试禁止 Observer import `opspilot.tracing`**：`tests/safety/test_safety_recovery_authority.py:187-197` 和 `tests/m1_observer_support.py:270-280` 都把它列为「investigation side」（C3 §3 D3 独立性）。
- Compose 里 Observer 没有 `OPSPILOT_TRACE`，`observer_env` 只放 Observer 自己的 Prometheus 账号（`compose.yaml` observer 段）。补 span 等于给 Observer 加一把 LangSmith key。
- Observer 已经有 `/metrics` 和 readiness（`observer/__main__.py:221-247`）。
- AGENTS.md:43 要求「触碰调查 loop、报告校验或恢复路径的 PR」附 LangSmith trace。只改 Observer 的 PR 做不到，#158 的做法是附 `summary.json` 加离线重放，并披露缺口。
- 已裁决（2026-10-08）：暂不补，记为已知缺口；如果补，只加领取、逐信号查询、提交三类 span，不带凭据和原始响应体，接 lab 级导出，失败即关闭，和调查侧用同一开关。
- 上游 HolmesGPT 没有独立的 Observer（ADR-0006 背景只描述了工具 span 和 LLM span）。

### 需用户裁决

| 编号 | 问题（行为语言） | 推荐 | 取舍 | 兜底 |
|---|---|---|---|---|
| Q-159-1 | Observer 要不要发 trace？不发的话，只改 Observer 的 PR 用「冻结 `summary.json` + 离线重放 + `/metrics`」作为证据，这句要写进 AGENTS.md:43 | **不发**，关闭 #159，同时改 AGENTS.md 那一句（改 AGENTS.md 需要你授权） | 不发：Observer 继续不碰模型侧任何凭据，独立性最干净；发：恢复路径能在 LangSmith 里和调查放在一起看 | 不发；AGENTS.md 不改时，在 limitations 里注明 |
| Q-159-2 | （发时）Observer 能不能拿一把 LangSmith key（只在 lab 模式，项目名以 `opspilot-lab-` 开头才导出） | **可以，只限 lab** | LangSmith 已经是批准过的 lab 接收方；但 Observer 多了一把凭据和一个出口 | 不给 key，此项作废 |
| Q-159-3 | （发时）独立性怎么守：Observer 用一个只给自己用的小 tracing 模块（不 import `opspilot.tracing`，安全测试不动），还是放宽安全测试 | **单独模块，安全测试不动** | 会有少量重复代码；放宽测试等于削弱验收 | 单独模块 |

### Agent 自定

- span 属性白名单：session_id、主体类型和 id、采样序号、信号名、结果码（ok/no_data/stale/timeout/error）、耗时；lab 模式附 PromQL 文本，不附响应体。理由：符合 2026-10-08 裁决，查询文本里没有凭据。
- lab 证明：`OPSPILOT_TRACE=lab` 加 Observer Prometheus 地址是 lab 主机，加项目名前缀和 key，缺一项就整轮不导出。outbox 模式下 Observer 不参与，不新增数据库授权。

---

## #391 发布校准清单钉死开发 kind 的 cluster_uid

### 已核查事实

- 清单只有一条：`target.cluster_uid = c8001471-…`，`integration_id/resource_uid = m0-otel-20260909`（`opspilot/observation/policies/release-calibrations.json:9-15`）。路径写死在包内（`release_policy.py:45-47`），模块首行写明「never a runtime override」。
- 匹配规则：`calibrated_revision_for` 要求清单的 `target` 和发布的 `target_snapshot` 五个字段完全相等，并且 profile revision 一致（`observation/releases.py:190-213`）。授权时还要再比一遍 profile sha 和 M 等参数（`release_commands.py:705-740`）。
- **产品运行时从不核对 cluster_uid 是否真是某个集群**：它只取自运维写的目标身份文件（`schema.py:146-151`）。只有 U2 校准 harness 拿 kube-system 命名空间的 UID 核对过（`scripts/f11_u2_harness.py:1396-1399`）。租机和 kind_lab 写入的是 `opspilot-m1`（`scripts/kind_lab.py:530`；`docs/development.md:197`），所以现在任何新装环境都对不上清单。开发机上那个原集群是否还在：**未确认**。
- 已裁决：F11 P2-04「授权必须匹配受版本控制、绑定 profile revision、比较规则、数值和证据哈希的冻结清单；没有 `allow_uncalibrated` 产品开关」，「唯一产品清单位置」（`docs/tasks/2026-10-13-f11-pr2-contract.md:102-118,883`）；写入正式清单是用户门 PR（`2026-10-14-f11-u2-calibration.md:23`）。B3-R15 已在 `docs/portfolio/limitations.md:21` 披露，并保持 issue 开着（`2026-11-22-next-batches-b3-prereview.md:56`）。
- F9 `passes` 已是 true（`feature_list.json`），issue 里写的「Blocks F9 cross-host wiring」已经不成立。
- 钉值文件：`tests/f11_release_support.py:71-106`。
- 数据兼容：已登记的发布冻结了 `calibrated_policy_revision` 和 sha，恢复时再查清单（`release_commands.py:728-734`）。旧条目只要不删、不改，旧发布就不受影响。

### 需用户裁决

| 编号 | 问题（行为语言） | 推荐 | 取舍 | 兜底 |
|---|---|---|---|---|
| Q-391-1 | 换一个集群（租机、staging）后要做发布观察，怎么办？(a) 不改代码：每个集群提一个用户门 PR，在清单里加一条目标改为新集群、其余数值和证据引用原 U2 校准的条目，`approval_ref` 写明「沿用 U2 校准，未在该集群重测」；(b) 改匹配规则：校准只看 namespace、workload 和 profile revision/sha，不看运维声明的 cluster_uid、integration_id、resource_uid；(c) 允许运维在部署时挂一份自己的清单 | **(a)，并关闭 #391**（把流程写进 limitations 那一行） | (a) 不改代码，P2-04 不动，每个集群要一次批准；(b) 一劳永逸，但一个集群的校准会默认推广到所有同名工作负载，等于修改 P2-04；(c) 直接违反 P2-04 | (a) |
| Q-391-2 | （选 a 时）沿用的条目要不要先在新集群上跑一轮最小复核（比如 U2 里健康和即时回归各 1 次）再批准 | **不要求**，在 `approval_ref` 里注明未复测 | 省一次 kind 窗口；代价是 M=1500 的余量在新主机的抓取节奏下未经验证 | 不要求 |

### Agent 自定

- 选 (a) 时清单格式不变（format_version 1），新条目的 revision 按现有哈希规则算；测试只加「两条目时匹配唯一」的用例（`RELEASE_POLICY_AMBIGUOUS` 已有）。

---

## 待补的探针与核实（实施前）

1. 在临时 worktree 跑 `tests/acceptance` 全量探针：#197 用「Compose 加 gateway 服务」的草稿补丁；#134 用「profile 加一项信号」的草稿补丁，预期 F11 相关钉值会失败；#391 用「清单加一条」的草稿补丁。
2. #197：核实 jq 和正则子进程在 transport 还是 executor。
3. #124：核实 Re-investigate 的预算和 deadline 口径；实测对未暂停的 human_owned 事故点 Resume 的效果。
4. #134：在 kind 上取 `kube_pod_container_status_restarts_total` join 后的序列形状。
