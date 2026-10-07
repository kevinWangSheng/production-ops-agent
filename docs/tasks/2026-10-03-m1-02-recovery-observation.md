# M1-02 独立恢复观察（F6）

- 状态：进行中（D1–D4 已决；门槛已合并 #73；第 0 步 kind 环境已执行并开 PR，与 M1 基础设施准备并行——2026-10-05 用户决定只对第 0 步放开先后顺序；第 1 步起仍排在基础设施准备之后）
- 更新日期：2026-10-06
- 依据：[feature_list.json](../../feature_list.json) F6；[PRODUCT-CONSTRAINTS](../../PRODUCT-CONSTRAINTS.md)「Recovery observations」；[C3](../design/technical-proposal-2026-09-07.md) §4「事故与发布观察分开建模」、§10「健康规则 / 观察主体与授权 / 采样与提交」、§13 观察预算；[ADR-0003](../adr/0003-business-state-recovery-authority.md)；ROADMAP「M1-01 剩余工作」行的下一步
- 工作区：门槛文档 `../production-ops-agent-m1-02-gate`，分支 `chore/m1-02-gate`；实施按子项另建 `feature/m1-02-*` worktree

## 目标与范围

人工在系统外处置事故后，在工作台登记处置，事故进入 `observing_recovery`。随后由确定性 Observer（不调用模型）按版本化 HealthProfile 采样，最终给出三种结果之一：

- 恢复：`resolved`
- 仍异常：保持 `observing_recovery` 有界继续采样，到期交接后回到 `open`
- 无法确认：交接给人

每个结果都能从已存的采样重建判定依据。

范围内：incident_recovery 一种观察主体；OTel Demo 实验环境上的一个 HealthProfile；F6 的 5 个验收步骤。

范围外：
- release_observation（F11，M2）
- 人工 close/reopen（M1-01 已决⑥移出）
- 复盘（F13）
- 任何恢复、回滚、发布门动作

## 前提与完成条件

- 前提：
  - SPEC 门槛 PR 合并（D1）。
  - M1 基础设施准备先完成（2026-10-05 用户决定）：[schema 迁移](2026-10-05-m1-prep-schema-migrations.md)、[trace 接入](2026-10-05-m1-prep-trace-langsmith.md)。
  - 计划 0 的 kind 环境就绪。旧 Compose 环境目录（本仓库 `tmp/m0-environment` 和 sibling worktree）都已不存在，`scripts/otel_demo_lab.py up` 当前无法启动；colima `m0-otel` VM 还在（Stopped）。
- 完成条件：
  - F6 五步在真实实验环境各至少执行一次，确定性断言通过，证据在 `docs/evidence/m1-02-*`。
  - 验收与合同测试由未参与实现的全新上下文 Agent 按 C3 §10 和 F6 步骤编写。
  - 各 PR 经独立审查。
  - 用户合并功能 PR 后，按证据决定是否翻 F6 `passes`。

## 已核查的现状

| 层 | 现状 | 证据 |
|---|---|---|
| 领域合同 | 已有纯函数层：`ObservationSession`、`HealthSample`、采纳判定 `evaluate_sample`（控制版本、generation、规则版本、水位、暂停、期限）、`confirms_health`（无 profile 或缺必要信号不算健康）；事故状态机 `open → observing_recovery → resolved/open`。M1-01 期间作为 F2 领域类型写成（`6efe24d`），未接入运行时 | `opspilot/domain/observation.py`、`opspilot/domain/subjects.py:24-40` |
| 持久化 | `opspilot_incidents.lifecycle` 列存在，但只写 `'open'`。没有观察会话表和采样表，也没有 HealthProfile 存储 | `opspilot/persistence.py:217,373,696,1626` |
| 采样数据源 | 产品只读 Prometheus/Jaeger profile（#54）。工程侧独立观察脚本（`scripts/otel_demo_observe.py`）只判调查验收前提，不是产品 Observer | `opspilot/tools/otel_demo.py` |
| 环境 | OTel Demo 2.0.2 跑在 colima + Docker Compose 上，**没有 Kubernetes**，没有 deployment 状态和 pod 健康信号 | `scripts/otel_demo_lab.py:1-20`；本机无 kind/k3d/kubectl |
| 上游 | HolmesGPT main `9e21560`（2026-10-04 核对）：只有 `holmes/checks/`，check 无状态、由 LLM 判 pass/fail/error、无观察窗口、`schedule` 字段注释为未来实现；没有「处置后观察」。可借鉴 pass/fail/error 三态输出；判定须按 C3 确定性实现，不照搬 LLM 判定 | [`holmes/checks/models.py:11-45` @ `9e21560`](https://github.com/HolmesGPT/holmesgpt/blob/9e21560c7ad1d02fdac29c9d9979999992bc3edb/holmes/checks/models.py#L11-L45) |

## 计划（每项一个 PR，按序合并，不做 stacked PR）

0. **kind 实验环境**
   - colima 上起 kind，装固定版本的 OTel Demo 官方 Helm chart 和 kube-state-metrics，Prometheus 抓取 deployment/pod 指标。
   - 工程脚本提供 up/health/stop/fault 四个动作，实验环境写权限只在工程侧。
   - 回归 M1-01 的只读工具 profile（服务标签、Jaeger 地址），跑一次真实故障调查确认没有退化。
1. **HealthProfile 与采样合同**
   - 版本化 profile 文件：必要信号、PromQL、最低样本与有效流量门槛、阈值、新鲜度、持续窗口、观察期限与次数。
   - 用 OTel Demo checkout 及其依赖写一份 profile。
   - 数值先按实验环境基线校准，在任务记录写明来源，候选评测前冻结（C3 §13）。
2. **PG 观察会话与原子采纳**
   - 建观察会话表和采样表。
   - 采纳、水位推进、生命周期转换、安排下一次采样在同一事务里完成，复用 `evaluate_sample`。
   - 观察表、采样表与 Observer 专用 PG 角色及最小授权（D3）都以 Alembic 增量迁移落地；迁移只建 `NOLOGIN` 角色与授权，Observer 的登录凭据在仓库外设置（PG 17.9 实测 `CREATE ROLE` 可在事务内执行，见 #75 thread）。
   - 每次采样保存查询、时间窗、来源、每个必要信号的实际返回值（原始结果经现有证据登记，带 hash）及判定，重放只读这些存储，不再查遥测（F6 第 5 步）。
3. **人工登记处置 → 开始观察**
   - 工作台动作，带 `expected_version`、幂等键和操作者审计。
   - 递增 `control_generation`，授权新观察会话。
   - 暂停、接管、取消在同一事务里撤销观察授权（C3 §10）。
4. **Observer 采样任务**
   - 独立 Observer 进程里的确定性任务，直接以自己的只读凭据查 Prometheus，不调用模型，不经调查 worker 或其工具网关（D3）。
   - 健康窗口满足 → `resolved`。
   - 缺测、陈旧、低流量 → unknown，按原期限有界继续，到期交接，事故回到 `open`。
   - 持续异常 → 保持 `observing_recovery` 有界继续，到期交接，事故回到 `open`（C3 §10）。
   - 每个会话最多一个活动采样任务，租约重试保持原逻辑序号；全局/目标暂停同样挡住 Observer 采样（C3 §4）。
   - 角色隔离（C3 §3，D3）：Observer 用独立 PG 角色，只授予观察会话/采样表读写和事故生命周期转换所需权限，不能写调查 Run、报告或证据结论；Prometheus 只读凭据与调查侧分开发放；网络上只配置 Prometheus 与 PG 端点。调查侧凭据与 DB 角色不能用于采纳采样，Observer 凭据不能调调查工具，两条各写负向测试。
   - 调查侧（Controller/Worker/Gateway）的 C3 §3 隔离不在本切片，沿用 M1-01 现状。
5. **工作台展示与重放**
   - 事故页把调查结论和恢复判定分开展示，恢复判定附采样依据。
   - 离线重放脚本只用已存采样重算判定，结果须与已存结果一致（F6 第 5 步）。
6. **F6 验收（真实环境）**
   - 四个场景：处置后恢复、撤流量、去掉必要遥测、持续异常；再加一次重放。
   - 每个场景记录 ledger 和独立观察结果。
   - 本项不调用模型，费用只有实验环境资源。

## 已决（2026-10-03 用户）

- **D1 实施门槛**：SPEC 原先只为 M1-01 开放实施，M0 遗留项是「更大 M1 范围」的入口条件，和 ROADMAP 的下一步冲突。用户决定**有界开放 M1-02**：只开放本记录范围；observer 授权在本切片内交付；其余 M0 遗留项仍挡候选评测和更大范围。SPEC 已加一段，随门槛 PR 合入。
- **D2 实验环境**：F6 第 1 步要求 deployment 状态和 pod 健康，Compose 给不出。用户决定**迁到 kind + OTel Demo Helm + kube-state-metrics**，产品仍只经只读 Prometheus 读取，不新增连接器。
- **D3 Observer 角色隔离**（2026-10-05 用户，起因：#73 机器人 P1 引用 C3 §3「隔离必须落实到凭据、数据库权限和网络可达性」）：原计划让 Observer 与调查共用 worker 进程和网关，属未经批准的合同偏离。用户决定**只隔离 Observer**：独立进程、独立 PG 角色、独立 Prometheus 只读凭据，见计划第 2、4 项；调查侧隔离不在本切片。网络可达性在本地实验环境能做到哪一层，以第 4 项的实测证据为准，做不到的如实列为限制。

- **D4 实验环境规格**（2026-10-05 用户）：本机运行精简版——Homebrew 安装 kind、kubectl、helm（第 0 步执行时安装），colima 约 8 GiB，关闭 checkout 故障路径用不到的 OTel Demo 服务；运行期间不同时跑其他重负载。依据：本机 16 GB 内存、10 核、磁盘余 34 GB；旧 Compose VM `m0-otel` 为 6 GiB。精简后的服务清单与实测内存写进第 0 步证据；若精简后仍无法稳定运行，停在决策边界报告，不擅自改用远程环境。

## 第 0 步执行（2026-10-05/06，#82，分支 `chore/m1-02-kind-lab`，worktree `../production-ops-agent-kind`）

证据：[docs/evidence/m1-02-lab/run.md](../evidence/m1-02-lab/run.md)（版本、服务清单、内存、回归 Run、启停）。产品代码零改动。

- 版本：colima 0.10.1 profile `m1-kind`（4 CPU / 8 GiB / 30 GiB，`m0-otel`/`default` 保留）；kind v0.33.0 节点 `kindest/node:v1.37.0`；kubectl v1.37.1、helm v4.3.0（Homebrew，D4 第 0 步安装）；chart `opentelemetry-demo` 0.37.8（appVersion 2.0.2，与 M0 一致）+ `kube-state-metrics` 8.6.0（v2.20.0）。
- 服务清单（精简，`scripts/kind_lab/values.yaml`）：关闭 accounting、fraud-detection、kafka（checkout 跳过 producer）、grafana、opensearch、flagd-ui；保留 20 个 Deployment（frontend-proxy、frontend、checkout、cart、valkey-cart、payment、product-catalog、currency、shipping、quote、email、ad、recommendation、image-provider、flagd、load-generator 2 用户、otel-collector、jaeger、prometheus、kube-state-metrics）。
- 实测内存：容器 working set 合计 1.29 GiB；kind 节点 2.5–2.7 GiB；VM 进程在宿主 RSS 1.2 GiB；运行期间无 pod 重启。宿主 16 GB 全程紧张（并行 agent），但本环境稳定，未触及 D4「无法稳定运行」边界。
- K8s 状态信号：Prometheus 经默认 `kubernetes-service-endpoints` 抓到 kube-state-metrics（`kube_deployment_status_replicas_available`、`kube_pod_status_phase`、`kube_pod_container_status_restarts_total`）；可供 #92 的 RBAC 取证使用（本步未做）。
- 回归：M1-01 `otel-demo` profile **不改地址、不改服务标签**（NodePort 发布到 127.0.0.1:19090 / 16686 / 18080，与默认值一致；Jaeger 子 chart 的 query Service 为 headless，加了一个工程侧 NodePort Service）。三次真实 Run 全部 `published`：normal-1（17 工具，窗口含环境启动期 cart 连接拒绝，报告如实报出）、fault-1（20 工具，定位 checkout→payment `Charge` Invalid token，独立观察先行确认 3/6 trace）、normal-2（31 工具，干净窗口无错误）。费用 ≤ 0.57 CNY（余额差上界）。
- 启停：`.venv/bin/python scripts/kind_lab.py up|health|stop|fault inject|restore --experiment-id <id>`；`up` 先查宿主可回收内存 ≥ 3 GiB；`stop` 只停 VM，集群与 release 保留，再 `up` 约 90 s 恢复且 Prometheus 历史保留。故障钩子 patch `flagd-config` ConfigMap，指标可见约 3 分钟（kubelet 同步），恢复同样。
- 已修的两个坑：kind 把宿主 `HTTP_PROXY=127.0.0.1:1087` 写进节点导致所有镜像拉取失败（脚本对 kind 剥离代理变量）；chart 的 opensearch exporter 不能用 `null` 删除（保留定义、移出 logs pipeline）。
- 自行决定并记录理由：Helm chart 版本取 appVersion 2.0.2 的最新 patch（0.37.8）以保持与 M1-01 的服务名/指标一致；kube-state-metrics 单独安装而非经 prometheus 子 chart，便于独立锁版本；Jaeger NodePort 用独立 Service 而非改产品默认地址。

## 待决

- 无。HealthProfile 的具体数值属于可逆技术细节，由实施 Agent 校准并记录来源，候选评测前冻结。

## 下一步与交接

- 门槛 PR 已合并（#73）；子项 issue 见 #82–#88。
- 上游核对已完成（见上表）；计划 0 已执行（PR 见 #82），M1 基础设施准备完成后从计划 1 开工。
- 当前没有运行中的服务或进程：colima `m1-kind` 已停（集群与 release 保留，`kind_lab.py up` 即可恢复）；回归用一次性 PG 55481 已停并位于会话临时目录。
