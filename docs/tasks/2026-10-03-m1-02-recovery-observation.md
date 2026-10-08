# M1-02 独立恢复观察（F6）

- 状态：进行中（D1–D4 已决；门槛已合并 #73；第 0–2 步已合并 #111 #113 #114；F6 验收与合同测试已合并 #112；下一步第 3 步 #85 与第 4 步 #86 并行）
- 更新日期：2026-10-07
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

## 计划（每项一个 PR，不做 stacked PR；无依赖的步骤可并行开发，各自基于 main，前置 PR 先合）

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

## 接口约定（第 1/2 步并行）

第 1 步（HealthProfile，分支 feature/m1-02-health-profile）与第 2 步（观察会话/采样持久化，分支 feature/m1-02-observation-store）同时从 main 0c97377 开发，互不 import 对方新代码。第 4 步（Observer 进程）负责把两边接起来。双方只按本约定对齐。

1. **profile 版本标识**：`health_profile_revision: str`，不透明字符串。第 1 步定义生成规则（建议 `<profile_id>@<规范化内容 sha256 前 12 位>`，内容变则 revision 变）。第 2 步只存储、只做相等比较，不解析。领域层已有字段：`opspilot/domain/observation.py` 的 `ObservationSession.health_profile_revision` / `HealthSample.health_profile_revision`。
2. **信号名**：`signal_name: str`，集合由 profile 定义。第 2 步不硬编码任何信号名。
3. **单信号读数**（一次采样包含多条）。字段名与类型双方一致：
   - `signal_name: str`
   - `status: "ok" | "no_data" | "stale" | "timeout" | "failed"`
   - `value: float | None`（status 非 ok 时为 None）
   - `sample_count: int | None`
   - `query: str`（实际执行的 PromQL）
   - `window_start` / `window_end`：带时区时间
   - `source: str`（数据源标识，如 `prometheus`）
   - `raw_sha256: str | None`（原始返回的 sha256）
   第 1 步在自己的模块里定义这个 DTO；第 2 步在持久化层定义同名同型的输入类型与表列。
4. **一次采样的判定**：第 1 步提供纯函数，输入 profile + 读数列表，输出 `outcome`（复用现有 `SampleOutcome`：healthy/degraded/no_data/stale/timeout/failed）、`required_signals_present: bool` 和逐信号判定理由。必要信号缺失或非 ok 时绝不能输出 healthy；流量低于有效流量门槛不能输出 healthy。第 2 步不做阈值判断，只接收现有 `HealthSample`（outcome、required_signals_present）加读数行。
5. **会话参数在授权时固定**：观察绝对期限 `deadline_at`、最大采样次数 `max_samples`、采样间隔 `sample_interval_seconds`、持续健康窗口 `sustained_window_seconds` 由调用方在创建会话时传入并存在会话行上；第 2 步不读 profile 文件。第 1 步的 profile 要能给出这四个值。
6. **持续健康窗口语义**（第 2 步在采纳事务内计算）：已采纳样本中，`confirms_health` 为真的连续样本，窗口首尾覆盖时长 ≥ `sustained_window_seconds` 才触发 `recovery_confirmed`（observing_recovery → resolved，会话 completed）。任何已采纳的非健康样本（含 no_data/stale/失败）重置连续计数。期限或次数耗尽且未确认 → `observation_ended_unconfirmed`（→ open，会话 expired），记交接原因。
7. **迁移**：只有第 2 步新增 Alembic 迁移（0003）。第 1 步不改 schema。
8. **范围边界**：人工「登记处置」动作（open → observing_recovery、递增 control_generation、撤销授权）属第 3 步；Observer 进程、调度、Prometheus 凭据属第 4 步。第 2 步可提供「创建已授权会话」的存储原语供测试与第 3 步复用，但不做人工动作语义。

## 第 1 步执行（2026-10-07，#83，分支 `feature/m1-02-health-profile`，worktree `../production-ops-agent-health-profile`）

- 目标：定义「怎样才算恢复」——版本化 HealthProfile 文件格式、加载/校验、checkout profile、一次采样的纯函数判定与单元测试。不改 schema，不写 Observer 进程。
- 交付：`opspilot/observer/health_profile.py`（`HealthProfile`/`HealthSignal`/`SignalReading`/`evaluate_readings`/`profile_revision`/`load_health_profile`）、`opspilot/observer/profiles/otel-demo-checkout.json`（8 个必要信号：deployment 可用副本、PlaceOrder 请求率、错误率、p95 延迟、pod Running、窗内重启、依赖 Deployment 可用、依赖调用错误率；K8s 信号来自 kube-state-metrics）、`tests/test_m1_health_profile.py`。
- 判定规则（按序，PR #113 独立审查后修订）：(a) 任一「已判定」的必要信号越界 → degraded——读数可用（ok、点数足、query/source 一致、新鲜）且（信号不依赖流量 `traffic_dependent=false`，或流量已确认过门槛）才算判定，所以 0 副本在无流量时仍是 degraded；(b) 否则有必要信号读数不可用 → unknown（failed > timeout > stale > no_data，`required_signals_present=false`）；(c) 否则流量低于门槛 → no_data（错误率为 0 也不行，F6 第 2 步；依赖流量的比率/分位信号在低流量或流量未知时标 `not_judged`）；(d) 否则 healthy。可选信号只记录不影响结果。新鲜度：`evaluate_readings` 多一个输入 `sample_time`（采样时刻，带时区）；读数 `window_end` 早于采样时刻超过 `freshness_seconds`、或窗口跨度与 `evaluation_window_seconds` 相差超过 5%（采样器按 Prometheus step 对齐窗口边界的容差）→ 该信号按 stale。**第 2 步需存采样时刻**（会话/采样行本来就有），重放用存下的 `sample_time`；读数字段不变，接口约定不改。 `sample_count` 语义（供第 4 步实现）：窗内原始样本数，Observer 对每个信号额外执行 profile 的 `coverage_query`（`count_over_time` 同选择器同范围）取整填入；读数只存 `query`，`coverage_query` 由 revision 对应的 profile 恢复。依赖可用性改用 `count(... >= 1)` 并要求等于 8（`min` 会忽略缺失序列，@codex review P1）；`format_version` 校验失败不再回显文件值（P2）。revision = `<profile_id>@<规范化 JSON sha256 前 12 位>`，内容变即变、格式与键序不变则不变。
- 自行决定并记录理由：文件格式用 JSON（仓库无 YAML 依赖，不加第三方包）；模块放 `opspilot/observer/` 作为第 4 步 Observer 进程的归属包；unknown 优先于 degraded（流量无意义时不对任何比率下断言）；读数 `query`/`source` 必须与 profile 完全一致，否则按 failed 处理（保证第 5 步重放用的是存下来的查询）。
- 数值校准：[docs/evidence/m1-02-health-profile/run.md](../evidence/m1-02-health-profile/run.md)。kind 启动中止（宿主 swap 2 分钟内 8.9 → 10.9 GB / 11.3 GB），未新采集；数值按第 0 步同一环境 2026-10-06 的四个独立观察窗校准（稳态 3.75 单 / 5 min → 流量门槛 0.008 /s；p95 37.5–182.5 ms → 上限 500 ms；错误率正常窗 0 / 故障窗 0.52 → 上限 0.01）。`minimum_samples` 全部 3：按 Prometheus 默认 1 m 抓取 / 60 s flush 估计 300 s 窗 5 个样本取一半，抓取间隔待实验环境实测确认（run.md）。当前 revision 见 PR #113 最新提交（`load_health_profile(...).revision`），候选评测前冻结。
- 状态：已合并（#113，2026-10-07）。
- 待决：无（数值属可逆技术细节）。限制：稳态流量薄（2 locust 用户），比率分辨力有限；要更稳定的 F6 验收可在实验环境加用户数（第 0 步 values，不在本步）。

## 待决

- 无。HealthProfile 的具体数值属于可逆技术细节，由实施 Agent 校准并记录来源，候选评测前冻结。

## 下一步与交接

- 门槛 PR 已合并（#73）；子项 issue 见 #82–#88。
- 上游核对已完成（见上表）；第 0–2 步已合并。第 3 步（#85）与第 4 步（#86）互不依赖，可并行，各自基于 main；第 5 步（#87）、第 6 步（#88）在其后。第 4 步合并前须完成 #86 中转入的要求（新鲜度基于底层样本时间戳等）。
- 当前没有运行中的服务或进程：colima `m1-kind` 已停（集群与 release 保留，`kind_lab.py up` 即可恢复）；回归用一次性 PG 55481 已停并位于会话临时目录。

## 第 2 步执行（2026-10-07，观察会话与原子采纳，#84）

- 工作区：`../production-ops-agent-obs-store`，分支 `feature/m1-02-observation-store`，基于 main `0c97377`。与第 1 步并行，只按「接口约定」对齐，未 import 对方代码。
- 迁移 `0003_observation_store`（downgrade 完整撤销，含条件删角色）：三张表 + `opspilot_incidents.observation_generation`；表结构、CHECK 与角色授权见 [development.md](../development.md#数据库迁移alembic)。会话行同时承载唯一的活动采样任务（`active_sample_*` 列），「每个会话最多一个活动采样任务」由结构保证；租约重试保持 `job_id` 与逻辑序号、只递增 epoch。
- 代码：`opspilot/observation/store.py` 的 `ObservationStore`（继承 `persistence.base._StoreBase`，共用连接池与超时）。`authorize_session[_in]`（Controller 侧存储原语：锁事故行、`open → observing_recovery`、递增 `observation_generation`、建会话与第一个任务；人工动作语义留给第 3 步）、`revoke_sessions[_in]`、`claim_due_samples`（`FOR UPDATE SKIP LOCKED`，全局/目标挂起只跳过不租）、`submit_sample`、`sweep_expired_sessions`、`session_history`、`replay_session`。
- `submit_sample` 事务内顺序：锁事故行 → 锁会话行 → 读控制范围（不加锁；挂起路径会锁事故行，事故行锁即排序点，且 Observer 角色没有控制表的 UPDATE 权限、无法 `FOR SHARE`）→ 租约校验（job/owner/epoch/到期，失败即 `lease_revoked` 只作历史）→ `evaluate_sample`（事故当前控制代际与观察代际作为会话代际，旧样本判 stale）→ `adopt_sample` 推进水位 → `confirms_health` 与持续窗口折叠（任何非健康已采纳样本重置连续计数）→ 会话状态机 → 事故生命周期（`recovery_confirmed` 非法则整笔回滚；`observation_ended_unconfirmed` 对已是 `open` 的事故不改生命周期）→ 安排下一任务或清空任务槽 → 写采样行（含判定与判定时的条件：生命周期、挂起、期限、租约、两种代际）→ 写读数行。被拒样本不改水位、不改生命周期、不排新任务，只释放租约让同一任务重试。
- 领域层唯一改动：`SampleReason` 增加 `lease_revoked`（C3 §10 把 owner/epoch/lease 列为提交时原子校验项，`evaluate_sample` 的输入里没有租约，由存储层判定）。`evaluate_sample`/`adopt_sample`/`confirms_health` 未改。
- 位置说明（待用户决定）：模块放在 `opspilot/observation/` 而非 `opspilot/persistence/`，因为 `tests/test_architecture.py` 的 strict xfail 把「持久化层是否建立在 domain 状态机之上」记为未决（ADR-0007 明示另决）；本模块直接建立在 domain 之上，放进 `persistence/` 会让该 xfail 变成 XPASS 失败。是否把它并入 `persistence/` 并撤掉 xfail，属架构决定。
- 验证（PostgreSQL 17.9 Homebrew 临时实例，端口 55471，数据目录在 scratchpad，`OPSPILOT_LAB_DSN` 指向，未碰 55431 lab）：空库 `make migrate` → `schema upgraded: 0003_observation_store`；`downgrade 0002` 后 15 张表、列与角色均消失、无残留授权；再 `migrate` 后 dump 与 fresh head 一致。`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_STEP_POSTGRES=1 pytest tests/integration`：325 passed、10 skipped（仅既有 opt-in/重启 lab 用例）。新 PG 测试 `tests/integration/test_m1_02_observation_store_postgres.py`（19 例）：Observer 登录角色跑通 claim/submit/sweep/replay，同时 20 条越权语句（写 runs/steps/evidence/controls/inputs、改事故 `conclusion`/`control_generation`/`state`、读 `conclusion`、改会话参数、插会话、删改采样）全部 `InsufficientPrivilege`；持续健康窗口 → `resolved`；no_data/stale/failed/缺必要信号可采纳但不确认且重置连续计数；无 profile revision 永不确认；profile/观察代际/控制代际过期只作历史；丢失租约的样本 `lease_revoked`、重领同序号 epoch+1；目标/全局挂起挡住 claim 与采纳；期限到（提交时与 sweep 两条路径）与次数耗尽 → 会话 expired、事故回 `open`；两线程同一租约并发提交只一个推进水位；篡改已存判定后 replay 报不一致。`make check` 结果见 PR。
- 独立审查处置（2026-10-07，PR #114，全部采纳）：
  - P1-1 暂停/恢复：租约记录领取时的 global/target scope generation，提交时不等 → `suspended` 只作历史（与 Run 的 `_lease_revoked` 同一规则）；健康连续期在「已采纳样本的 window.start 晚于上一个已采纳 window_end（有间隙）」或「连续期起点的 scope generation 与当前不同」时重新起算，会话行记 `healthy_since_{global,target}_generation`，采样行记当时的两种 generation 供重放。
  - P1-2 处置前数据：会话行记 `authorized_at`；window.start 早于它的样本可采纳（水位照常推进、留底）但 `confirms_health=false`，采样行 `health_basis='window_before_authorization'`（其余取值 confirmed / not_adopted / no_health_profile / outcome_not_healthy / required_signals_missing）。按 profile 形参（窗口 300 s、持续 600 s、间隔 60 s）的测试：授权后 60k 秒取样、窗口 [t-300, t]，k=1..4 只作观察，k=5 起健康连续期从授权时刻起算，k=10（授权后 600 s，窗口 [T+300, T+600]）确认——即连续健康覆盖 ≥ 600 s 的时刻，不是审查意见写的「window+sustained = 900 s」。lead 裁决（2026-10-07）：按此语义——持续健康窗口 = 授权后连续已采纳健康样本的覆盖跨度（首个样本的 window.start 到最新样本的 window.end）≥ `sustained_window_seconds`；[T, T+300] 已全是整改后数据，不另加一个窗口长度。
  - P2-1 被拒样本：可恢复原因（profile revision 不符、挂起、窗口倒退）释放租约并把 `active_sample_due_at` 推迟一个采样间隔；不可恢复原因（控制代际/观察代际过期、事故状态不可采纳）直接结束会话（state `revoked`、`ended_reason='binding_stale'`），不再排任务，生命周期不动。
  - P2-2 生命周期护栏：触发器只允许 Observer 角色（`current_user` 为该角色或其直接成员；嵌套成员不识别，部署时登录角色须直接 `IN ROLE`）做 `observing_recovery → resolved | open`，其余 `insufficient_privilege`；代码只用这两条边。
  - 原始返回：读数表加 `raw bytea`（CHECK ≤ 131072 字节且必须带 `raw_sha256`；取 M0 的 128 KiB 响应上限）；`SignalReading.raw` 给出时自动算 sha256、与给定值不符拒绝；`replay_session` 对存有 raw 的读数重算 sha256，并比对折叠出的会话终态与已存终态（sweep/撤销导致的结束按「无样本结束」接受）、已存判定蕴含的事故生命周期（resolved / open）与事故当前生命周期。
  - profile 内容留底（PR #113 复验补充）：新表 `opspilot_health_profiles`（主键 revision、profile_id、content_sha256、content text）；用 text 而不是 jsonb 存，因为 jsonb 会重排键、改写数字格式，哈希只能对原文算。`authorize_session[_in]` 新增 `health_profile`（规范化 JSON 文本），给了 revision 必须给内容，校验 `revision == <profile_id>@<sha256(content)[:12]>` 且已存同 revision 的完整 sha256 一致，否则 `HEALTH_PROFILE_REVISION_MISMATCH`，不写任何东西；按 revision 去重（一张表而非会话列：同一 revision 多次授权只存一份，重放按 revision 取回）。会话 `health_profile_revision` 外键指向它；`session_history` 带回内容；Observer 角色只读。
  - 复验第二轮（2026-10-07，全部采纳）：P2-A 提交时 `window.end > DB now + 30 s` 按 INVALID_INPUT 拒绝、不入判定（30 s：远大于 NTP 时钟偏差、远小于任何采样间隔；Observer 用自己的 now 截窗，不能因小偏差拒掉全部样本，但未来的窗口没有遥测可覆盖）；暂停区间不计入覆盖：`opspilot_scope_controls` 没有时间列，取 `opspilot_suspension_audit` 中「当前 generation 对应行」的 `created_at`（global 行 target_id 为 NULL），只看授权之后的变化（之前的数据已由 `window_before_authorization` 排除），存在采样行 `scope_changed_at`；窗口起点早于它的样本可采纳不确认，`health_basis='window_before_scope_change'`。Observer 角色为此增加 `opspilot_suspension_audit` 只读。P2-B 提交时样本携带的 sequence / subject_control_generation / observation_generation / health_profile_revision 必须等于租约印记，否则 INVALID_INPUT、不入库、不结束会话；`binding_stale` 只在印记与租约一致而事故侧已变化时发生。P2-C `replay_session` 不再比较事故当前生命周期，改为与会话最后一条样本记录的生命周期（其 `subject_lifecycle` 施加其 `transition`）比较；sweep 结束不再推导 expected lifecycle；旧会话过期→新会话授权、resolved 后人工 close 都不再误报。P2-2 触发器改用 `pg_has_role(current_user, 'opspilot_observer', 'MEMBER')` 并排除 superuser 与表 owner（两者 pg_has_role 恒真），嵌套成员的登录角色同样被拦。
  - @codex review 分诊（2026-10-07，lead 定、全部采纳）：① 暂停不再推迟重试：会话授权时记 `authorized_{global,target}_generation`，领取或提交时发现 scope generation 已变（无论当前是否仍暂停）→ 会话 `revoked`、`ended_reason='scope_suspended'`、不再排任务、生命周期不动，解除暂停后旧授权不会被自动续用（C3 §4），第 3 步重新授权；随之删掉了 `window_before_scope_change`/`scope_changed_at`/连续期 generation 列与 `opspilot_suspension_audit` 授权（同值审计行问题随之消失）。② 生命周期变更绑定已提交的观察判定：新表 `opspilot_observation_endings`（append-only，Observer 只能 INSERT）记录每次会话结束（原因、触发的 transition、决定它的 sample_id），`DEFERRABLE INITIALLY DEFERRED` 约束触发器在提交时要求 Observer 角色对 lifecycle 的改动在同一事务内有本事故会话的结束记录且 transition 与边一致，`resolved` 还须指向同事务写入的 `transition='recovery_confirmed'` 的已采纳采样行；裸 `UPDATE` 在 COMMIT 时 `insufficient_privilege`；sweep 同样先写结束记录。③ claim 的暂停过滤移进 SQL（当前挂起的目标/全局不占 LIMIT）。④ 重放的生命周期记录取携带终态 transition 的那条采样，迟到的 history_only 重复提交不再干扰。⑤ 结构校验：重放按已存 profile 内容的 `signals[].name`（`required` 默认 true）取必要信号，healthy 样本每个必要信号都须有 status=ok 的读数行，`required_signals_present` 须与读数行一致，否则报 `signal_mismatches`；按阈值重算属第 5 步 #87。
  - 合并前复验（2026-10-07）：claim 结束会话前先 `FOR UPDATE SKIP LOCKED` 拿事故行，拿不到就留给下次（所有结束路径统一事故行先锁，修掉会话→事故的反向锁序）；`required_signals` 改为 fail-closed（解析失败/无 signals/清单为空 → `HEALTH_PROFILE_UNREADABLE`，重放对每条样本报 `health_profile_unreadable`），并用与第 1 步同形的 profile JSON 固定解析；证据触发器加校验结束记录的会话属于被改的事故；采样行 `submitted_at` 与结束记录 `recorded_at` 不授 Observer INSERT（只能由 DEFAULT 取数据库时钟）。已知限制（见 development.md）：触发器防 Observer 的 bug，不防被攻陷的 Observer 凭据；暂停期间授权的会话恢复后首次触碰即结束。
  - 第二轮 @codex（2026-10-07）：有效租约下样本印记（sequence / 两种 generation / profile revision）与租约不符不再抛 INVALID_INPUT，改为留底 `history_only`、新 reason `lease_stamp_mismatch`（domain `SampleReason`、迁移 CHECK、采样行 `lease_stamps_match` 列同步），读数照存、不推进水位、不改生命周期、不结束会话，释放租约并推迟一个间隔；session_id 不符与窗口在未来仍按 INVALID_INPUT 拒绝不入库（前者无处可归档，后者是非法输入而非身份/水位问题）。
  - 漏看三条（2026-10-07）：① 提交时按已存 profile 的必要信号校验读数行（复用 `required_signals`，fail-closed）：样本自称 healthy / required_signals_present 但读数行不支持（缺 ok 行、或标志与行不符、或 profile 读不出清单）→ `history_only`、新 reason `readings_inconsistent`（Literal/CHECK/采样行 `readings_consistent` 列同步），不推进水位、不确认、不结束会话，释放租约推迟一个间隔；重放用同一函数从已存读数行重算，删读数行会同时改变重放判定。② 重放校验结束记录：会话 authorized 时不得有结束记录；已结束时最后一条记录的 ended_reason 须等于会话的、须蕴含会话状态、transition 须与原因相符（确认带 recovery_confirmed 并指向确认采样；未确认结束带 observation_ended_unconfirmed 或空；撤销类为空），由已采纳样本判定结束的还须指向该样本；缺记录或不符即不一致，不再无条件接受 authorized→expired/revoked。③ 两个触发器函数 `SET search_path = pg_catalog, pg_temp`，`pg_has_role`/`pg_roles`/`pg_class`/`format`/`transaction_timestamp` 全部 `pg_catalog.` 限定，业务表 `public.` 限定；负向测试：Observer 建临时 `pg_roles`/`pg_class` 冒充 superuser/owner 后改 closed 仍被拒、裸改 open 仍在 COMMIT 被拒。
  - 领取与暂停串行化（2026-10-07，机器人 P1）：claim 对每个候选会话先 `FOR NO KEY UPDATE SKIP LOCKED` 拿事故行（Observer 有 lifecycle 列 UPDATE，足以加锁），拿不到就跳过留给下次；持锁后重读 scope generation，与授权时不同或当前挂起 → 按既有规则结束会话，否则才发租约。已提交的暂停一定被看到；在领取之后提交的暂停要等 claim 提交，随后在 submit 以 `suspended` 使租约失效。Observer 发起查询前的校验属第 4 步（#86）。
  - 读数覆盖（2026-10-07，复验 P2）：读数表 CHECK 改为 `(status='ok') = (value IS NOT NULL)`，`SignalReading` 同步；结构校验里 ok 但 value 为空、或 sample_count 为空/0 的读数不算覆盖必要信号，提交走 `readings_inconsistent`，重放同一判定。
  - 最后一轮机器人修复（2026-10-07，停机规则生效，此后只修能引用 C3/PRODUCT-CONSTRAINTS 原文的 P1）：① 读数窗口须属于本次采样：`_covers` 要求读数 window_start/end 与样本窗口之差 ≤ 样本窗口长度的 5%（Prometheus 范围查询按 step 对齐边界，与第 1 步一致），否则不算覆盖 → `readings_inconsistent`，重放同判。② 证据触发器按事务身份绑定：结束记录与确认采样行的 `xmin = pg_current_xact_id()::xid`，去掉时间比较；另一连接后写并提交的记录不再能被借用（测试复现）。③ 授权校验完整 Target：注册表只有 resource_uid，其余字段由该目标第一个会话固定，后续授权的 Target 任一字段不同 → `TARGET_MISMATCH`、不写任何东西；**限制**：第一个会话的非 resource_uid 字段无处可核对（`opspilot_targets` 是基线表，加列属另一决定）。④ 结束记录新增 `lifecycle_before` / `lifecycle_after`；期限/次数结束一律记 `observation_ended_unconfirmed`（事故已是 open 时 before=after=open），触发器也要求记录的前后值等于本次改动；重放只接受精确 transition，并校验 after == fire(before, transition)（非法时 after == before）。⑤ 重放与提交读取 profile 时重算 sha256，须等于 `content_sha256` 且与 revision 后缀一致，否则 fail-closed（`health_profile_unreadable` / `readings_inconsistent`）。
  - 第 4 步须知：`within_deadline` 用提交时的数据库时钟 `now < deadline_at`，不看样本窗口。
- 未执行：真实实验环境采样（第 4 步）。本机 55431 lab 库已于 2026-10-07 升级到 0003（`make migrate` 需设 `OPSPILOT_DSN` 与 `OPSPILOT_PG_DUMP`，见开发指南）。
- 风险：`controls.control()` 的续开路径把生命周期写回 `open` 而不撤销会话，第 3 步须在同一事务撤销（本步对这种状态的处置：确认恢复整笔回滚、到期只结束会话）；`observation_generation` 列是本步新增的权威位置，第 3 步递增 `control_generation` 时须经 `authorize_session_in` 同步递增它。

## 第 3 步执行（2026-10-07，人工登记处置 → 开始观察，#85）

- 工作区：`../production-ops-agent-register`，分支 `feature/m1-02-register-remediation`，基于 main `924068d`；与第 4 步（#86，`../production-ops-agent-observer`）并行，本步只改授权/撤销路径，不碰 claim/submit。
- 行为：工作台动作 `register_remediation`（表单字段 `revision` = 处置后的目标版本，必填；带 `expected_generation`、幂等键、操作者审计，走与其他动作相同的 ledger/审计对账路径）。`ObservationStore.register_remediation` 一个事务：scope FOR SHARE → 事故行锁 → 代际不等 `CONTROL_CONFLICT` → 全局/目标挂起 `SCOPE_SUSPENDED`（挂起优先于单独观察授权，C3 §4，不记为已授权）→ `control_generation+1` → 撤销既有授权会话（`authority_revoked`）→ `authorize_session_in`（`observation_generation+1`，会话绑定新代际，`open → observing_recovery`）→ `opspilot_controls` 审计行（payload 含 revision、session_id、profile revision、被撤销会话）。期限/次数/间隔/持续窗口来自工作台加载的 HealthProfile（`OPSPILOT_HEALTH_PROFILE`，默认随产品的 checkout profile），前端不能给；无 profile 拒绝 `HEALTH_PROFILE_REQUIRED`。调查 Run 不动（D3）；代际推进会像任何人工决定一样使旧租约失效。再次登记（新 revision）= 新决定：旧会话同事务撤销、新会话承载新代际。
- 用户决定落地：目标身份 = `integration_id`/`cluster_uid`/`namespace`/`resource_uid`。迁移 `0004_target_identity` 给 `opspilot_targets` 补三列（NOT NULL、非空 CHECK；`resource_uid` 仍是唯一登记键）。已有行回填只从 `OPSPILOT_TARGET_IDENTITIES` JSON 文件取，缺任何已登记 uid、文件未设或条目畸形即 `TargetIdentityMissing`（列出 uid）、退出 1、不改任何东西、库停 0003；空登记表无需文件。授权动作不再接收 Target：从事故 `target_id` 指向的登记行取四项身份 + 本次 `revision` 快照构成会话 Target，并要求同一目标所有已有会话记录的四项身份与登记行一致，否则 `TARGET_MISMATCH` 不写任何东西；revision 变化照常授权。`#114` 的「首个会话固定全部字段」折中已删。事故接收时经配置的目标登记表（同一文件）把操作者 `target_id` 解析成完整身份再登记，不在表里的 `target_id` 以 `UNKNOWN_TARGET` 拒绝（已登记 uid 的不同身份 `IDENTITY_CONFLICT`）。
- 撤销：`controls.control()` 的 pause、cancel 与续开路径、`runs.new_run()` 在同一事务里调用 `opspilot/observation/revocation.py` 的 `revoke_authorized_sessions`（结束记录在生命周期写回 `open` 之前写，`lifecycle_before == after`），修掉第 2 步记录的续开风险；`ObservationStore._end_session` 改为委托同一模块（静态方法，第 4 步的调用点不受影响）。follow_up/correct/resume 不显式撤销：代际一变会话绑定过期，下一次采样按既有 `binding_stale` 结束。撤销/授权路径没有再套 `conn.transaction()` 或 SAVEPOINT（#114 证据触发器按 xmin 绑定）。
- 验证：`make check` 2747 passed / 425 skipped / 2 xfailed（既有 strict xfail）；ruff、ruff format、mypy 全过。临时 PG 17.9（55491，会话临时目录，未碰 55431）`M1_DURABLE_POSTGRES=1 M0_B_POSTGRES=1 M0_STEP_POSTGRES=1 pytest tests/integration`：358 passed / 10 skipped（既有 opt-in）+ 迁移文件 17 passed。新 PG 测试 `tests/integration/test_m1_02_register_remediation_postgres.py`（12 例）：登记后生命周期/两种代际/会话参数/Target/审计行；过期代际冲突不写；二次登记取代旧会话（结束记录 before=after=observing_recovery）；挂起拒绝不写、解除后同代际通过；resolved/closed 拒绝；登记行被改 `TARGET_MISMATCH` 不推进代际；pause/cancel/续开/new_run 同事务撤销；登记 vs cancel 同代际并发 8 轮，恰一个应用、另一个 `CONTROL_CONFLICT`，终态按胜者一致；工作台端到端：同键重放一个会话、过期表单 409、缺 revision 400、页面显示会话。迁移测试新增 3 例：有行无文件/部分覆盖/畸形条目均 fail-closed 停在 0003 且无新列；有文件回填、NOT NULL 与非空 CHECK 生效、`register_target` 幂等与冲突、downgrade 删列后再升级须再回填、dump 与 head 一致；空登记表无需文件。`tests/test_m1_02_register_web.py`（5 例，内存存储）：动作词汇、`revision` 归属、无 profile 拒绝、pause/cancel 撤销、登记表解析与 `UNKNOWN_TARGET` 不污染幂等键。第 2 步 PG 测试按新签名改 `_authorize`/`_target`，目标身份用例改为篡改登记行；断言语义不弱化。F6 验收/合同测试未改。
- 真实运行：[docs/evidence/m1-02-register/run.md](../evidence/m1-02-register/run.md)（空库 `migrate` 到 0004 → 起工作台 → 提交事故 → 未登记目标 `UNKNOWN_TARGET` → 登记处置 generation 1 → 同键重放 `replayed:true` → 过期表单 `CONTROL_CONFLICT` → 页面 `observing_recovery` / 会话 `authorized` / revision / profile revision → pause 后会话 `revoked`、结束记录 before=after=observing_recovery；页面 HTML 同目录）。临时 55491 库的 30 个已登记目标用生成的身份文件走了一次真实回填升级。
- 自行决定（可逆）：① `revision` 由操作者在登记时填写（Target.revision 非空必填、不属于身份，F6 夹具也以 `handled-revision` 表达「处置后的版本」）；② 登记表配置是 JSON 文件 `OPSPILOT_TARGET_IDENTITIES`，迁移回填与运行时解析共用同一文件与同一加载函数，不加依赖；③ 再次登记取代旧会话而不是拒绝（C3 §10「如仍获授权则创建新版本观察会话」，审计行记被撤销会话）；④ 挂起期间拒绝登记而非先记后撤（避免产生一个注定被结束的授权）；⑤ 撤销逻辑独立成 `observation/revocation.py`，`persistence` 在函数内延迟 import 避免包级循环（`test_architecture` 的两个 xfail 保持）；⑥ 工作台每次只加载一份 HealthProfile（M1-02 只有 checkout 一个），按目标选 profile 留给 #87/#88。
- 限制与未执行：接管（takeover）与同事务撤销拆到 #121；身份文件是静态文件而非注册服务；`resource_uid` 仍唯一（同 uid 跨集群未支持）；`register_remediation` 后 `state` 镜像不变、调查 Run 不动，运行中的尝试会在下一次提交被代际栅栏交接（与其他人工动作一致）；页面只显示会话行，采样依据展示属第 5 步；本机 55431 lab 库**未升级**（合并后按开发指南 `make migrate`，不再需要身份文件）；未做 Playwright 截图，以命令输出与保存的页面 HTML 为证。
- 独立审查处置（2026-10-07，PR #120，Codex 全新上下文，结论「修复后可合并」，1 P1 + 3 P2；全文在 lead 的 `tmp/m1-02-review/pr120-review-final.md`）：
  - P1 登记绕过事故暂停（已修）：`authorize_session_in` 在事故行锁下另读 `state`（不扩 `_lock_incident` 的列，Observer 角色没有 `state` 的 SELECT 授权），`paused` 一律 `ILLEGAL_TRANSITION`，`register_remediation` 与存储原语同受此限；登记不隐式解除暂停，恢复须显式 `resume`。目标/全局挂起解除后控制镜像仍是 `paused`（既有语义），因此也要先 `resume` 再登记——第 2 步 PG 测试「解除挂起后可再授权」与本步两个用例据此改为「先拒绝、resume 后通过」。领取/采纳侧（claim/submit 段，#119 在改）未改：pause 在同一事务撤销会话、登记在 paused 下被拒且两者都锁事故行，不存在「授权会话 + 事故 paused」的组合可供领取；审查复现的输入（pause → register → claim/submit）现在在 register 一步即被拒。PG 复现测试 `test_a_paused_incident_takes_no_registration`、web 测试 `test_a_paused_incident_takes_no_registration_until_resumed`。
  - P2 崩溃恢复把不同 revision 的登记误认成同一次（已修）：登记事务的审计 payload 带本请求的 ledger 幂等键与 revision；`_audit_matches()` 对 `register_remediation` 只在 payload 的键与 revision 都等于本 intent 时才确认，不再用 `unconfirmed_peers==1` 兜底。审查的 A/B 两键复现 `test_crash_recovery_confirms_a_registration_only_against_its_own_key`：只提交 A 的事务后重试 B → `CONTROL_CONFLICT`、无确认行；重试 A → `replayed:true`、会话 revision 为 rev-A。
  - P2 接管（takeover）未交付、P2 事故接收改为必须靠静态身份文件：属范围决定，lead 正在问用户，本轮未动。
  - 复验（第 1、2、4 条合并后）：`make check` 2749 passed / 426 skipped / 2 xfailed；临时 PG 17.9（55495，会话临时目录）全套 `tests/integration` 361 passed / 10 skipped + 迁移 15 passed；定向 5 个模块（登记、观察存储、迁移、web、事故控制）106 passed。
  - 第 4 条（事故接收不得依赖静态身份文件）——用户决定 2026-10-07，已按决定改回：事故接收恢复 main 行为，只按 `resource_uid` 登记（`register_target(resource_uid)` 签名不变），`serve`、接收与重投都不读 `OPSPILOT_TARGET_IDENTITIES`，接收路径的 `UNKNOWN_TARGET` 已删；0004 三列改可空（有值须非空 CHECK），迁移不再需要身份文件，已有行留空，lab 库升级不再需要回填文件。完整身份只在登记处置 / 授权观察时要求：`register_remediation` 在同一事务、事故行锁下 `FOR UPDATE` 登记行，行为空且工作台从身份文件解析出该 target_id 的身份 → 首次写入；已完整且与文件不同 → `TARGET_MISMATCH`；仍为空且无配置身份 → `TARGET_IDENTITY_MISSING`；三者都在写入任何东西之前。自决理由：选「登记处置时从身份文件读、首写锁定」而非单独工程命令，因为它复用同一文件与同一加载函数、不加命令面、并且身份进入系统的时刻恰好是第一次需要它的时刻（最简单、可逆）。兼容性测试：全套 PG web/控制测试在不传身份文件（`targets=None`）下照旧通过；`test_intake_needs_no_registry_and_registration_needs_the_identity`（未设文件接收两个任意目标 201，登记处置 `TARGET_IDENTITY_MISSING` 不写；配置文件后仅列出的目标可登记并补齐登记行）；PG `test_a_bare_registry_row_without_a_configured_identity_refuses_registration`、`test_a_completed_identity_never_changes_in_place`；迁移测试改为「已有行留空、可空列、非空 CHECK、`register_target(uid)` 幂等、downgrade/再升级 dump 一致」。
  - 第 3 条（接管）：拆到 issue #121，本 PR 不做。
