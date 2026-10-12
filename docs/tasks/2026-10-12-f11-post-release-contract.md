# 合同预审：F11 发布后观察入口（依据 git HEAD `0edf0e6d7ea710b692c2ea11f7c3c24451e6c208`）

范围依据：#207；任务记录 `docs/tasks/2026-10-12-f11-post-release.md:5-21`；C3 §3/§4/§6/§9/§10/§12；F11 四步 `feature_list.json:108-119`。本次只读，未修改文件、未写 Git、未迁移、未注入故障。

## 九类结论

1. **时间**

   - 已定：发布观察必须固定发布起点、最短跟踪跨度、采样频率、健康窗口、缺口处理和绝对 deadline；暂停不延长 deadline；到期缺证据为 `unknown`（C3 §10，`docs/design/technical-proposal-2026-09-07.md:364-370`）。
   - 已定：产品观察使用 PostgreSQL 数据库时钟，领取、提交、清扫均调用 `_db_now()`/`clock_timestamp()`（`opspilot/observation/store.py:1495-1510,1608-1645,1833-1843`）。
   - 已定：现有 HealthProfile 数值为评估窗 300 秒、freshness 90 秒、查询超时 20 秒、deadline 3600 秒、40 次、60 秒频率、600 秒健康窗、流量门槛 0.008/s（`opspilot/observer/profiles/otel-demo-checkout.json:9-22`）。
   - 待决：F11 的 `released_at` 来源、最短跟踪跨度、是否复用上述四项、deadline 是否从 webhook 接收时间还是事件发生时间起算。

2. **计数与口径**

   - 已定：Observer 只在必要信号完整、有效流量和健康窗口满足时确认健康；低流量、缺测、陈旧或失败不能算健康（M1-02 合同，`docs/tasks/2026-10-03-m1-02-recovery-observation.md:121-123,130-133`）。
   - 已核实：kind/Prometheus 存在 KSM Deployment、ReplicaSet 指标；当前 `checkout` 有新旧 ReplicaSet，Prometheus 可区分 `pod-template-hash` 对应的 ReplicaSet ready 值。
   - 已核实：OTel 序列含 `service_version`、`service_instance_id`、`k8s_pod_name`、`k8s_deployment_name`，但没有 `k8s_replicaset_name`；现有 profile 查询按 service/workload 聚合，不按版本或实例过滤（`opspilot/observer/profiles/otel-demo-checkout.json:26-39,42-46`；Prometheus 只读 GET 实测）。
   - 待决：F11 的 reviewed traffic policy、before/after 窗口和 old/new instance 的确定性比较字段。

3. **状态转移**

   - 已定：发布状态为 `pending → observing → healthy | anomalous | unknown | cancelled`，终态不可由旧采样重开（C3，`docs/design/technical-proposal-2026-09-07.md:82-90,358-370`）。
   - 已定：重复投递去重；新发布版本建新记录；终态再次观察建 successor（C3 同上）。
   - 未确认：领域状态机已有 `ReleaseObservation`，但存储层没有对应表和转移实现；当前 `authorize_session*`、claim、submit、sweep 全部以 `incident_id` 为主体（`opspilot/domain/subjects.py:42-62,126-145`；`opspilot/observation/store.py:1018-1085,1467-1510,1608-1639`）。

4. **主体与权限**

   - 已定：Observer 是独立进程、独立 PostgreSQL 角色、独立 Prometheus 只读凭据，不调用模型或调查 worker（`opspilot/observer/__main__.py:1-25`；C3 §3）。
   - 已定：Kubernetes 产品面只允许 `get/list/watch/logs`，禁止写、`exec`、`rollout` 等（`opspilot/tools/registry.py:67-90`）。
   - 已核实：仓库没有 Kubernetes Python 客户端、K8s release watcher 或产品级 Kube credential；当前 kind RBAC 只有 kube-state-metrics ServiceAccount 的 `list/watch`，没有 OpsPilot Observer 的 K8s API 身份。
   - 待决：发布来源是否新增外部 webhook、K8s 只读身份，或两者都支持；两者同时支持时如何统一事件身份。

5. **数据合同、迁移、部署顺序**

   - 已定：当前迁移 head 是 `0012_generation_call_diagnostics`，下一迁移只能写“下一个号”（`opspilot/migrations/versions/0012_generation_call_diagnostics.py:1-5,29-30`）。
   - 已核实：0003 的 session 与 ending 都要求 `incident_id NOT NULL`，唯一活动会话按 incident 建索引；Observer grants 也只覆盖 incident lifecycle（`opspilot/migrations/versions/0003_observation_store.py:214-266,337-380`）。
   - 已定：迁移必须停止 web、worker、Observer 后执行，再启动并校验 head（`docs/development.md:30-35`；C3 §11）。
   - 待决：新增独立 `ReleaseObservation` 表并扩展 session subject，还是改成通用主体表；如何更新 Observer grants、0011 runtime grants 和约束触发器。

6. **旧数据与已归档证据兼容**

   - 已定：已有 `incident_recovery` session、sample、ending 的语义和重放不得改变；旧证据不能改写成 F11 证据。规则变化时仍授权的旧观察会话按 C3 规则结束，由人重新授权（`docs/development.md:40-41`）。
   - 已核实：当前无发布观察表、无发布观察归档证据、无 F11 acceptance projector。
   - 待决：迁移后的旧 session 是否保持原表原字段，及发布观察 evidence 是否采用新增 subject 标识。

7. **并发与水位**

   - 已定：每 session 最多一个活动采样任务；`FOR UPDATE SKIP LOCKED` 领取；提交原子校验 owner/epoch/lease、generation、顺序和窗口（`opspilot/observation/store.py:1467-1510,1608-1621`；C3 §10）。
   - 已定：暂停、人工控制和 Observer 采纳按主体控制代际串行化，旧结果只能保留 history。
   - 待决：发布 webhook 与集群探测同时到达时的唯一键、事件水位、旧/新发布交叠，以及同一 release 的并发异常采样如何只安排一次调查。

8. **外部调用**

   - 已定：Observer 不调用模型；每个信号通过 Prometheus 查询，失败/缺测进入可采纳但不健康的 unknown 路径（`opspilot/observer/__main__.py:1-10`；`docs/tasks/2026-10-03-m1-02-recovery-observation.md:137-143`）。
   - 已定：调查 Run 的既有上限仍是模型轮数、工具次数、超时和 24 小时期限（C3 §13，`docs/design/technical-proposal-2026-09-07.md:451-465`）。
   - 待决：release source webhook 的认证、大小上限、超时、重试和失败状态；K8s watch 是否属于 Observer 外部调用还是独立接收器。

9. **验收投影与 `passes`**

   - 已定：F11 `passes` 只有四步全部真实通过、有证据后才能翻转；当前为 `false`（`docs/tasks/2026-10-12-f11-post-release.md:10-21`；`feature_list.json:114-119`）。
   - 已定：产品不得写 CI check、deployment gate、rollout 或 traffic policy（`PRODUCT-CONSTRAINTS.md:6-16,20-24`）。
   - 待决：`anomalous` 到 F11 要求的 `degraded` 映射；“零发布门写入”需要哪些确定性代码断言、权限负向测试和真实实验投影。

## A：需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| A1 | 发布事件来源 | 集群 K8s/Prometheus 探测：自动但需新增 K8s 只读身份与 watcher；外部 webhook：复用 `event_token`，身份精确但依赖部署方投递；两者都要：覆盖广但需统一去重语义 | 外部部署 webhook，首版单一来源；沿用 `POST /intake/alertmanager` 的 event-token 通道模式（`opspilot/web/app.py:524-550`），集群探测另立合同 | 接收端、凭据、去重、实验 | 采纳推荐（用户 2026-10-12，r1）；未定项见任务记录「r1 未定项」 |
| A2 | 来源唯一发布 ID | webhook 外部 release ID；K8s Deployment UID+generation；ReplicaSet UID/hash | webhook 必须提供不可变 source release ID；K8s 兼容字段保留 Deployment UID + ReplicaSet UID/hash，不把 generation 单独当版本 | `ReleaseObservation` identity、唯一索引 | 采纳推荐（用户 2026-10-12，r1）；未定项见任务记录「r1 未定项」 |
| A3 | 不可变发布版本 | image digest；pod-template-hash；Deployment generation；完整模板规范化 hash | 记录 image digest + 完整 pod-template/config digest；hash/generation 仅作辅助证据。实测 Pod 有 image digest，但 OTel 没有 ReplicaSet 标签 | before/after revision、old/new evidence | 采纳推荐（用户 2026-10-12，r1） |
| A4 | 发布观察参数 | 复用现有 HealthProfile session 值；单独 F11 profile；固定常量写入合同 | 复用 profile 的 cadence/health window/deadline/max samples，新增并冻结 `released_at` 与 `min_tracking_seconds`；全部以 DB clock 采纳 | session schema、真实验收时长 | 采纳推荐（用户 2026-10-12，r1）；具体取值见任务记录「r1 未定项」 |
| A5 | before/after 与并发故障口径 | 全量服务流量；按版本/实例切片；无法切片即 unknown | Observer 只确定性判健康/退化；因果归因交给 Incident Run。无足够版本或流量证据时输出 `unknown`，不称为发布回归 | HealthProfile、证据投影、F11 第 2 步 | 采纳推荐（用户 2026-10-12，r1）；具体取值见任务记录「r1 未定项」 |
| A6 | 异常关联事故与 M1-04 规则 | 按 target/time 自动合并；按 release identity 独立事故；跨来源只建立关联 | release anomaly 使用独立幂等 key；不按相似时间自动合并 Alertmanager 事故，遵守 C3 §6 的跨来源默认关联规则；关闭事故按新事故处理（`docs/design/technical-proposal-2026-09-07.md:223-229,372`） | Incident、Run、重复调查 | 采纳推荐（用户 2026-10-12，r1） |
| A7 | 发布主体的数据模型 | 把 session 改成多态 subject；新增 `release_observations` 与 release session；复用 incident 表伪装 | 新增独立 `opspilot_release_observations`，session 显式 subject kind/id；禁止用空 Incident 代替发布主体 | 下一个迁移、旧数据兼容、replay | 采纳推荐（用户 2026-10-12，r1） |
| A8 | Observer 角色与来源权限 | Observer 直接持 K8s read-only credential；专用 release receiver 持 credential；外部 webhook 无 K8s credential | 首版 Observer 仍只持 PostgreSQL + Prometheus；webhook receiver 用 event token，不给产品 K8s 写权；若以后 K8s 探测，单独建只读身份和负向测试 | RBAC、0011 grants、网络隔离 | 采纳推荐（用户 2026-10-12，r1） |
| A9 | 发布 Run 主体类型 | 新增 `release` Run；异常后才创建 Incident Run；发布观察完全不建 Run | 推荐异常后创建 Incident Run，输入快照携带 release identity/before/after/old-new evidence；不新增 `release` Run，避免当前 `opspilot_runs.incident_id NOT NULL` 与结论权威冲突（`opspilot/migrations/versions/0001_baseline.py:64-71`） | worker、结论写入、Run schema | 采纳推荐（用户 2026-10-12，r1） |
| A10 | 工作台与结果映射 | 数据库直接改为 `degraded`；保留 C3 `anomalous`，页面映射为 `degraded`；两套状态并列 | 数据库保留 C3 `anomalous`，验收/UI 投影映射 `anomalous → degraded`，`unknown` 保持 `unknown`；提供列表、详情、暂停、恢复、取消 | 页面、API、F11 第 4 步 | 采纳推荐（用户 2026-10-12，r1） |
| A11 | 零发布门写入与 passes | 静态 grep；权限负向测试；真实运行投影；只做其中一项 | 三层证明：代码禁止写入接口/工具、K8s/外部权限负向测试、真实发布前后检查 CI/gate/rollout/traffic policy 未变；四步证据齐全才翻 `passes` | acceptance projector、feature_list、独立审查 | 采纳推荐（用户 2026-10-12，r1）；未定项见任务记录「r1 未定项」 |

## B：可逆默认

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B1 | webhook payload 规范化 | 直接保存原文；规范 JSON+hash；允许任意字段 | 规范 JSON、长度上限、原文 hash、未知字段拒绝或进入不可信审计上下文，沿用 Alertmanager 的脱敏/限长纪律（`docs/tasks/2026-10-10-m1-04-alert-intake.md:43-52`） | 审计、重放 | 可逆默认：复用已验证的 token、hash、限长模式，减少新认证面。 |
| B2 | 发布事件/采样锁顺序 | 先锁 release；先锁 target；事件接收与 session 分事务 | release identity → observation → session，采样继续沿用 incident-first 的既有顺序 | 死锁、重复任务 | 可逆默认：保持现有 `FOR UPDATE SKIP LOCKED` 与 DB clock 语义（`opspilot/observation/store.py:1495-1510`）。；未定项见任务记录「r1 未定项」 |
| B3 | 缺口与暂停 | 暂停补采样；自动延长 deadline；缺口直接 anomalous | 缺口只保留观察，暂停不补跨度、不延长 deadline；到期无证据为 unknown（C3 §10） | deadline、healthy window | 可逆默认：直接复用 M1-02 的 history-only/unknown 规则。 |
| B4 | 证据投影字段 | 只存结论；存完整 before/after 证据；由模型解释版本 | 存 source ID、target、image/config digest、Deployment/ReplicaSet identity、窗口、标签快照和 hash；模型只写叙述 | replay、报告输入 | 可逆默认：确定性字段先落库，模型只消费已提交证据。 |

## 建议实施拆步

1. **PR-1：发布事件合同与 `ReleaseObservation` 持久化。**  
   需迁移；使用下一个 Alembic 编号。新增独立发布主体、唯一身份、不可变版本、状态、policy 和事件审计；旧 0003 incident session 不改。先写独立合同测试，不翻 `passes`。

2. **PR-2：Observer 多主体与原子采纳。**  
   需迁移；扩展 session/sample/ending 的 subject 绑定、release 状态转移、幂等 anomaly ending、Observer grants、`RUNTIME_GRANTS` 和 replay。需验证旧 `incident_recovery` 数据重放兼容；不翻 `passes`。

3. **PR-3：发布来源接入与实验发布 harness。**  
   需真实 kind 环境运行。新增 event-token release webhook；工程 harness 必须改变应用镜像或配置而不重建 chart，并记录 before/after digest、ReplicaSet、Deployment generation 和 received time。现有 `scripts/kind_lab.py fault` 只改 `flagd-config` 的 `paymentFailure`，不能直接证明发布回归（`scripts/kind_lab.py:867-955`）。

4. **PR-4：before/after 证据与异常调查关联。**  
   需真实环境运行健康发布、带无关并发故障的发布、延迟回归；Observer 只负责确定性健康结论，异常时事务内幂等创建/关联 Incident，再创建 Incident Run；附 LangSmith trace 与冻结 `summary.json`。

5. **PR-5：工作台与验收投影。**  
   实现发布列表、详情、暂停、恢复、取消、healthy/degraded/unknown 投影及零发布门写入断言；独立 Agent 编写验收测试，四步真实证据齐全后才把 F11 `passes` 改为 `true`。

PR-1 的 schema/合同和 PR-2 的兼容性基础可以先合并，但都不能翻 F11 `passes`；PR-3 至 PR-5 需要用户裁决后的合同 r1 和真实实验环境证据。

## 风险与未确认项

- 当前代码的 `ObservationSession` 虽接受 `release_observation`，数据库 session/ending 仍强制 `incident_id`，这是实现前必须解决的真实结构缺口。
- 当前没有 K8s release watcher、K8s API credential 或 release webhook；只有通用只读 verb 白名单和 Alertmanager event-token 接口。
- 实测环境存在旧/新 ReplicaSet 和 image digest，但 OTel span metrics 没有 ReplicaSet 标签；按现有 profile 无法确定性完成 old/new instance 的流量切片。
- C3 状态名是 `anomalous`，F11 第四步要求输出 `degraded`；必须明确为存储状态与页面/验收投影的映射。
- kind 真实发布和延迟回归本次未执行，遵守“不得 apply、不得重启、不得注入故障”约束；当前只核对了只读 K8s/Prometheus 状态。
- `pytest --collect-only` 未能运行：工作区没有 `.venv`，系统 Python 也未安装 pytest；这是环境前提未满足，不是测试失败。
- 工作区原有 `SPEC.md` 修改和任务记录未提交文件保持原样，预审没有覆盖或改写。

