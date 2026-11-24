# 合同预审：#374（报告/审计/知识 90 天或归档）与 #375（steps/inputs/runs 清理的恢复合同）

- 预审者：全新上下文 Claude（Opus 5.5），只读，未参与 #244 实现
- 基线：`main` 与 `origin/main` 一致（2026-11-24）；工作区只有未跟踪的 `.playwright-mcp/`
- 全量探针：**未执行**。lead 明确禁止跑全量 pytest / `make check`（本机 swap 将满）。推荐方案 A 不改代码，不需要探针；如果用户选 B 或 C，实现开工前须补跑 `tests/acceptance` 探针，把失败清单补进本表
- 上游对照：本机上游 checkout `tmp/upstream/holmesgpt-a0009195/holmes` 里 grep `retention|ttl|purge`，只命中缓存 TTL（`core/supabase_dal.py:257,316-326`、`config.py:442`），没有业务数据保留期或归档逻辑。开源 HolmesGPT 自己不持久化调查记录（推断：持久化在 Robusta SaaS 侧，**未确认**）。所以「照搬上游」在这里给不出可抄的行为

## 1. 已核查事实（引路径行号）

### C3 与约束原文
- C3 §11 第 427 行：「关闭后的原始资料默认 30 天；报告、审计和知识默认 90 天或归档；eval 证据独立归档」。第 429 行：知识引用所依赖的资料固定保留；清理覆盖「原始数据、缓存、provider history、摘要、outbox 和外部平台副本」；删除依赖时撤销用途或留墓碑。第 431 行：已记录 #244 的实现，以及两处已知偏离（#374、#375）。第 433 行：不做备份与灾备。
- C3 §7 第 248 行：「PostgreSQL 业务记录是唯一跨进程恢复依据」；第 266-268 行：撤权后无法安全重建时阻塞交接；不兼容状态进入 `blocked(INCOMPATIBLE_STATE)`，通过基于业务事实的新 Run 接续。
- PRODUCT-CONSTRAINTS「Evidence」节：报告链接必须能解析到实际采集的证据；知识保留来源、审核状态与新鲜度。「Data flow」节：新增数据接收方、部署外部服务由用户决定。

### #374 相关
- 0008 给 F13 十张表装了语句级 DELETE/TRUNCATE 拒绝触发器，其中七张 `FROZEN_TABLES` 连 UPDATE 也拒绝（`opspilot/migrations/versions/0008_postmortem_knowledge.py:119-140,640-654`）。正文列有：`postmortem_versions.content`（:240）、`postmortem_conclusions.body`（:275）、`postmortem_proposals.content`（:324）、`knowledge_revisions.content`（:337），都是 NOT NULL，都带 sha256。
- 导入的 runbook 表（0028）同样拒绝所有角色的 DELETE/TRUNCATE（`0028_runbook_imports.py:34-35,236-240`）。
- F7 审计时间线 `audit_timeline()`（`opspilot/acceptance_audit.py`）只发 SELECT，`detail` 只含 id、枚举和计数，不含自由文本（`docs/tasks/2026-10-16-f7-authz-audit.md:37-41`）。它从约 20 张表读行（`acceptance_audit.py:100-577`）。**行在，时间线就完整；行被删，时间线就缺条目**；只把正文列置空不会影响时间线。唯一例外：query 类别的 tool/target/reason 在没有证据视图时从 `opspilot_steps.tool_results` 取（`acceptance_audit.py:211-245`），这一点和 #375 相关。
- 报告就是 `opspilot_incidents.conclusion`，写入的是终答步骤的完整 response（`opspilot/persistence/steps.py:430-445`）。图表与 F3 页面读 `opspilot_evidence.view`（#244 保留了它，`0001_baseline.py:159`）。
- 已有的 30 天清理 `opspilot/retention.py`：凡有 active 知识引用的事故整体阻挡（`ACTIVE_KNOWLEDGE`，:295-300），草稿审核中或生成待定也阻挡（:279-293）。因此 D12 对原始资料一侧已经满足。

### #375 相关
- `rebuild()`（`opspilot/persistence/incidents.py:218-301`）对当前 Run 的**每一个**非 late 步骤都校验 `response` 与 `tool_results`；response 不是 dict，或 tool_results 不是 list，就抛 `INCONSISTENT_STATE`（:239-250；`steps.py:19-44,97-122`）。它还读取整个事故的全部 `opspilot_inputs`（:277-290）。
- `rebuild()` 的调用方远不止 worker：事故页（`web/service.py:3334`）、追问与续开（`:2070,2126,3012,3976`）、runner（`investigation/runner.py:164,263,332,358`）、工具层（`tools/ledger.py:47`、`fixture.py:290`、`otel_demo*.py`）、验收投影（`acceptance_knowledge.py:94`）。**步骤一旦清空，关闭事故的页面也会直接报错**，不只是恢复路径。
- 后继 Run（追问 / 续开）的输入取自上一个 Run 的 `runs.input`，加上上一个 Run 步骤 `tool_results` 里已采纳的视图（`investigation/inputs.py:361-383`、`investigation/context.py:1800-1846,1876-1925`）。`runs.input` 为空时报 `INPUT_MISSING`，然后回退为「用 intake 原问题开全新输入」；这条回退要调 `_alert_rebuild`（`web/service.py:2096-2175`）。按 #244 PR-3 的已知限制，原始告警已清理时这条路径返回 `INCOMPATIBLE_STATE`，控制动作被拒绝（任务记录 #244「PR-3 已知限制」）。
- 关闭事故上的追问必须先 reopen（`REOPEN_REQUIRED`，`persistence/controls.py:410`）。reopen 会清空 `closed_at`（0023），清理命令就不再碰这个事故。
- 页面的「报告所属 Run」靠 `s.response = i.conclusion` 等值匹配（`web/store.py:727-742`），报告的覆盖范围与证据列表读该 Run 的 `input` 与 steps（`web/service.py:3380-3420`；`investigation/coverage.py:167`）。
- 复盘生成把 `opspilot_inputs.content` 作为人工动作文本读入（`knowledge/generation.py:440,528-540`）。
- `steps.response` 原样保存 provider 私有字段 `assistant.reasoning_content`（#458）。工作台 SQL 读取时剔除它，也剔除 `result.content`（`web/store.py:1183-1188`）；trace 白名单丢弃它（`tracing.py:31,421`）。**#458 已于 2026-11-22 关闭**，结论是「按合同允许：C3 §12 允许它作为受限协议状态在同 provider、同 Run 内回传」。
- 现状：`opspilot_steps`、`opspilot_inputs`、`opspilot_runs` 没有不可变触发器；`inputs.content` 是 NOT NULL，`steps.tool_results` 是 NOT NULL DEFAULT `[]`，`steps.response` 与 `runs.input` 可空（`0001_baseline.py:78-102,131`）。`opspilot_retention` 对这三张表只有锁用的单列 UPDATE，有守卫触发器拒绝任何实际改动（`persistence/grants.py:446-510`；0023 docstring）。
- 测试精简（#709/#714）以后，仓库里已经没有 `test_244_*` 验收文件；`tests` 下 grep `RETENTION_GRANTS|opspilot_retention` 只命中 `tests/integration/test_runbook_imports_postgres.py`。钉值面很小。

## 2. 已裁决（不再问，引出处）

| 编号 | 内容 | 出处 |
|---|---|---|
| K1 | 30 天从最后一次 close 起算（DB 时钟写 `closed_at`，reopen 清空）；没有 close 时间的旧数据不动 | #244 记录 U1、U6 |
| K2 | 固定保留：活跃 Run/观察、未终态复盘生成、审核中草稿、被 active 知识引用、冻结评测依赖（PG 侧无评测表，r3-C） | U2、r3-C |
| K3 | 执行者为独立 CLI 加独立 `opspilot_retention` 角色；web/worker/Observer 不增权；默认 dry-run，apply 显式开启，没有关闭开关，天数只能加长 | U4、U6、r3-D |
| K4 | 锁序 incident → run/session → postmortem/knowledge，锁后复查；按合并组整体判定 | U8、r3-A、r3-B |
| K5 | D12：将来清理时，被知识引用的复盘、审核审计与证据最小元数据必须固定保留或留墓碑，不得悬空 | `docs/tasks/2026-10-05-m1-03-postmortem.md:36` |
| K6 | 已清理原始告警的事故 reopen 后若需重建告警上下文，fail-closed 返回 `INCOMPATIBLE_STATE` | #244 记录 PR-3 已知限制（lead 2026-10-27） |
| K7 | LangSmith 产品副本只用 14 天基础保留，本地 outbox 7 天删除 | C3 §11:431；用户 2026-10-31（#243） |
| K8 | `reasoning_content` 存进 steps 按合同允许 | #458 关闭评论 2026-11-22 |
| K9 | 方向：只保主路径，不为边角 case 投入；能照搬上游就照搬 | post-M3 方向（memory，2026-11 系列） |

## 3. 决定表一：需用户裁决

### U1（#374）关闭满 90 天后，系统怎么处理事故报告、审计记录和已批准的知识？

| 选项 | 对用户数据做什么 | 代价 |
|---|---|---|
| **A（推荐）** | 什么都不删，也不改。报告、审计、复盘与知识按现状长期留在 PG（F13 表已由触发器保证只追加）。C3 §11 改写为「报告、审计与知识随事故长期保留，不做自动删除或归档；单团队自托管，整库处置由部署者负责」，作为已知偏离照实写进 C3 与作品集说明 | 与 C3 原文「90 天」不一致；库只增不减（这三类是小文本，量很小）。零代码、零迁移，不改任何 `passes` |
| B | 原地归档：90 天后给事故打上「已归档」标记，默认列表隐藏，打开后仍可完整查看；不删任何行，不导出 | 隐私收益为零（数据都还在）；要新增列与迁移、加列表过滤、页面徽标，F7 时间线需标注；约 2 个 PR |
| C | 删除或墓碑：90 天后把报告正文、复盘/知识正文、审计细节置空，只留 id、哈希与时间；被 active 知识引用的整组保留 | 必须改 0008 的 UPDATE/DELETE 拒绝触发器（F13 的不可变保证从「绝对」降为「清理角色例外」）；F7 时间线、F13 历史页、F13 验收投影、知识提示都要定义「已清理」语义；知识被清理后，后续调查拿不到提示；约 4 个 PR，涉及 F13/F7 验收面 |

- 推荐理由：审计记录在一般实践里保留得比原始数据更久，90 天后删审计与 F7「可重建审计」的目标相反。知识本来就靠人工 revoke/supersede 与 30 天陈旧标记管理有效期（SPEC 2026-10-15 F13 第 4 步段）。上游没有对应行为可抄。作为作品集项目，照实写明偏离比做一个没人会运行的归档器更可信。
- 兜底：用户不答，按 A。

### U2（#374，仅在选 B 或 C 时才需要回答）归档或删除前，要不要把数据导出到 PG 以外？

- **推荐：不导出**。归档只在原库内标记或置空，不新增任何接收方，不写文件，不接对象存储。
- 选「导出到本机文件或对象存储」等于新增数据出口；按 PRODUCT-CONSTRAINTS Data flow 节和 C3 §12，新增接收方与部署外部服务都是用户决定，还要单独登记凭据与脱敏规则。
- 代价：不导出意味着 C 方案清掉的内容无法找回。兜底：不导出。

### U3（#375）关闭满 30 天后，要不要清理调查步骤（模型回复与工具结果）、人工消息和 Run 输入快照？

| 选项 | 对用户数据做什么 | 代价 |
|---|---|---|
| **A（推荐）** | 不清理。这三类随事故长期保留。C3 §11 改写为「Run 步骤、输入与输入快照随事故保留：它们是报告覆盖范围、调查日志、复盘生成与追问续接的依据（C3 §7）」，作为已知偏离 | 模型思考文本（`reasoning_content`）与工具结果副本一直留在库里。这与 #458 的关闭结论一致：页面与 trace 已经剔除它们 |
| B | 只剥离 provider 私有字段：关闭满 30 天、Run 已终态时，从 `steps.response` 删掉 `assistant.reasoning_content`，其余不动（正好对应 C3 §11 的「provider history」） | 要给清理角色加一列受限 UPDATE 和守卫触发器（权限变更，属用户门）；需核对 `incidents.conclusion` 的等值匹配与 `rebuild_transcript` 不受影响（**未确认**终答行是否带 reasoning）；约 2 个 PR。与 #458 刚关闭的结论部分重叠 |
| C | 按 issue 原文完整清理：`response`/`tool_results`/`inputs.content`/`runs.input` 置空并标记，`rebuild()` 返回显式不可恢复码 | 关闭事故的页面会丢掉报告覆盖范围、调查日志原始结果和人工消息；追问只能拒绝或从头开；复盘生成和 F7 query 细节降级；约 15 个 `rebuild()` 调用方都要适配；触及恢复路径，要附有界真实 Run trace；约 4 个 PR |

- 推荐理由：这三类正是 C3 §7 的恢复权威，也是事故页的数据源。清掉以后能换来的只是把已经不外发的文本从自己库里删掉，代价是主路径（看报告、追问）降级，与 post-M3「只保主路径」方向相反。
- 兜底：用户不答，按 A。

### U4（#375，仅在选 C 时才需要回答）清理之后有人 reopen 并追问，系统怎么做？

- **推荐（C 下）：拒绝追问**，返回固定码（暂定 `RUN_DATA_CLEANED`，与 K6 同向 fail-closed），页面提示「该事故的调查资料已按保留期清理，请新开事故」。
- 另一选项：用最初的问题开一个全新 Run，不带任何旧证据与旧对话。代价：后继 Run 看上去是续接，其实丢了上下文，用户可能误以为模型记得之前的结论；而且告警来源的事故仍会因 K6 被拒，行为不统一。
- 兜底：拒绝。

### U5（C3 文字，A 方案必经）把上面的偏离写进 C3 §11

- 改第 427 行：保留「原始资料 30 天」与「eval 证据独立归档」；把「报告、审计和知识默认 90 天或归档」改为「报告、审计和知识随事故长期保留（已知偏离，见下）」。
- 改第 431 行「当前实现」段：去掉「尚未清理（恢复合同 #375）」与「90 天归档尚未实现（#374）」的待办口吻，改为两条已知偏离：① Run 步骤、输入与输入快照不清理，理由是 C3 §7；② 报告、审计、知识不做 90 天删除或归档，理由是审计与知识的用途，以及单团队部署没有归档接收方。provider 私有字段的处理引用 #458。
- 第 429 行「清理覆盖……provider history、摘要」需加一句限定：steps 内的 provider history 按偏离 ①处理。
- 这是 C3 合同文字变更，属用户门：文档 PR 合并前给用户一句行为说明。

## 4. 决定表二：可逆默认（Agent 自定，仅在 B/C 时生效）

| 编号 | 问题 | 默认 | 理由 |
|---|---|---|---|
| R1 | 执行入口 | 复用 `python -m opspilot.retention`，加 `--scope raw|provider-history|records`；默认仍只跑 raw | 同一 CLI 与角色，K3 不变；不加新进程 |
| R2 | 天数旋钮 | records 默认 90，下限 90，只能加长；provider-history 跟 raw 走 30 | 与 K3「只能加长」同规则 |
| R3 | 起点时钟 | `closed_at`（DB 时钟） | K1 已定 |
| R4 | 状态字段 | 照 0023 的 `payload_state`（present/absent/cleaned）加插入触发器 | P1/P2 先例，避免写路径破坏 |
| R5 | rebuild 遇到已清理步骤（C） | 新码 `RUN_DATA_CLEANED`；worker 把 Run 置为 `blocked(INCOMPATIBLE_STATE)`；页面走只读降级视图，不调 `rebuild()` 的校验 | C3 §7:268 已规定不兼容即 blocked；码名可逆 |
| R6 | 固定保留扩展（C） | 在 K2 上再加两条：复盘尚未生成且观察已结束；事故被 reopen 过但尚未再次关闭 | 复盘生成读 `inputs.content`（`generation.py:528`）；都是「多保留」方向 |
| R7 | 归档页面（B） | 列表默认隐藏已归档事故，详情照常可看，加「已归档」徽标；F7 时间线照常输出 | 行不删，时间线无缺失语义 |
| R8 | 墓碑形态（C） | 保留 id、`*_sha256`、时间与 actor，正文置空；页面显示「已按保留期清理」，与「被篡改」区分 | #244 R2/R3 先例 |
| R9 | D12 落地（B/C） | 凡有 active 知识引用的事故，整组不归档、不清理；revoked/superseded 知识的来源复盘照常处理，但保留墓碑 | K5；与 `retention.py:295-300` 同口径 |

## 5. 九类必答项对照

| 类别 | 结论 |
|---|---|
| 1 时间 | 已定 K1；records 的 90 天按 R2 |
| 2 计数口径 | CLI summary 沿用 r3-D；B/C 加 scope 字段（可逆） |
| 3 状态转移 | U3/U4 待决；R5 可逆 |
| 4 主体与权限 | B/C 都要扩 `RETENTION_GRANTS`（权限变更，PR-1 用户门）；C(374) 还要放宽 0008 触发器 |
| 5 数据合同 | U1/U3 待决；迁移编号接 0029 |
| 6 旧数据兼容 | 没有 `closed_at` 的旧事故不动（K1）；R4 回填 |
| 7 并发 | K4 |
| 8 外部调用 | 无模型调用；U2 决定是否有数据出口 |
| 9 验收与 passes | A：不变。B：不变。C(374)：F13/F7 验收投影需定义已清理语义（验收断言变更，属用户门）。C(375)：触及恢复路径，PR 须附有界真实 Run trace 与 `summary.json` |

## 6. 推荐：做 / 延后 / 关闭

- **#374：关闭**（选 U1-A）。由文档 PR 把偏离写进 C3 §11 与作品集说明，issue 评论链接该 PR。理由见 U1。
- **#375：关闭**（选 U3-A）。同一个文档 PR 处理。若用户在意「provider history」字面，退一步选 U3-B，作为一个小功能单独做；不推荐 C。
- 作品集角度：照实写明「做了什么、刻意不做什么、为什么」，比为没有真实用户的保留期再搭两套清理器更可信。#244 已经证明这套清理机制能做出来。

## 7. PR 切分

- A+A：1 个文档 PR（C3 §11 三处、ROADMAP F13 行的延后项改为已知偏离、#244 任务记录追加一行），属用户门（C3 变更），不派独立审查（memory：文档 PR 不派审查），CI 过了就简报。
- 若选 U3-B：PR-1 迁移 0029（给 retention 角色 `steps.response` 的受限 UPDATE，守卫只允许删除 `assistant.reasoning_content` 一个键）+ `RETENTION_GRANTS`，权限属用户门 → PR-2 CLI `--scope provider-history` 与独立验收 → 文档 PR。
- 若选 U1-B：PR-1 迁移（`archived_at` 放在 incidents 上，不碰 F13 表）+ CLI → PR-2 列表过滤与徽标 → 文档。
- 若选 C：每个 issue 各 4 个 PR（schema 与触发器 → rebuild 与读侧降级 → CLI → 文档），均为功能 PR 用户门；#375 的 PR 要附真实 Run trace。

## 8. 迁移风险

- A：无。
- U3-B：低。单列受限 UPDATE 加守卫，downgrade 在有已剥离行时拒绝（0023 先例）。
- U1-B：低。incidents 上新增可空列，不触碰 0008。
- C(374)：高。要重建 0008 的 FROZEN 触发器并加清理角色例外，F13「对 owner 也不可变」的保证被削弱；downgrade 必须拒绝。
- C(375)：中高。要去掉 `inputs.content` 的 NOT NULL；`rebuild()` 及其约 15 个调用方的行为都要变；旧 worker 不认识新码（靠 C3 §11 停机迁移前提兜住）。

## 9. 相邻缺口（不在两 issue 范围，仅登记）

- `opspilot_web_ledger` 里的 intake envelope（原问题文本，`0001_baseline.py:171`）、Chat 面板表（0026）、反馈表（0027）也是关闭后仍在的自由文本，C3 §11 没有单独点名，#244 也没覆盖。U5 的文字建议一并写进「随事故保留」，不另开 issue。
