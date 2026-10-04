# M1-02 独立恢复观察（F6）

- 状态：待开始（D1、D2 已决；门槛 PR 待用户合并）
- 更新日期：2026-10-03
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
| 上游 | HolmesGPT 是否有恢复观察能力：未确认（预期没有，它只做调查），实施前核对 | — |

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
   - 每次采样保存查询、时间窗、来源、每个必要信号的实际返回值（原始结果经现有证据登记，带 hash）及判定，重放只读这些存储，不再查遥测（F6 第 5 步）。
3. **人工登记处置 → 开始观察**
   - 工作台动作，带 `expected_version`、幂等键和操作者审计。
   - 递增 `control_generation`，授权新观察会话。
   - 暂停、接管、取消在同一事务里撤销观察授权（C3 §10）。
4. **Observer 采样任务**
   - 常驻 worker 的确定性任务，经现有只读工具网关查 Prometheus，不调用模型。
   - 健康窗口满足 → `resolved`。
   - 缺测、陈旧、低流量 → unknown，按原期限有界继续，到期交接，事故回到 `open`。
   - 持续异常 → 保持 `observing_recovery` 有界继续，到期交接，事故回到 `open`（C3 §10）。
   - 每个会话最多一个活动采样任务，租约重试保持原逻辑序号；全局/目标暂停同样挡住 Observer 采样（C3 §4）。
   - 已知偏离：Observer 与调查共用 worker 进程和网关，不做 C3 §3 的独立角色隔离，沿用 M1-01 现状。
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

## 待决

- 无。HealthProfile 的具体数值属于可逆技术细节，由实施 Agent 校准并记录来源，候选评测前冻结。

## 下一步与交接

- 门槛 PR（SPEC + ROADMAP + 本记录）提交后等用户合并。
- 合并后：先核对 HolmesGPT 有没有可借鉴的恢复观察实现，再从计划 0（kind 环境）开工。
- 当前没有运行中的服务或进程。
