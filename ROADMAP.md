# Roadmap

只保留当前状态表；更新时替换对应行，不追加段落。历史叙述见 [ROADMAP 历史归档](docs/archive/roadmap-history-2026-09-21.md)。

## 当前状态（2026-09-23）

| 项目 | 状态 | 证据 / 下一步 |
|---|---|---|
| 实施门槛 | 有界开放 M1-01（2026-09-13 用户决策 B） | [决策记录](docs/evidence/m0-real-investigation/round-06-gate-decision-draft.md)；M0 未完成项转为 M1 入口条件，见 SPEC 第 6 行。 |
| feature passes | 0 / 11 | [feature_list.json](feature_list.json)；验收 harness 场景覆盖某 feature 全部步骤时翻转，接线为独立任务。 |
| M1-01 已合并 | 持久化与恢复（#19 #22 #26）、人工控制（#28）、重启恢复（#30）、工具执行器（#20）、租约续期（#35）、claim 人工优先（#34）、探针 flake（#36）、指令纪律单一来源（#27）、调查 loop 含长程改造（#29）、流程审计（#39）、loop 小缺陷修复（#41）、adapter flake（#38） | main `c6fd889`；任务记录见 `docs/tasks/` 下对应的 m1-01 与 fix 记录。 |
| M1-01 待合并 | #21 intake auth、#31 控制补全、#32 验收 harness、#33 workbench UI、#37 集成记录（09-17 集成分支的历史验证证据） | #21 同步 main 中；#31/#32/#33 待 #21 同步后 retarget 到 `main` 并同步。 |
| M1-01 调查 loop 后续 | #29 已合并；已知偏离 C3 §5「按 ID 和片段读取」待证据读取工具；小缺陷修复（待重放计划校验、派发前 reasoning 校验、4xx 仅 429 重试）已合并 #41 | [任务记录「用户审核裁定」](docs/tasks/2026-09-21-m1-01-loop-long-horizon.md)、[设计草案](docs/design/investigation-loop-long-horizon-2026-09-21.md) |
| M1-01 缺项 | worker 组合层未接 DurableToolLedger；suspension 持久化栅栏为 Controller 待办 | 见 [集成记录 PR #37](https://github.com/kevinWangSheng/production-ops-agent/pull/37) 与 [工具执行器记录](docs/tasks/2026-09-14-m1-01-tool-executor.md)。 |
| DurableStore 加固 | A 类已合并（#26）；B/C 类待决 | [任务记录](docs/tasks/2026-09-15-durable-store-hardening.md)；运行时依赖声明按 AGENTS.md 由 Agent 自行处理。 |
| 模型 profile | 出站名 `deepseek-flash`，单次真实探针通过 | [任务记录](docs/tasks/2026-09-15-model-profile-v41.md)。 |
| LangGraph（ADR-0004） | 推迟，视为已决 | [ADR-0004](docs/adr/0004-langgraph-orchestration.md)；需要扩大比较时另立合同。 |
| M0 遗留入口条件 | K8s RBAC/OS 隔离、正式保留集与 judge 校准、产品级 streaming/PG 审计/压缩器接入 | [M0 退出矩阵](docs/evidence/m0-real-investigation/m0-exit-matrix.md)；供应商账单对账改为余额差记账，不再是入口条件。 |

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
