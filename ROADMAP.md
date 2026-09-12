# Roadmap

当前工程交付状态（2026-09-11）：M0-03 normal/fault v4 正向样本已达 2/2，M004 raw evidence 独立复核通过；PR #16 仍 OPEN，SPEC/M1 gate 等待用户决定。
Current phase: M0 质量包已完成本轮可执行复验；专属 m0-otel 已停止，历史证据保全。CI/本地/独立复验完成，Security 按用户决定忽略；不自动合并 PR。
Current phase: M0-03 已完成新 normal/fault 有界复验与相邻 baseline 对照；专属 m0-otel 已停止，历史容器/卷/VM/证据保留。Code/CI 交付门已通过，Security 按用户决定忽略；SPEC gate 仍 not cleared，等待最终入口决策。[M0-02 历史结果](docs/evidence/m0-real-investigation/round-02-results.md) 保留。

## 当前有效状态（2026-09-12，覆盖下方历史 checklist）

| 项目 | 当前状态 | 证据/下一步 |
|---|---|---|
| PR #16 尾项 | **已完成** | `commit_tool` 非 mapping fail-closed；merge-tree 无冲突；当前 HEAD `c618651`，CI `checks`/`m0-postgres` 通过。 |
| B1 退出矩阵 | **已完成** | [`m0-exit-matrix.md`](docs/evidence/m0-real-investigation/m0-exit-matrix.md) 已建立并持续更新。 |
| B2 预算校准 | **部分完成** | 候选值和历史分布已写入 v4 校准段；最终冻结仍待用户批准。 |
| B3 控制合同 | **部分完成** | 专属 PG 4 项合同通过；pause/resume 完整状态机、HealthProfile 持久乱序、独立 observer 授权仍缺。 |
| B4 真实恢复 | **已完成（机制范围）** | 三项 Run、PG 重建/取消/不兼容 handoff 和 LangSmith 白名单回读均有证据；不等于产品恢复验收。 |
| B5 权限/隔离 | **部分完成** | PG 只读角色实际拒绝写/DDL；K8s RBAC 环境缺测，OS 隔离未采购。 |
| B6 上游/eval | **部分完成** | provider probe 和 M004 upstream 复验已执行；正式可比报告未形成。judge 标注包已准备，人工校准/盲测待用户处理。 |
| B7 LangGraph | **已完成（离线比较）** | 1.2.11 隔离比较无可见收益，ADR-0004 推荐推迟；是否采用仍待用户决定。 |
| B8 账单 | **未完成** | 对账模板已准备，等待用户提供供应商账单导出/截图。 |
| SPEC gate / feature passes | **未开放/未修改** | `SPEC.md` 仍 `not cleared`；11 个 feature passes 均保持原值。 |

下方按日期排列的旧段落是历史过程记录；读取当前进度时以上表和文档末尾最新日期段为准。

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

- PR #16：最新代码与文档 HEAD 以远端为准；CI 与本地验证已完成，历史段落保留为历史。
- Security Review 对当前 HEAD 无运行或完成结果，不能记为通过；PR 仍 OPEN/BLOCKED。
- M0-03 正向样本 2/2 已完成；已披露 unknown 与后续任务边界保留，SPEC gate 由用户决定。
- 历史 20 CNY/20 HTTP/5 trace 合同和账本保留，不改写、不复用；无新预算合同前不追加模型/trace。
- 接手入口：`docs/tasks/2026-09-09-m0-real-investigation.md` 的“当前交接更新：M0 仍未完成”。

## M0-03 用户接受的已披露限制（2026-09-11）

用户接受当前五条 evidence unknown 为环境/工具契约的固有限制，不视为 M0-03 报告错误：bounded trace/log sampling、当前集成日志源缺失、日志过滤/allow-list 与展示上限、HTTP 500 无法总由 access-log view 直接映射、缺少 HealthProfile/SLO。日志源、过滤能力和 HealthProfile/SLO 归入后续任务；原始 evidence 和 unknown 保留，SPEC gate 是否开放另行决策。

## M0-04 独立质量包更新（2026-09-11）

M004 normal/fault 两个新 Run 的 raw evidence 已提交并经全新上下文独立复核，7/7 hash 各自匹配，均无 P1/P2；v4 有界开发包计数更新为 normal 2/2、fault 2/2。报告仍保留 partial 与五条已接受 unknown；SPEC gate 是否开放、PR #16 是否合并仍由用户最终判断。

## M0 退出矩阵与离线补证（2026-09-11）

- [x] B1 八包退出矩阵与 M1-01 任务/工时估算已建立：[m0-exit-matrix](docs/evidence/m0-real-investigation/m0-exit-matrix.md)。矩阵仅作索引，未把 partial/证据不足汇总为通过。
- [-] B2 已从 M002–M004 的可保留 usage/timing/count 记录提出候选预算；旧 `128KiB/8192/4/20/180s/20s/780s` 明确不冻结，候选与缺失 sidecar 写入 [first-investigation-v4 校准段](docs/testing/first-investigation-v4-2026-09-10.md)，最终冻结待用户批准。
- [-] B3 已补 unknown 语义、真实 PG 子集和 4 项控制合同；pause/resume 完整状态机、HealthProfile 持久乱序和独立 observer 授权仍为证据不足，见 [B3 记录](docs/evidence/m0-real-investigation/round-06-control-contracts.md)。

PR16 收尾 tip `0774960`：本地 `make check` 723 passed/44 skipped，专属 PG 合同 27+9+1 passed 后已停止；review threads 已处置并 resolve。`0babc02` 的 bot review 无 major issues，覆盖其代码；`0774960` 当前 bot 结果尚未返回，已由全新上下文独立复验与父提交 review 边界替代并在 PR 描述披露；不自动合并，SPEC gate 仍 not cleared。

后续 `da505dc`/`0a514a0`/`d1b6d15` 修复 timing sidecar 读前 guard、较早 query deadline 传播及 tool lock 后截止重检，均通过本地 `make check` 723 passed/44 skipped；当前 PR tip 为 `d1b6d15`（后续文档收尾），CI checks/m0-postgres 成功，threads 已处置并 resolve，SPEC gate 仍 not cleared。

## 2026-09-12 PR16 C1–C5 与 B4–B7 准备

- [x] C1–C5 代码修正已按独立提交完成，最终本地 `make check` 730 passed/44 skipped；没有模型/trace/OTel 操作。代码独立复审待记录。
- [x] D1–D3 文档状态已拆分并修正：退出矩阵、B3 `-rA` PG 输出、v4 候选预算出处均已更新。
- [x] B4 真实恢复合同已执行：每项 1 次、总 3 HTTP，三次 LangSmith 白名单回读 `TRACE_VERIFIED`；结果见 `round-05-recovery-results.md`。
- [-] B5 只读 PG 子项已执行并保留角色；K8s RBAC 环境缺测，OS 隔离未采购。
- [-] B6 上游比较已执行 provider/单步复验，judge 标注包已准备；正式可比报告、人工校准和盲测仍未完成。
- [x] B7 已用隔离 `uv run --with langgraph` 执行最小 StateGraph（版本 1.2.11）；与现有 loop 均 2 步/2 持久点/第二步取消，0 模型/工具 HTTP。ADR-0004 推荐推迟，采用与否待用户决定。

以上准备不打开 SPEC gate、不修改 feature passes；B4 付费执行、B5 设施、B7 采用与否均由用户决定。

文档最终独立复核已完成（`round-05-docs-final-recheck.md`），B4–B7 仅为待批准准备，未新增模型/trace/服务操作；SPEC gate 保持 not cleared。

## 2026-09-12 B4/B5/B7 执行结果

- [x] B4 三项真实恢复/控制验证各执行 1 次；B4-1 恢复、B4-2 cancel 迟到拒绝、B4-3 `INCOMPATIBLE_STATE` handoff 均有结果记录。3 模型 HTTP、known cost 0.004293 CNY、unknown reservation 1.0 CNY，未超 6 CNY 合同上界；三次 LangSmith 白名单回读 `TRACE_VERIFIED`。
- [x] B5 专属 PG 只读角色实际 SELECT 成功、写/DDL 拒绝；K8s 标环境缺测，OS 隔离方案不采购。
- [x] B7 已用隔离 `uv run --with langgraph` 安装 1.2.11 并完成固定序列比较（两路径 2 步/2 持久点/第二步取消，0 HTTP）；ADR-0004 推荐推迟，是否纳入主依赖仍待用户决定。
- [ ] B8 账单仍未核对；SPEC gate 继续 not cleared，PR 不自动合并。

PR review 新增的 unknown publish step P2 已由 `93f8555` 修复并有 PG 回归；最终 `make check` 732 passed/45 skipped，SPEC gate 与 feature passes 保持不变。

## 2026-09-12 B6 上游比较结果

- [ ] 候选真实 fixture/PG 链路 2 HTTP 成功；上游认证线路后续复验已返回模型内容，但未形成可比较最终报告，B6 同条件质量比较保持证据不足。结果见 `round-06-upstream-comparison-results.md` 与 `round-06-upstream-auth-followup-results.md`。
- [ ] 本 session 费用授权已明确，不再等待费用确认；后续 B6 只需另立受控 Run，保留每 Run 上限、usage/unknown 账本和数据出口边界。judge 校准、盲测和 B8 账单仍未完成，SPEC gate 仍 not cleared。

## 2026-09-12 B6 认证线路后续复验

- [x] 受信任启动器从私有 `.env` 向固定 Holmes checkout 注入 DeepSeek 凭据，单步无工具请求已返回模型内容；未打印、导出或上传凭据。
- [ ] 该次启动未持久化 wire HTTP 状态/usage，且模型输出未执行的 shell tool-call，不能计入正式同条件质量样本；B6 正式比较、judge 校准和盲测仍保持证据不足。详见 [`round-06-upstream-auth-followup-results`](docs/evidence/m0-real-investigation/round-06-upstream-auth-followup-results.md)。

## 2026-09-12 PR 尾项与 B3 控制合同

- [x] `commit_tool` 对非 mapping result 统一返回 `TOOL_PAIRING_INVALID`，list/str 回归通过；review thread 已回复并 resolve，见 `e505f2b`。
- [x] `git merge-tree --write-tree main HEAD` 无冲突，结果树与 main 用户未提交改动均未被触碰；证据见 [`round-07-merge-tree`](docs/evidence/m0-real-investigation/round-07-merge-tree.txt)。
- [x] 工作包 3 新增专属 PG opt-in 控制合同：generation 竞争、HealthProfile 旧版本/固定时钟存储、pause/resume fail-closed、investigation identity 隔离；4 passed，失败样例保留于 [`round-06-control-contracts`](docs/evidence/m0-real-investigation/round-06-control-contracts.md)。pause/resume 完整状态机与独立 observer 授权 API 仍缺，未汇总为 M0 通过。

## 2026-09-12 工作包 2 真实协议补证

- [x] 隔离 probe 在 6 HTTP 上界内实际执行 5 HTTP：stream 中断 2、工具 4xx continuation 2、手工配对视图 1；0 trace，费用按 ledger 记账，详见 [`round-06-protocol-results`](docs/evidence/m0-real-investigation/round-06-protocol-results.md)。
- [-] 结果只形成 provider/隔离实验部分证据：stream/工具错误尚未接入产品 StepStore/PG 审计，context compressor 尚不存在；不修改主 transport 合同，不打开 SPEC gate。

## 2026-09-12 B6 M004 上游复验

- [x] M004 normal/fault 各完成 1 次固定 Holmes 上游复验（2 HTTP，0 trace，4 CNY unknown reservation）；均到达模型但只产生未执行 tool-call，没有最终报告。详见 [`round-06-upstream-comparison`](docs/evidence/m0-real-investigation/round-06-upstream-comparison.md)。
- [-] 同条件质量比较、judge 人工校准与保留集盲测仍证据不足；失败进入分母，不宣称候选优于上游。SPEC gate 继续 `not cleared`。

## 2026-09-12 用户审核材料

- [x] 已生成 6 份独立审查样本的 judge 人工校准包、ledger 账单对账模板和 SPEC gate 两选项草案；均只作审核材料，不修改 SPEC gate 或 feature passes。
- [ ] 待用户确认 judge 分数、供应商账单差额、是否保持 gate `not cleared`；B5 K8s/OS 设施与采购仍不执行。
