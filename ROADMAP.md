# Roadmap

当前工程交付状态（2026-09-11）：PR #16 仍 OPEN。M0-03 已有真实 Holmes returned 报告；协调者质量审查结论为正常 1/2 通过、故障 0/2 正向通过，SPEC 不开放 M1。[M0-03 汇总](docs/evidence/m0-real-investigation/round-03-quality-summary.md) / [当前任务](docs/tasks/2026-09-09-m0-real-investigation.md)。

Current phase: M0-03 已完成新 normal/fault 有界复验与相邻 baseline 对照；专属 m0-otel 已停止，历史容器/卷/VM/证据保留。Code/CI 交付门已通过，Security 按用户决定忽略；SPEC gate 仍 not cleared，等待最终入口决策。[M0-02 历史结果](docs/evidence/m0-real-investigation/round-02-results.md) 保留。

## Completed decisions and documentation

- [x] 2026-09-09 用户指定默认DeepSeek Flash：直接使用deepseek-v4-flash，保留thinking/high。Pro及首次Flash记录保留；本轮新的真实Flash/上游实验见当前报告，不复用旧批准。

- [x] Operating constraints clarified — 2026-09-07: no backup/disk-disaster recovery; retain process/task recovery; CNY 1,000 adjustable initial budget reference; effectiveness first; data-use plan prepared by assistant then reviewed by user. F8 backup step retired with history, not passed.

- [x] Initial model adaptation targets DeepSeek; future GLM/provider replacement remains possible without committing first-release implementation — 2026-09-07.

- [x] Shared context-driven investigation, on-demand relevant knowledge, system-enforced execution boundaries and separation of test categories from product routing confirmed — 2026-09-07; ADR-0002.

- [x] Read-only incident/post-release investigation, human follow-up, recovery observation and reviewed postmortems confirmed — 2026-09-06.
- [x] Production writes, release gates, general pre-release review and separate operations Agents excluded — 2026-09-06.
- [x] Primary comparison direction: HolmesGPT; OpenSRE/K8sGPT supporting references; kagent optional — 2026-09-06.
- [x] Current specification, decision record, requirements, acceptance inventory and instructions synchronized; original draft archived — 2026-09-06.

## Development Agent environment

- [x] 开发能力接入与全盘规划 — 2026-09-08：[当前任务](docs/tasks/2026-09-08-agent-capabilities.md)。在主项目安装 LangChain 文档/API MCP，规划 CLI+skill、API/UI 测试、类型/秘密检查、trace/eval 和交接能力；Codex 项目配置读取、Claude Connected 及公共资料查询通过，独立审查 P2 已修复复验；其余新增工具待对应工作项实施，不改变 M0 门槛。


- [x] 开工上下文补齐 — 2026-09-08：[当前任务](docs/tasks/2026-09-08-preflight-context.md)。同步已合并状态、明确控制/观察语义及 coding agent 评测隔离，准备配置模板和 [M0-01 任务](docs/tasks/2026-09-08-m0-01-preflight.md)；本批本地检查和独立全文审查通过；未执行模型实验或开放实施门槛，M0-01 已接续离线准备，见下方当前任务。

- [x] GitHub 基础 CI 与 PR 管理 — 2026-09-08：PR #1 的 Ubuntu checks 成功并已合并（4502ece）；[任务](docs/tasks/2026-09-08-github-ci.md)，[交付与资源后续安排](docs/plans/delivery-and-resources-2026-09-08.md)。本轮无 dataset eval 或 CD；用户授权公开仓库后 main 分支保护已配置，机器人手动审查完成，文档发现已处理；自动推送复审待单独确认。PR #2 已合并为 f225d82，主线 CI、本地同步及 CI 任务 worktree 清理完成，最终结果已补入任务记录。

- [x] GitHub 仓库托管与基础设置 — 2026-09-08：[kevinWangSheng/production-ops-agent](https://github.com/kevinWangSheng/production-ops-agent)，初始私有，现已按用户授权公开；默认 main，合并后自动删除远程任务分支；Actions 默认只读、不允许工作流批准 PR。基础 GitHub Actions workflow 与 main 必需 checks 已配置；dataset eval、镜像构建与部署/CD 仍按交付资源规划后续推进。

- [x] 当前工程基线检查与本地版本保存 — 2026-09-08；范围、归档哈希、入口链接、开发检查均通过，保留已批准文档及开发工具；无远程发布，产品验收仍为 0/11。

- [x] 独立审查与上下文交接约定 — 2026-09-08；明确触发范围、实现者自测与独立审查职责，旧项目 skills 改为显式按需参考，未删除或新增 skills。仅规则落盘与静态检查，不代表全生命周期行为验证。

- [x] Python 3.12 开发入口已验证 — 2026-09-08；锁定依赖、Makefile、只读 doctor、Ruff/pytest 和 5 项测试通过；[任务与证据](docs/tasks/2026-09-08-dev-environment.md)。AGENTS 命令段落已确认写入，实验设施尚未就绪。

- [x] 任务记录与交接约定写入 AGENTS.md — 2026-09-08；明确 ROADMAP、任务记录与本地会话摘要职责，消除旧条款重复；[任务目录](docs/tasks/README.md)已建立；中文模板于 2026-09-08 经独立草案审查和用户确认后写入，静态检查通过。

- [x] Shared project instructions written — 2026-09-08: AGENTS.md is the common source, CLAUDE.md imports it, legacy rules are pointers; static checks and independent persisted-text review passed. No product gate changed. See [migration record](docs/agents/instruction-migration-2026-09-08.md).
- [x] 公共指令中文化与四条 worktree 生命周期约定 — 2026-09-08；独立实文复核、链接/锚点及验收清单不变检查通过。仅文本验证，未执行合并清理演练。
- [x] 共享指令加载验证 — 2026-09-08：Codex 已有受限新会话加载证据；Claude 本次新会话正确读取中文规则与最新独立审查/旧 skills 约定，账户限制不再阻断。仅加载与理解验证，不等于完整任务行为验证；旧 skills 原生发现不作为当前收尾条件。

## Next: M0 validation and acceptance calibration

- [-] 2026-09-11 M0-03：新增 m003e-normal-02 与 m003e-fault-05，均 strict completed/supported；补齐相邻 normal/fault baseline 对照。报告仍保留日志/采样/SLO unknown，未打开 SPEC gate；m0-otel 已停止，原始证据保全。[汇总](docs/evidence/m0-real-investigation/round-03-quality-summary.md)。 2026-09-11 独立审查：正常 1/2、故障 1/2，v4 包未通过；五条 unknown 经用户接受为已披露限制并记后续归属。[独立审查汇总](docs/evidence/m0-real-investigation/round-03-independent-review-summary.md)。

- [-] 2026-09-10 M0-02：20模型HTTP/0trace，相关真实PG同Run跨进程续传及最小持久/取消机制已独立验证，正常/故障JSON与核心结论有据；两份报告仍有P2事实错误，最新修复仅离线。首片v3为历史包；严格v4已补齐PR发现并经联合独立终审，案例/重复/非退化不削弱。下一有界工作M0-03，不写产品passes；详见[本轮结果](docs/evidence/m0-real-investigation/round-02-results.md)。

- [-] 2026-09-09 新一轮20模型请求已用完、1trace上传；正常1份报告有质量限制，主动故障0/2完成，另接续length空正文。环境已还原、停止并保留26容器/卷、2792trace归档及原PG；[运行结果与具体下一任务](docs/evidence/m0-real-investigation/results.md)。

- [-] 2026-09-09 M0 离线批次修复与汇合已进入 main：[批次索引](docs/tasks/2026-09-08-m0-batch.md)。#4–#10 全部已合并，#8/#9 内容及索引漏扫修复经 #10 汇入 main（e5ecfc0）；独立复验与主线 CI 均成功，全部历史工件保留。该离线批次不自动授权真实调用或打开M0退出/产品实施门槛；后续真实调用授权与结果见当前M0任务。

- [-] M0-01 — 2026-09-09：PR #14已合并为738b5c7，main CI34380825700成功，本地已同步。旧Pro/首次Flash失败及4CNY未核账保留；本轮Flash JSON mode最小修复后完整链路单次通过。[本轮报告](docs/evidence/m0-real-investigation/results.md)接续真实OTel/Holmes正常、故障及还原证据；主动故障报告与M0/产品入口仍未通过。

- [x] M0 P2 execution plan patched and persisted — 2026-09-07; isolated-context full review passed after restoring upgrade compatibility and explicit eval rules. See [execution plan](docs/plans/m0-validation-plan-2026-09-07.md) and [review](docs/reviews/m0-plan-adversarial-review-2026-09-07.md). 部分真实协议已执行，其他工作包按当前任务继续。

- [x] C3 complete technical design accepted for persistence — 2026-09-07. Three whole-candidate adversarial rounds closed observer/control races, independent release-observation identity and provider-private-field export contradictions. See [technical plan](docs/design/technical-proposal-2026-09-07.md), [review record](docs/reviews/technical-design-c3-review-2026-09-07.md) and [ADR-0003](docs/adr/0003-business-state-recovery-authority.md).
- [x] Technical direction: Python/FastAPI, PostgreSQL business recovery authority, DeepSeek-compatible adapter, LangSmith, Compose/Helm. LangGraph loop benefit and exact versions remain to be validated; graph checkpoints have no cross-attempt authority.
- [x] Source-first research for HolmesGPT/OpenSRE/Stratus and runtime/platform comparisons completed as static evidence; not feature completion.
- [-] F14 upstream capability mapping: source/issue candidates inspected; pinned real runtime attempts and concrete protocol/context gaps recorded; a bounded active fault report now localizes payment/Charge, while full report quality and matched candidate comparison remain pending.
- [-] M0模型/协议与控制快照已有子集真实证据；首流程步骤重建、取消/owner/epoch/lease与版本阻塞已有本轮有界证据；完整observer/发布/升级及产品集成仍待对应后续验证。
- [-] M0 pinned OTel Demo实际部署、正常/故障/还原、来源/权限探针和资源费用已记录；按服务日志/身份缺口、实际Holmes进程隔离、HealthProfile等仍待对应任务。
- [-] F1 历史v3包保留，当前[严格v4首片包](docs/testing/first-investigation-v4-2026-09-10.md)已完成实现/schema及独立复验冻结；开发案例规模/重复/评分/非退化不变；模型候选尚未通过，完整保留集/产品验收在后续阶段。原验收steps/passes不变。
- [-] 已形成[M0-02 / M1-01具体任务与入口缺项](docs/plans/first-vertical-investigation-2026-09-09.md)；故障报告收束、动态验收/目标及步骤恢复前提未满足，SPEC保留not cleared，不更新passes。

The earlier outer-readiness audit and V0/V1/V1.1/V2 proposal review are historical. Both product entry points remain in one complete release; symptom categories remain testing only.

## Planned delivery sequence

1. **M0 — compatibility and experiment prerequisites:** F14 baseline/reuse evidence and F1 contracts/eval preparation, dependency/model/storage/permissions validation and capacity measurements.
2. **M1 — complete vertical path:** authenticated intake through investigation, evidence, human handling, independent recovery and reviewed postmortem, with budgets and observability.
3. **M2 — full lifecycle and failures:** both entries, human control, delayed/duplicate inputs, knowledge versions and recovery; F2/F3/F6/F7/F11/F12/F13 acceptance evidence.
4. **M3 — complete release evidence:** matched baseline/held-out comparisons, soak, upgrade/recovery and delivery; F1/F8/F9/F14 and every active acceptance check.

F7/F8/F9 requirements apply from the first runnable system. No internal milestone substitutes for the complete product. No V2 scope or delivery date is promised; estimate effort after M0. Architecture is approved; runtime validation is partial and feature implementation has not started.

## Retired scope

F4 action broker, F5 rollout executor and F10 autonomous promotion are retired, not completed. IDs remain reserved. The original steps and migration mapping are in `docs/archive/pre-readonly-scope-2026-09-06/`.

Legend: `[ ]` Todo | `[-]` In Progress | `[x]` Completed. No active feature has passed its acceptance checks.
## Current handoff (2026-09-10)

- PR #16 HEAD `055e646`：本地 `make check` 689 passed / 44 skipped，CI `checks` 与 `m0-postgres` 成功；Code Review 覆盖当前 HEAD 无新发现。
- Security Review 对当前 HEAD 无运行或完成结果，不能记为通过；PR 仍 OPEN/BLOCKED。
- M0-03、真实报告质量、动态证据与完整恢复/取消验收仍未完成；SPEC gate 继续 `not cleared`。
- 历史 20 CNY/20 HTTP/5 trace 合同和账本保留，不改写、不复用；无新预算合同前不追加模型/trace。
- 接手入口：`docs/tasks/2026-09-09-m0-real-investigation.md` 的“当前交接更新：M0 仍未完成”。

## M0-03 用户接受的已披露限制（2026-09-11）

用户接受当前五条 evidence unknown 为环境/工具契约的固有限制，不视为 M0-03 报告错误：bounded trace/log sampling、当前集成日志源缺失、日志过滤/allow-list 与展示上限、HTTP 500 无法总由 access-log view 直接映射、缺少 HealthProfile/SLO。日志源、过滤能力和 HealthProfile/SLO 归入后续任务；原始 evidence 和 unknown 保留，SPEC gate 是否开放另行决策。

## M0-04 独立质量包更新（2026-09-11）

M004 normal/fault 两个新 Run 的 raw evidence 已提交并经全新上下文独立复核，7/7 hash 各自匹配，均无 P1/P2；v4 有界开发包计数更新为 normal 2/2、fault 2/2。报告仍保留 partial 与五条已接受 unknown；SPEC gate 是否开放、PR #16 是否合并仍由用户最终判断。
