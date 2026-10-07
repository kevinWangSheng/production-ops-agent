# M1-02 独立恢复观察（F6）

- 状态：进行中（第 1 步 PR 待审；第 2 步并行）
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
- 状态：代码与测试完成；PR 待独立审查。
- 待决：无（数值属可逆技术细节）。限制：稳态流量薄（2 locust 用户），比率分辨力有限；要更稳定的 F6 验收可在实验环境加用户数（第 0 步 values，不在本步）。

## 待决

- 无。HealthProfile 的具体数值属于可逆技术细节，由实施 Agent 校准并记录来源，候选评测前冻结。

## 下一步与交接

- 门槛 PR 已合并（#73）；子项 issue 见 #82–#88。
- 上游核对已完成（见上表）；M1 基础设施准备完成后从计划 0（kind 环境）开工。
- 当前没有运行中的服务或进程。
