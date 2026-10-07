# Roadmap

只保留当前状态表；更新时替换对应行，不追加段落。历史叙述见 [ROADMAP 历史归档](docs/archive/roadmap-history-2026-09-21.md)。

## 当前状态（2026-10-07）

| 项目 | 状态 | 证据 / 下一步 |
|---|---|---|
| 实施门槛 | 有界开放 M1-01（2026-09-13 用户决策 B）；有界开放 M1-02 恢复观察（2026-10-03）；有界开放 M1 基础设施准备（2026-10-05：数据访问层标准化、OTel + LangSmith trace），均见 SPEC | [决策记录](docs/evidence/m0-real-investigation/round-06-gate-decision-draft.md)；M0 未完成项转为 M1 入口条件，见 SPEC 第 6 行。 |
| feature passes | 0 / 11 | [feature_list.json](feature_list.json)；验收 harness 场景覆盖某 feature 全部步骤时翻转，接线为独立任务。 |
| M1-01 已合并 | 持久化与恢复（#19 #22 #26）、人工控制（#28）、重启恢复（#30）、工具执行器（#20）、租约续期（#35）、claim 人工优先（#34）、探针 flake（#36）、指令纪律单一来源（#27）、调查 loop 含长程改造（#29）、流程审计（#39）、loop 小缺陷修复（#41）、adapter flake（#38）、集成记录（#37）、intake 认证合同（#21）、人工控制补全（#31）、外部验收入口（#32）、workbench UI（#33）、交接不发布 + 工作台进度接真实驱动（#44）、超时清扫（#45）、网页追问上限 8192（#46）、超时 Run 追问开新 Run（#47）、suspension 栅栏（#48）、网页 worker（#52）、验收入口真实驱动（#53）、OTel 只读工具（#54）、报告合同修复与 v4 重跑（#55–#57）、窗内计算点与视图显式化（#58）、调查 loop 对齐上游（#59）、收口（#60）、事故状态改为人工控制标记（#61）、报告证据图表（#62 #65）、e 类错误归因（#63）、工具视图按真实 token 计量与超限拒绝（#64）、ROADMAP（#66）、估算器对账不改代码（#67）、覆盖摘要发现（#68）、指标视图 `series_note`（#69）、列表页 Run 状态（#70）、交接停放后 `run_handoff` 补写（#71） | main `1e0c4d5`；任务记录见 `docs/tasks/` 下对应的 m1-01 与 fix 记录。 |
| M1-01 完成定义 | ①–⑦ 已决（2026-09-24，见下行）后补齐接线；真实驱动端到端跑通；v4 冻结包确定性用例全过；正常/故障各 2 次真实 Run 均通过独立证据审查、无未处理 P1/P2。不翻 `passes`，不含恢复观察与复盘。**状态（2026-09-28）：已收口，严格口径未达成，`passes` 不翻转**——v4 严格口径（各 Run 无未处理 P1/P2）在 6d、6f 与对齐上游三批中均未达成；按上游评测口径（根因找对、正常场景无误报）连续三包 6/6（含 2 个留出故障）；核心 4 场景 P2 合计 14→10→8→6，剩余以模型自由文本数值 e 类为主。用户 2026-09-28 决定如实收口，[收口记录](docs/tasks/2026-09-28-m1-01-closure.md) | [v4 验收包](docs/testing/first-investigation-v4-2026-09-10.md)「确定性与报告判据」 |
| M1-01 已决（2026-09-24） | ① 交接不发布、事故保持开放；⑦ 过期 Run 由清扫写超时交接（两项见 ADR-0005）；② 追问累计重发保持现状；③ 网页追问上限改 8192 并提示；④ `web` 依赖默认安装；⑤ 不做按事故授权（单团队）；⑥ close/reopen、重绑定、合并/拆分移出 M1-01。已知限制：合格结论发布后不可再追问；携带证据无观测载荷、时间策略双标准、intake 非同事务、C3 §5 按 ID 读证据降为已知偏离 | [ADR-0005](docs/adr/0005-handoff-and-deadline-terminal.md)；原则：优先对标上游 HolmesGPT |
| M1-01 剩余工作 | 无。1–8 已完成（明细见 `docs/tasks/` 下 2026-09-24 至 09-29 的 m1-01 任务记录，收口见 [收口记录](docs/tasks/2026-09-28-m1-01-closure.md)）；收口后续项 1–4 已完成（#61–#65）；2026-10-01 批次已完成（#67–#71）：[交接补写](docs/tasks/2026-10-01-m1-01-handoff-backfill.md)、[列表页 Run 状态](docs/tasks/2026-10-01-m1-01-list-run-state.md)、[series_note](docs/tasks/2026-10-01-m1-01-series-note.md)、[估算器对账](docs/tasks/2026-10-01-m1-01-token-estimator.md)（实测少算约 11–20%，校准后仍可能残余约 6%，用户决定不改代码）、[覆盖摘要](docs/tasks/2026-10-01-m1-01-coverage-refusals.md)（只随强制最终报告轮发送，被拒调用列入放弃）。后续改进（不属 M1-01）：#94 #95 #96 #97 | 下一步：M1-02（见下行） |
| M1 基础设施准备 | 已完成（2026-10-07）：① 数据访问层标准化（ADR-0007：保留参数化 SQL）——Alembic 基线与接管 #104、状态 CHECK 约束 #106、拆分 `persistence.py` #107、`psycopg_pool` #109；② OTel 埋点接 LangSmith（仅 lab，失败即关闭）#108，实验证据改为平台 trace + 仓库冻结摘要 `summary.json` #110。后续：实验证据脚本两处改进 #116 | [数据访问层](docs/tasks/2026-10-05-m1-prep-schema-migrations.md)、[ADR-0007](docs/adr/0007-data-access-raw-sql-with-standard-tools.md)、[trace](docs/tasks/2026-10-05-m1-prep-trace-langsmith.md)、[ADR-0006](docs/adr/0006-trace-evidence-and-backlog.md) |
| M1-02 独立恢复观察（F6） | 进行中：第 0–2 步已合并——kind 实验环境 #111、HealthProfile 与单次采样判定 #113、观察会话/原子采纳/Observer PG 角色（迁移 0003，本机 lab 库已升级）#114；F6 验收与合同测试 #112（全新上下文 Codex 编写，40 个外部场景待接线 skip）。下一步第 3 步登记处置（#85，含完整目标身份决定）与第 4 步 Observer 进程（#86，含新鲜度须基于底层样本时间戳等合并前转入的要求）可并行；之后第 5 步重放展示（#87）、第 6 步真实验收（#88）；验收夹具接线加固 #115；模块位置随 ADR-0007 待决项 #117 | [任务记录](docs/tasks/2026-10-03-m1-02-recovery-observation.md)、[F6 测试映射](docs/testing/f6-acceptance-tests.md) |
| M1-03 复盘与知识（F13） | 待设计：上游无复盘生成；P1–P4 待用户决定（#89）；开工前还受 M0 入口条件约束 | [任务记录](docs/tasks/2026-10-05-m1-03-postmortem.md) |
| DurableStore 加固 | A 类已合并（#26）；B2/C1/C2 随 M1 基础设施准备完成（#104 #106 #107 #109）；B1、C3 待决（#98 #99） | [任务记录](docs/tasks/2026-09-15-durable-store-hardening.md) |
| 模型 profile | 出站名 `deepseek-flash`，单次真实探针通过 | [任务记录](docs/tasks/2026-09-15-model-profile-v41.md)。 |
| LangGraph（ADR-0004） | 推迟，视为已决 | [ADR-0004](docs/adr/0004-langgraph-orchestration.md)；需要扩大比较时另立合同。 |
| M0 遗留入口条件 | 待规划：逐项提议已写，E1/E2 待用户决定（#90）；子项 #91 #92 #93；K8s RBAC 可借 M1-02 的 kind 环境取证 | [任务记录](docs/tasks/2026-10-05-m0-entry-conditions.md)；[M0 退出矩阵](docs/evidence/m0-real-investigation/m0-exit-matrix.md)；供应商账单对账改为余额差记账，不再是入口条件。 |
| 测试纪律 | 红证明 CI 试行中（#49，只报告不阻塞，PG 路径已在 CI 回放 #47 验证）；审查范围收窄与验收测试作者分离已写入 AGENTS.md（#50） | [任务记录](docs/tasks/2026-09-26-red-proof-ci.md)；2026-10-10 前后评估是否改为阻塞（#100）。 |

## 交付顺序

1. **M0** 兼容性与实验前提：已收敛，见退出矩阵。
2. **M1** 完整竖切片：认证入口 → 调查 → 证据 → 人工处理 → 独立恢复观察 → 复盘；含预算与可观测性。当前在此阶段。
3. **M2** 全生命周期与失败路径：两个入口、人工控制、迟到/重复输入、知识版本与恢复；F2/F3/F6/F7/F11/F12/F13 验收。
4. **M3** 完整发布证据：匹配基线/保留集比较、soak、升级/恢复与交付；F1/F8/F9/F14 及全部验收。

F7/F8/F9 从第一个可运行系统起适用。没有里程碑可替代完整产品；不承诺 V2 范围或日期。

## 已退役范围

F4 action broker、F5 rollout executor、F10 autonomous promotion 已退役，ID 保留；原步骤与迁移映射见 `docs/archive/pre-readonly-scope-2026-09-06/`。

## 已确认决策

- 2026-09-06 只读事故/发布后调查、人工跟进、恢复观察与复盘；生产写入、发布门、通用预发布审查与独立运维 Agent 排除。
- 2026-09-07 上下文驱动的共享调查机制（ADR-0002）；C3 技术方案批准；无备份/灾备范围；首发适配 DeepSeek。
- 2026-09-09 默认 Flash；2026-09-15 出站名改为 `deepseek-flash`。
- 2026-09-13 门槛决策 B，预算冻结值批准，judge 标注包单人确认。
- 2026-09-21 费用逐次审批取消，改为常设授权加技术上限；流程按 [流程审计](docs/tasks/2026-09-21-process-audit.md) 调整。
