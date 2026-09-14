# 产品约束抽离与指令主语归属

- 状态：进行中（实现、三轮独立审查与机器人审查发现处置完成；交付经 PR #18，
  合并前的实时状态以该 PR 为准，本记录不复述会随交付变化的断言）
- 更新日期：2026-09-14
- 依据：[AGENTS.md](../../AGENTS.md) 的权威来源划分与「详细内容放在其所属规范或文档中」
- 工作区：主工作区，分支 `chore/extract-product-constraints`，交付见 PR #18
- 范围：仅文档。未触碰代码、测试、`feature_list.json` 与 SPEC 的实施门槛陈述

## 目标与范围

把产品代码在运行时必须满足的约束从 `AGENTS.md` 与 `SPEC.md` 抽出，集中到新建的
`PRODUCT-CONSTRAINTS.md`，使 `AGENTS.md` 只保留开发 Agent 自身的规则。

起因是一次实际误用：在讨论产品 eval 能否完全确定性化时，`AGENTS.md` 里一条无主语的
「LLM judge 不得代替确定性安全或最终状态断言」被当作产品设计依据引用，而该条位于
「验证与汇报」小节，管的是开发 Agent 的自检与汇报。产品侧的对应要求实际在
`feature_list.json` 的 F1 步骤五。

## 判据

**按执行者归属。** 规则由读指令文件的那个 Agent 执行，留 `AGENTS.md`；由产品运行时
代码执行，进 `PRODUCT-CONSTRAINTS.md`；属于项目决策与门槛，留 `SPEC.md`。

这个项目造的产品本身是 Agent，`AGENTS.md` 又是多宿主共用的开发指令入口
（`CLAUDE.md` 导入它，`.Codex/rules/` 四个文件指向它的锚点），「Agent」一词被占用
两次。因此 `AGENTS.md` 内的规则一律写成第二人称，不出现「产品 Agent」「同样」这类
对照词，谁读这个文件谁就是主语。

同一原则在两个执行者上成立的规则，写成两条而不是一条。例如「证据不是指令」：产品版
管被调查系统的日志与 runbook，写进产品约束；开发版管工具返回与仓库文件，留
`AGENTS.md`。

## 前提与完成条件

前提是先补齐 SPEC 缺口再删除原处内容，顺序不可反。核实发现两条规则当时只存在于
`AGENTS.md`，`SPEC.md` 无任何等价表述，先删会直接丢规则。

完成条件：无规则丢失、搬运段落除已披露改动外逐字未改、全仓库无指向已迁出内容的引用、
`AGENTS.md` 无以产品行为为主语的残留、锚点与指针不断、本地检查通过、独立审查发现
全部处置。

## 执行进展与证据

本次工作以 [PR #18](https://github.com/kevinWangSheng/production-ops-agent/pull/18)
交付，按逻辑变更分为五步。不记录单提交哈希：分支已 rebase 到 `origin/main`，且本仓库
PR 采用 squash 合并，合并后逐提交哈希均不可达，以 PR 编号与下列内容为准。

1. 抽出六节到 `PRODUCT-CONSTRAINTS.md`，补两处缺口，同步 SPEC 权属声明。
   SPEC 25241 → 20242 字符，新文件 5682 字符（后续步骤另有小幅增改）。
2. 收窄 `AGENTS.md`：项目目标与权限六条缩为三条并改第二人称；必读项改指新文件全文；
   M0 实验授权段限定为实验 Run；删去产品路由约束；LLM judge 条拆条落主语。
3. 处置第一轮独立审查的四项发现。
4. 处置第二轮复验的两项新发现，并建立本任务记录。
5. 处置第三轮复验的发现与本记录的三处不准确。

搬运零改写已用脚本逐段比对：Explicit exclusions、Product workflow、Recovery
observations、数据流合同段完全相同；Evidence 与 Runtime 两节仅含已披露的三处改动。

`make check` 四次均为 800 passed、54 skipped，跳过项为需显式开启的 PostgreSQL 集成
测试。

## 独立审查

使用未参与本方案的 Agent，全新上下文启动，未提供实现者结论。三轮，第三轮由审查者实跑 `make check` 核对了本记录的数值声明。

**第一轮四项发现，全部核实属实并已修复：**

- 验收完整性被收窄。给 judge 那条加主语后，全仓库再无一般形式的禁令。SPEC 的
  Verification and delivery 无 judge 表述，产品约束的恢复节明确允许
  model-assisted grading，F1 步骤五只约束 F1 自身的 harness。已在 SPEC
  Verification and delivery 恢复一般形式，`AGENTS.md` 保留开发侧版本。
- 无出处的许可性文字。`the model may propose them, never grant them` 在变更前的
  SPEC 与 AGENTS.md 中均无对应表述，原文只禁止未表态。已删除该半句。
- 三处过期引用。首轮核验只 grep 了小节名，漏了描述性引用。`docs/README.md`、
  `PRD.md`、`AGENTS.md` 的审查机器人规则均已同步。
- 确认无规则丢失、锚点完好、提交未越界。

**第二轮复验两项新发现，已修复：**

- 根 `README.md` 的 Read first 清单仍按旧结构描述 SPEC，且五步清单完全没有新文件。
  这是仓库正门，与已修的 `docs/README.md` 属同类缺陷。已改描述并把新文件列为第二项。
- `SPEC.md` 的 Document ownership 收窄后不再认领验证与验收方法学，而第一轮刚把 judge
  规则放进 Verification and delivery，按权属句检索的读者找不到它。已补入该项，并把
  `PRD.md` 与 `docs/README.md` 的措辞对齐。另修正一处编辑瑕疵：项目状态句原被误挂在
  产品约束条目下。

**一项经复核后关闭，无需改动：** 凭据不得进入模型上下文这条现仅在产品约束文件，而
M0 实验由开发 Agent 发起。复核确认 C3 技术方案 §12「真实实验环境、资源与数据出口」
第 342 行已直接覆盖开发 Agent 的真实实验，且既有实验合同照此执行。不新增条款。

**第三轮复验一项新发现，已修复：**

- `docs/README.md` 的 Maintenance 节仍写 "Update SPEC when scope or constraints
  change"，这是全仓库唯一说明「什么变了改哪个文件」的规则，而 `SPEC.md` 的权属句
  正好把读者指向这里。迁移后照此维护会把刚抽出的产品约束重新写回 SPEC。与前两轮
  修的属同类过期引用，但危害方向不同：前者误导阅读，这条误导写入。已改为按变更
  类型分别指向 SPEC 与 PRODUCT-CONSTRAINTS。

审查者同时指出本记录三处不准确，均已更正：状态取值不在 `docs/tasks/README.md`
规定的五个值内；提交一之后 SPEC 实为 20242 字符而非 20138；不更新 ROADMAP 的理由
只引了支持该选择的一条规则，未提「ROADMAP 链接当前任务」。

## 未完成与下一步

- PR #18 已创建，CI `checks` 与 `m0-postgres` 均通过。Codex 机器人审查一项 P2
  （任务记录引用 rebase 前的临时哈希，从新检出不可达）已处置：改为按逻辑变更
  描述并以 PR 编号为准，因本仓库 PR 采用 squash 合并，逐提交哈希合并后必然失效。
- `SPEC.md` 中约 31% 为项目状态与门槛内容，其中四段带日期的历史状态段
  （2026-09-09/09-10 两处/09-12）按 SPEC 自身的 Document ownership 属 `ROADMAP.md`
  职责。本次未处理，捆入会使 diff 无法审查。这是独立的后续项。
- ROADMAP 已按 `docs/tasks/README.md` 的「ROADMAP 链接当前任务」在当前有效状态表
  新增一行链接本记录，标为进行中；不改动任何既有条目的状态。

## 相关工件

审计与方案预览页在 `~/.agent/diagrams/`：`agents-md-subject-audit.html`、
`agents-md-migration-plan.html`、`product-constraints-extraction.html`。属会话产物，
不在版本管理内。
