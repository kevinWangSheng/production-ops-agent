# Roadmap

只保留当前状态表；更新时替换对应行，不追加段落。历史叙述见 [ROADMAP 历史归档](docs/archive/roadmap-history-2026-09-21.md)。

## 当前状态（2026-09-24）

| 项目 | 状态 | 证据 / 下一步 |
|---|---|---|
| 实施门槛 | 有界开放 M1-01（2026-09-13 用户决策 B） | [决策记录](docs/evidence/m0-real-investigation/round-06-gate-decision-draft.md)；M0 未完成项转为 M1 入口条件，见 SPEC 第 6 行。 |
| feature passes | 0 / 11 | [feature_list.json](feature_list.json)；验收 harness 场景覆盖某 feature 全部步骤时翻转，接线为独立任务。 |
| M1-01 已合并 | 持久化与恢复（#19 #22 #26）、人工控制（#28）、重启恢复（#30）、工具执行器（#20）、租约续期（#35）、claim 人工优先（#34）、探针 flake（#36）、指令纪律单一来源（#27）、调查 loop 含长程改造（#29）、流程审计（#39）、loop 小缺陷修复（#41）、adapter flake（#38）、集成记录（#37）、intake 认证合同（#21）、人工控制补全（#31）、外部验收入口（#32）、workbench UI（#33）、交接不发布 + 工作台进度接真实驱动（#44）、超时清扫（#45）、网页追问上限 8192（#46） | main `1ece309`；任务记录见 `docs/tasks/` 下对应的 m1-01 与 fix 记录。 |
| M1-01 完成定义 | ①–⑦ 已决（2026-09-24，见下行）后补齐接线；真实驱动端到端跑通；v4 冻结包确定性用例全过；正常/故障各 2 次真实 Run 均通过独立证据审查、无未处理 P1/P2。不翻 `passes`，不含恢复观察与复盘 | [v4 验收包](docs/testing/first-investigation-v4-2026-09-10.md)「确定性与报告判据」 |
| M1-01 已决（2026-09-24） | ① 交接不发布、事故保持开放；⑦ 过期 Run 由清扫写超时交接（两项见 ADR-0005）；② 追问累计重发保持现状；③ 网页追问上限改 8192 并提示；④ `web` 依赖默认安装；⑤ 不做按事故授权（单团队）；⑥ close/reopen、重绑定、合并/拆分移出 M1-01。已知限制：合格结论发布后不可再追问；携带证据无观测载荷、时间策略双标准、intake 非同事务、C3 §5 按 ID 读证据降为已知偏离 | [ADR-0005](docs/adr/0005-handoff-and-deadline-terminal.md)；原则：优先对标上游 HolmesGPT |
| M1-01 剩余工作 | 按序：1 runner 交接不发布 + 工作台实时进度接真实驱动（已完成，#44）；2 超时清扫（已完成，#45）；2b 超时 Run 上的追问/纠正自动开新 Run（用户 2026-09-25 决定，本 PR，[任务记录](docs/tasks/2026-09-25-m1-01-timeout-followup.md)）；3 网页 8192 与 web 默认安装（已完成，#46，[任务记录](docs/tasks/2026-09-25-m1-01-web-input-limit.md)）；3b 网页提交 → 常驻 worker → 真实 runner 调查（工作台写入调查输入快照、worker 命令、DurableToolLedger 接入、超时 Run 的 resume 不再留死行；2026-09-26 用户确认属 M1-01 并优先）；4 suspension 持久化栅栏核对与补齐（已核查：main 各写路径已闭合，PR #48 补 PG 测试钉住，[任务记录](docs/tasks/2026-09-26-m1-01-suspension-fence.md)）；5 验收入口持久化场景改真实驱动（进行中，PR 待开：6 个场景由 DurableStore / runner / control / 清扫在真实 PG 上产生，入口按 ADR-0005 行形状投影，[任务记录](docs/tasks/2026-09-26-m1-01-acceptance-real.md)）；6 正常/故障各 2 次真实 Run + 独立证据审查。后续项：页面进度事件与恢复证据投影写失败后的补写修复（#44 机器人发现，按 ADR-0003 投影非权威） | [#44 任务记录](docs/tasks/2026-09-24-m1-01-handoff-runner.md)、[#32 任务记录](docs/tasks/2026-09-16-m1-01-acceptance.md)、[集成记录 PR #37](https://github.com/kevinWangSheng/production-ops-agent/pull/37) |
| DurableStore 加固 | A 类已合并（#26）；B/C 类待决 | [任务记录](docs/tasks/2026-09-15-durable-store-hardening.md)；运行时依赖声明按 AGENTS.md 由 Agent 自行处理。 |
| 模型 profile | 出站名 `deepseek-flash`，单次真实探针通过 | [任务记录](docs/tasks/2026-09-15-model-profile-v41.md)。 |
| LangGraph（ADR-0004） | 推迟，视为已决 | [ADR-0004](docs/adr/0004-langgraph-orchestration.md)；需要扩大比较时另立合同。 |
| M0 遗留入口条件 | K8s RBAC/OS 隔离、正式保留集与 judge 校准、产品级 streaming/PG 审计/压缩器接入 | [M0 退出矩阵](docs/evidence/m0-real-investigation/m0-exit-matrix.md)；供应商账单对账改为余额差记账，不再是入口条件。 |
| 测试纪律 | 红证明 CI 试行中（#49，只报告不阻塞，PG 路径已在 CI 回放 #47 验证）；审查范围收窄与验收测试作者分离已写入 AGENTS.md（#50） | [任务记录](docs/tasks/2026-09-26-red-proof-ci.md)；2026-10-10 前后评估是否改为阻塞。 |

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
