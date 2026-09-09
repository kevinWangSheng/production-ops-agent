# Roadmap

Current phase: technical design approved and persisted; local M0 A/B/C and integration are merged with independent review and main CI evidence; M0-01 one-shot live experiment was authorized and executed: model business completed; follow-up read-only diagnosis confirmed stored trace data and fixed a platform-metadata validation error. The initial failed exit is preserved; no full rerun or product gate clearance. Product boundary confirmed 2026-09-06; C3 design reviewed 2026-09-07. Product feature implementation has not started.

## Completed decisions and documentation

- [x] 2026-09-09 用户指定默认DeepSeek Flash：直接使用deepseek-v4-flash，保留thinking/high。Pro实验记录保留；当前仅切换配置/入口与合成检查，不重跑付费实验。

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

- [-] 2026-09-09 M0 离线批次修复与汇合已进入 main：[批次索引](docs/tasks/2026-09-08-m0-batch.md)。#4–#10 全部已合并，#8/#9 内容及索引漏扫修复经 #10 汇入 main（e5ecfc0）；独立复验与主线 CI 均成功，全部历史工件保留。真实调用、M0 退出与产品实施门槛保持关闭。

- [-] M0-01 — 2026-09-09：PR #12 已合并为 `55539bc`，#13 已关闭、自动审查门禁暂缓。原 Pro 两轮完成及 trace 后续只读确认保留，原 2 CNY 未核账；本轮按新的 [Flash 单次合同](docs/evidence/m0-01-live/flash-contract.md)运行，2 次 Flash 请求后最终 JSON 合同失败，未上传 trace；[结果与诊断修复](docs/evidence/m0-01-live/flash-execution.md)保留。[当前任务](docs/tasks/2026-09-08-m0-01-preflight.md)接续运行证据，完整矩阵/恢复与产品门槛仍未完成。

- [x] M0 P2 execution plan patched and persisted — 2026-09-07; isolated-context full review passed after restoring upgrade compatibility and explicit eval rules. See [execution plan](docs/plans/m0-validation-plan-2026-09-07.md) and [review](docs/reviews/m0-plan-adversarial-review-2026-09-07.md). Experiments remain unexecuted.

- [x] C3 complete technical design accepted for persistence — 2026-09-07. Three whole-candidate adversarial rounds closed observer/control races, independent release-observation identity and provider-private-field export contradictions. See [technical plan](docs/design/technical-proposal-2026-09-07.md), [review record](docs/reviews/technical-design-c3-review-2026-09-07.md) and [ADR-0003](docs/adr/0003-business-state-recovery-authority.md).
- [x] Technical direction: Python/FastAPI, PostgreSQL business recovery authority, DeepSeek-compatible adapter, LangSmith, Compose/Helm. LangGraph loop benefit and exact versions remain to be validated; graph checkpoints have no cross-attempt authority.
- [x] Source-first research for HolmesGPT/OpenSRE/Stratus and runtime/platform comparisons completed as static evidence; not feature completion.
- [-] F14 upstream capability mapping: source/issue candidates inspected; pinned runtime baseline, real reproductions and measured reuse costs pending.
- [ ] M0 dependency/model protocol validation, persistent reconstruction, cancellation/lease/observer races, upgrade compatibility and target-scoped identities.
- [ ] M0 pinned OTel Demo source/permission/label/retention/HealthProfile mapping; normal telemetry prerequisites and measured resource/cost estimates.
- [ ] F1 detailed IncidentScenario/IncidentOutcome packet, development calibration, frozen sample/repeat/scoring/non-regression thresholds before candidate evaluation. Retain existing acceptance inventory; no passes changed.
- [ ] Use M0 results to resolve incompatibilities and produce bounded implementation tasks/effort estimates; update the conditional SPEC gate with evidence.

The earlier outer-readiness audit and V0/V1/V1.1/V2 proposal review are historical. Both product entry points remain in one complete release; symptom categories remain testing only.

## Planned delivery sequence

1. **M0 — compatibility and experiment prerequisites:** F14 baseline/reuse evidence and F1 contracts/eval preparation, dependency/model/storage/permissions validation and capacity measurements.
2. **M1 — complete vertical path:** authenticated intake through investigation, evidence, human handling, independent recovery and reviewed postmortem, with budgets and observability.
3. **M2 — full lifecycle and failures:** both entries, human control, delayed/duplicate inputs, knowledge versions and recovery; F2/F3/F6/F7/F11/F12/F13 acceptance evidence.
4. **M3 — complete release evidence:** matched baseline/held-out comparisons, soak, upgrade/recovery and delivery; F1/F8/F9/F14 and every active acceptance check.

F7/F8/F9 requirements apply from the first runnable system. No internal milestone substitutes for the complete product. No V2 scope or delivery date is promised; estimate effort after M0. Architecture is approved, runtime validation and feature implementation have not run.

## Retired scope

F4 action broker, F5 rollout executor and F10 autonomous promotion are retired, not completed. IDs remain reserved. The original steps and migration mapping are in `docs/archive/pre-readonly-scope-2026-09-06/`.

Legend: `[ ]` Todo | `[-]` In Progress | `[x]` Completed. No active feature has passed its acceptance checks.
