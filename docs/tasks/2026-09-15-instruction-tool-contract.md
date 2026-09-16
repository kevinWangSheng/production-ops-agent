# 调查指令与工具接口合同

- 状态：第一轮（合同撰写，PR #24）**已合并**；
  第二轮（DISCIPLINE 收敛 + 确定性测试）PR 已就绪，待用户审核合并。
- 更新日期：2026-09-15
- 依据：[SPEC.md](../../SPEC.md)「Model priority and design ownership」「Verification and delivery」；
  [C3](../design/technical-proposal-2026-09-07.md) 第 5、7、8 节；
  [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)；[ADR-0002](../adr/0002-context-driven-investigation.md)。
- 第一轮工作区：`chore/prompt-tool-contract` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-prompt-tool-contract`，
  PR [#24](https://github.com/kevinWangSheng/production-ops-agent/pull/24) 已合并进 main。
  收尾时的状态：最新提交的 `checks` 与 `m0-postgres` 均 success，
  机器人审查 thread 全部已回复并 resolve，`mergeStateStatus` 为 `CLEAN`、`mergeable` 为 `MERGEABLE`。
- 第二轮工作区：`chore/instruction-contract-impl` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-instruction-contract`，
  起点 main `b483a12`。进展、验证证据与交接见下方「第二轮：DISCIPLINE 收敛与确定性测试」。
  交付状态以 PR 当前 HEAD 为准，不在此复制提交哈希（避免记录随推送失效）。
  **两轮的合并均由用户审核后执行，本任务不自动合并。**

## 目标与范围

产出[C3 第 5/8 节](../design/technical-proposal-2026-09-07.md)：
定义模型可见文字（L1 调查纪律 / L2 报告契约 / L3 工具描述）的归属、版本生成规则、
必填项与来源索引，并按用户要求核对 DeepSeek 官方文档后写入供应商绑定事实。

范围界限（**第一轮**）：仅新增设计提案文档与导航/任务记录。
不改产品代码、不改实验脚本、不做模型切换、不改 SPEC/ROADMAP/`feature_list.json`/门槛陈述/预算冻结值。

第二轮范围另见下方「第二轮：DISCIPLINE 收敛与确定性测试」——该轮**新增产品模块与测试**，
仍不改实验脚本记录值、不做模型切换、不改 SPEC 门槛陈述/`passes`/预算冻结值。

## 前提与完成条件

- 前提：只读核查仓库与官方文档，无模型调用、无外部付费、未知费用为 0。
- 完成条件：三条落差各有仓库内证据；来源索引每条 L1 约束可回指具体记录；
  DeepSeek 事实均引官方页面；冲突与决策点上报用户而不自行选择版本。
- 不在完成条件内：用户对提案 §5 两个决策点（U1 别名弃用处置、U2 并入 C3 或独立）的决定，
  以及合同的实现与实现后的确定性测试。

## 必要上下文

- `opspilot/domain/runs.py` `ModelProfile`；`opspilot/persistence.py` `claim()` 的 `versions` 比对。
- `opspilot/tools/registry.py`（PR #20，分支 `feature/m1-01-tool-executor`，**未合并 main**）。
- `scripts/m0_environment/report_contract.py`（L2 现存唯一正确版本化实例）。
- `scripts/m0_lab/round07/{candidate_runner.py,replay_tools.py}`；`scripts/m0_environment/holmes_baseline.py`。
- `docs/evidence/m0-real-investigation/round-02-*-review.md`、`round-03-m003c-fault-02-review.md`、
  `investigation-outcome-review.md`（来源索引的取证对象）。

## 执行进展与证据

### 已完成

1. **三条落差已取证**，见提案第 0 节：
   `ModelProfile` 已声明 `prompt_revision`/`tool_schema_revision`，`versions` 栅栏已实现并触发
   `blocked(INCOMPATIBLE_STATE)`，**但二者尚未接线**（三分支全仓搜索仅命中定义与测试夹具），
   且无 revision 生成规则；`ToolRegistration` 无 `description` 字段
   （`opspilot/tools/` 全包搜索无面向模型的描述文本，且 fingerprint 的工具层与参数层两处投影都会静默丢弃新字段）；
   调查纪律因 L1 混入 L3 内容而按工具面分叉成两份，
   `candidate_runner.py` 的 15 句是 `holmes_baseline.py` multi-step 变体 24 句的真子集，0 句独有。
   另有 `max_steps == 1` 的 final-report 变体（同为 24 句，与前者共享 18 句），
   故 **L1 全集为两变体并集 30 句**；覆盖检查以 30 句为分母并按变体分别校验（提案 §3.3）。

2. **来源索引已建立**，见提案第 3 节：**L1 全集 24 句**逐条回溯，
   区分规范派生 / 失败派生 / 效率派生 / 设计派生。

   初稿只回溯了候选臂的 11 条，且把
   `The authorized query window is fixed by the trusted runner` 误判为设计派生——
   当时的检索只找显式拒绝（`tool arguments denied`、round-07 `tool-calls.json` 的 params 均为 `{}`），
   而真实失败是**静默截断**：`investigation-outcome-review.md` 记录模型把 start/end
   抄早 0.533447 秒、参数被接受、无任何错误码。该行现为**失败派生**，
   回溯方法学写入提案 §3.1。现存仅有的两条无失败背书约束是第 6 行（效率派生）
   与第 24 行（设计派生）。

3. **DeepSeek 官方事实已核对**（2026-09-15，均为 api-docs.deepseek.com 一手页面），见提案第 4.1 节。

4. **发现并上报请求别名的弃用时钟**，见提案第 4.3 节。官方 2026-09-10 声明 V4 Flash 已退役、
   `deepseek-v4-flash` 临时路由到 V4.1 Flash。

   初稿把它写成「新发现的未处理冲突」，经独立审查更正：
   `round-02-provider-identity-decision.md` 及其独立审查已于同日处理请求名/响应名分歧，
   `round03.py` 的双名允许集是该已批准决定的实现而非漏检。
   真正新增的只有「已退役」与「临时路由」两点定性。仓库实测：

   ```
   round-07-upstream-runs/*/response-{1,2}-business.json  共 8 份
   created 1789241365 → 2026-09-12T19:29:25Z
   请求名 deepseek-v4-flash，响应 "model": "deepseek-flash"
   ```

   风险落点是 SPEC 与 C3 固定的出站名 `deepseek-v4-flash` 有了失效时钟：
   临时别名一旦下线，冻结包规定的出站名会直接失败。
   **不影响既有 M0 证据与 ROADMAP B2 冻结的校准值**——报告身份分界在
   2026-09-10T02:19Z，而 B2 取材的 M002–M004 全部在其之后，集内身份一致（提案 §4.3 末）。
   **处置属用户决定，本任务不选择版本**。

### 验证

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| 提案内引用的评审原文 | 逐条 `sed -n '<行>p'` 核对 9 处 | 全部命中，引文与原文一致 |
| 仓库内模型回报名统计 | 遍历 `docs/evidence/**/*.json` 取 `model`+`created` | 8 份带时间戳响应全为 `deepseek-flash` |
| 硬编码模型名范围 | `grep -rl deepseek-v4-flash`（排除 docs/.venv） | 28 个文件；本次一个未改 |
| 内部链接与锚点 | 见下方链接检查 | 通过 |

**关于 `make check`**：初次提交时判断「纯文档新增不必跑」，**该判断是错的**——
本仓库的 `ruff format --check .` 会格式化 Markdown 内的 Python 代码块，
PR #24 的首次 CI 因此失败（`docs/design/...md:193` 的行内注释缩进）。
修复后已用主工作区的 `.venv/bin/ruff format --check docs/`（268 files already formatted）
与 `ruff check docs/`（All checks passed）本地复验。
**教训**：本仓库的文档改动同样受 `make check` 覆盖，不能以「只改文档」跳过。

## 归位决定（2026-09-15 用户裁定）

用户裁定 U2：**治理合同并入 C3，模型特性参考保持独立并被 C3 引用。**

据此：

- 分层、revision 生成与 bump 条件、四类变化四套机制 → 写入 **C3 第 5 节「指令分层与版本」**；
- 模型可见的工具描述（结构化五字段、`cannot_prove`、枚举内联、保密与权限两类禁止项、
  纳入注册表哈希）→ 写入 **C3 第 8 节**；
- `deepseek-flash-prompt-tool-reference.md` 保持独立，按 C3 第 15 节「设计依据」的既有先例
  被引用（与 `deepseek-adapter-design-evidence-2026-09-07.md` 同类）。选独立的理由是它
  **日期绑定**：供应商一变就要重核，塞进 C3 会让已批准合同频繁 churn。
- 独立的 `instruction-and-tool-interface-contract-2026-09-15.md` **已删除**，
  内容并入 C3，不保留平行文档（AGENTS.md：不将现有流程另建为平行文档）。
- **AGENTS.md 未改动**：它自己规定「详细内容放在其所属规范或文档中；普通链接是导航」，
  且每次会话加载，不适合承载具体撰写规则。
- **未在 SPEC 之上新增层级**：那会造出与 SPEC 竞争的权威。

### 未并入 C3 的材料及去向

| 材料 | 去向 | 理由 |
|---|---|---|
| 三条落差分析（`prompt_revision` 未接线、`ToolRegistration` 无描述、L1 混入 L3） | 保留在本任务记录 | 是并入的动因，不是合同条款 |
| L1 来源索引（24/30 句逐条回溯） | **不并入** | 取材自 M0 实验脚手架，用户已明确该批内容无产品参考价值 |
| 回溯方法学（须覆盖静默失败，不能只检索拒绝码） | 保留在本任务记录 | 方法有效，但属实施经验非合同条款 |
| 七项确定性检查 | 保留在本任务记录 | 实现时的验证设计，待实现任务承接；逐项实施状态见下方「C3 第 7 节七项确定性检查的逐项状态」 |

## 接入点（实施者从哪里会被指到）

文档写完不接线等于没写。已接入四处，都是 AGENTS.md 要求「每个编辑任务前固定复核」
或「沿引用核对」会经过的位置：

| 位置 | 接入内容 |
|---|---|
| **C3 第 5 节** | 新增子节「指令分层与版本」——合同正文，不是指针 |
| **C3 第 8 节** | 注册合同句之后新增「模型可见面」段——合同正文 |
| **C3 第 15 节** | 设计依据列表加入模型特性参考文档 |
| `SPEC.md`「Model priority and design ownership」 | 在 *Record effective prompt … versions per run* 之后，指向 C3 第 5/8 节与参考文档，**写任何 prompt 或工具描述前必读** |
| `m0-exit-matrix.md` 的 M1-01 拆分表 | 「只读工具执行器」「Flash 调查 loop」两行之后加说明 |

`docs/README.md` 的导航两条为补充入口，不作为唯一接入点。

## 下一步与交接

1. **两个决策点均已裁定，不再是待办。** 本条原为「待裁决」表述，已过期，此处更正为事实：
   - **U1**（`deepseek-v4-flash` 请求别名的弃用处置）：用户 2026-09-15 决定
     **把出站请求名改为 `deepseek-flash`**，随 PR #25 合并进 main。见
     [模型 profile 任务记录](2026-09-15-model-profile-v41.md)、
     [C3 第 5 节「模型接入」](../design/technical-proposal-2026-09-07.md)与
     [SPEC「Model priority and design ownership」](../../SPEC.md)。该裁定
     **不影响既有 M0 证据或 ROADMAP B2 冻结的校准值**——依据是供应商的退役公告，
     不是逐 Run 身份覆盖。
   - **U2**（并入 C3 还是保持独立文档）：裁定为
     **治理合同并入 C3，模型特性参考保持独立并被 C3 引用**，见上方「归位决定」。
     独立合同文件已删除，正文在 C3 第 5/8 节。
   - 原编号 U2「是否采用 strict tool mode」已整条撤销（C3 第 5 节已定默认关闭 strict beta），
     原 U3 顺延为现 U2。
   - C3 正文经核查**不含** U1/U2 待决表格，无需同步：
     `grep -n 'U1\|U2\|待用户决定' docs/design/technical-proposal-2026-09-07.md` 无命中。
2. 合同实施分两批。**第一批（DISCIPLINE 收敛 + 确定性测试）已完成**，
   见下方「第二轮：DISCIPLINE 收敛与确定性测试」。
   **第二批（`ToolRegistration.description`）待 PR #20 合并后承接**，依赖关系见同节。
3. 独立审查已完成（全新上下文 Agent，未参与撰写），提出 F1–F10 共 10 条发现，
   全部已复核并处置，逐条记录见下方「独立审查与处置」。
4. 本任务未启动任何进程或服务，无未提交的用户 WIP。
   worktree `chore/prompt-tool-contract` 在 PR 合并且确认整合后再清理。

## 独立审查与处置

审查者以全新上下文启动，仅获目标、约束、批准合同与待审工件，未获实现者结论。
10 条发现全部采纳，其中 1 条我独立复核后修正了审查自身的论据错误（F3）。

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| F1 | §0.3 漂移方向写反 | 成立 | 逐句差集确认 candidate 15 句是 baseline 24 句真子集、0 句独有；重写为「L1 混入 L3 内容」 |
| F2 | start/end 约束误判为「无失败来源」 | 成立 | 来源在 `investigation-outcome-review`（窗口被抄早 0.533447 秒，无错误码）；§3 重建并新增 3.1 回溯方法 |
| F3 | §4.3 漏掉仓库已有处置 | 部分成立 | `round-02-provider-identity-decision` 及其独立审查已处理；§4.3 重写为「别名弃用时钟」，删去无证据的「校准横跨代际」。**审查的支持论据「无响应回报过 deepseek-v4-flash」经复核为误**（实有 6 处），但其结论仍成立，已另循 ROADMAP B2 取材路径证实 |
| F4 | §1.1 规则 2 与规则 4 互斥；权限收紧会强制取消在途调查 | 成立 | L3 拆为 L3a 模板 / L3b 实例，新增 §1.3 |
| F5 | §2.3 禁止项与 D1/D3 冲突，且误用 `RESERVED_PARAMETERS` | 成立 | 拆为 (a) 保密 / (b) 权限，§7 第 4 项改为凭据正则 + 参数键检查 |
| F6 | §0.1 承重前提是推断 | 部分成立 | 三分支搜索确认绑定未实现；更正为「字段已声明、栅栏已实现、绑定未实现」，并据此加强论点；§1.2 补记 blocked 唯一出路是 cancel |
| F7 | strict mode 被当作待决项 | 成立 | C3 第 5 节已定默认关闭 strict beta；§2.4 重写，原 U2「是否采用 strict tool mode」整条撤销，原 U3「并入 C3 还是独立」顺延为现 U2 |
| F8 | 压缩器 `reasoning_content` 被称「未决张力」 | 成立 | C3 第 5 节已有「不兼容则阻塞交接，不猜测删除协议字段」；§4.2 改为实现核对项，不需用户决定 |
| F9 | §7 第 1 项口径已过期，与第 7 项对立 | 成立 | L3a/L3b 拆分后「改动 L1/L2/L3 任一字节须 bump」会要求 L3b 也 bump；且内容哈希下「字节变而 revision 不变」构造上不可能。第 1 项改为 L3a fingerprint + L1/L2 golden hash 两句，并明写不适用于 L3b |
| F10 | §7 第 3 项不可确定性判定 | 成立 | D1/D5 是语义属性，自由文本无注册期判据。§2.1 的 `description` 改为结构体 `ToolDescription`（`returns`/`window_format`/`values_format`/`limits`/`cannot_prove`），注册期只断言结构完整性，文字质量交人工审查 |

决策点 6 的四个方面（数据出口、私有协议字段、只读权限、人工控制优先级）经审查均未发现违规；
F4 留下的「权限收紧连带作废在途 Run」链路已由 §1.3 与 §7 第 7 项闭环。
§7 七项中 2、4、5、7 可直接确定性实现，6 已补机器可读载体要求，1、3 按 F9/F10 修正。

另处置两点保留：任务记录的 hold 范围已按 F3 结果收窄；§1/§2 补「批准前不生效」限定。
§7 编号顺序错误（自引入）已修。

### 机器人审查（`@codex` on PR #24，commit `a3cfbbd`）

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| P1 | §1.3 与 C3 第 4 节 suspension 合同冲突：按原文，授权收紧后旧 Run 可在变更后的授权下继续 | **采纳** | 原 §1.3 犯了二选一错误（要么 `versions` blocked、要么 Run 继续），漏掉 C3 已有的第三套机制。重写为「三种变化，三套机制」：合同变更走 `versions`、实例变化走 `tool_face_sha256`、**授权收紧走 scope generation 且在途结果失效、需显式重新授权**。本节规定收窄为单一否定式命题：授权收紧不得经由 `versions` 触发 `INCOMPATIBLE_STATE`（因语义与恢复路径不同），而非「让 Run 继续」。§7 第 7 项相应拆为三类断言 |
| P2 | 任务记录的决策编号过期（称 strict mode 为 U2、并引用不存在的 U3） | **采纳** | 按现 §5 表更正：原 U2 整条撤销，原 U3 顺延为现 U2，现存 U1/U2 两项 |

第二轮（commit `6570652` 复审）：

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| P1-2 | L1 自身含运行时插值，与 §1.1「L1/L2 覆盖最终拼装字符串」+「不得运行时注入」矛盾 | **采纳** | 实测确认 L1 24 句中有两处插值：第 5 句 `{args.max_steps}`、第 24 句按 scope 拼接的授权服务列表。若计入 `prompt_revision`，两个仅预算或授权范围不同的 Run 会仅因此被 blocked。L1 比照 L3 拆为 **L1a 模板 / L1b 实例值**，规则 2 改为三层一律适用；§7 第 1 项新增断言「仅 `max_steps` 或授权服务列表不同的两个 Run，`prompt_revision` 必须相同」 |
| P2-2 | L1 句数不一致（§0.3 写 23，§3 写 24） | **采纳** | 实测：基础段 22 句 + 按 scope 拼接的两句 = **24 句**。§0.3 更正为 24 并补全文统一的口径说明，明确第 3 节按这 24 句编号 |

第三轮（`6570652` 复审）P2-3：任务记录「执行进展与证据」第 2、4 项仍是初稿口径
（11 条 L1 约束、窗口约束标为设计派生、模型代际冲突牵连 B2 校准值）——**采纳**，已按 F1/F2/F3 结论同步。

第四轮（`917d225` 复审）P2-4：§0.3 句数改 24 时独有句数同步改为 9，
正文却仍称「独有的 8 句全部是投影字段语义」，第 9 句未点名——**采纳**，
已分两类点名：8 句投影字段语义迁往 L3，第 9 句（第 24 句授权服务列表）属 L1b 不迁。

机器人审查共四轮 5 条，**全部采纳并修复，无拒绝项**。第二轮的 P1-2 与第一轮的 F4 同源——
模板/实例拆分当时只做在 L3，L1 原地漏掉；机器人复审补上了这个缺口。

## 第二轮：DISCIPLINE 收敛与确定性测试（2026-09-15）

- 工作区：`chore/instruction-contract-impl` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-instruction-contract`，起点 main `b483a12`。
- 范围：执行上方「下一步 2」里**本轮可做**的部分。不改产品运行行为、不改历史实验脚本的记录值、
  不做模型切换、不改 SPEC 门槛陈述、`feature_list.json` 的 `passes` 或预算冻结值、不调用模型或任何外部付费服务。

### 收敛前的实测：DISCIPLINE 实际有几份、在哪

`grep -rn 'DISCIPLINE'`（排除 `.venv/` 与 `docs/evidence/`）只有一处具名常量，
但**按文本算是两处拷贝**，第二处是内联字面量而非常量，所以按名字搜不到：

| 位置 | 形态 | 句数 |
|---|---|---:|
| `scripts/m0_lab/round07/candidate_runner.py:47` | 具名常量 `DISCIPLINE`（含 `{steps}`） | 15 |
| `scripts/m0_environment/holmes_baseline.py:1134–1138,1145` | 三段内联 `addition` 字面量 + 窗口/服务句 | 24（两变体） |

逐段拆解后，上一轮记录里的全部句数结论都被**独立复算证实**：
基础开场 6 + 证据纪律 7 + 投影语义 8 + 缺失序列 1 + 窗口 1 + 授权服务 1 = 多轮变体 **24 句**；
final-report 变体换开场（同为 6 句）故亦为 **24 句**，与前者共享 **18 句**，并集 **30 句**；
候选臂 6+7+1+1 = **15 句**，且逐字节是多轮变体的真子集；标为 L3 的投影语义正好 **8 句**。

### 收敛前先固定住硬约束

先算出收敛前的真实哈希，再作为测试的期望值：

```
$ .venv/bin/python -c "... DISCIPLINE.format(steps=2) + report_instruction(version=LEGACY_REPORT_VERSION) ..."
DISCIPLINE template sha256 = 1aa8cec328f0575109b03c93d7982deda3e04b122da4243ca93f7d510442b834
steps=2  end-to-end prompt_sha256 = 9648c6deda1aa97d8801b9e3f2518ee3f7a022e28ad24c8d2d0af3a143cd4abc
```

该值与 M0 证据中 `m004-normal-candidate-retry2` 与 `m004-fault-candidate` 两个 Run
记录的 `prompt_sha256` 一致。**收敛后复算不变**，测试从证据 JSON 读回期望值而不是写死。

### 做了什么

新建 `opspilot/instructions/discipline.py`：按**复用边界**把句子收敛成只声明一次的 segment
（开场两种、证据纪律、投影语义、缺失序列、窗口句、授权服务前缀），
再把三个历史变体表述为 segment 的**有序序列**。顺序进结构是必要的——
baseline 把窗口句拼在报告契约**之后**，候选臂拼在**之前**，这个差异是真实存在的。

按 C3 第 5 节的四条 revision 规则实现 `discipline_revision` 与 `prompt_revision`：
内容哈希而非人工编号；只覆盖模板字节、`{steps}` 与授权服务列表以占位符留在模板里；
人类可读前缀加哈希短码；可从代码确定性重算。变体身份进前缀，
避免 final-report 与多轮调查两条路径共用一个版本号而内容不同。

| 变体 | `discipline_revision` |
|---|---|
| `baseline-multi-step` | `l1a-baseline-multi-step-89166c68a0b0` |
| `baseline-final-report` | `l1a-baseline-final-report-8d87a738d81f` |
| `replay-candidate` | `l1a-replay-candidate-bc2730f23697` |

### 哪些收敛、哪些冻结为历史，依据是什么

**历史实验脚本一律保留原字面量，不改成 import。** 依据是 ROADMAP 对模型 profile 切换的
既定做法（「带 allocation id 的历史实验脚本保留原值作为历史记录」），
以及上一轮记录的理由：这两处的 `prompt_sha256` 已冻结在 M0 证据里，
把历史脚本改成引用新模块会改变它们的历史语义。

代价是两处仍有字节副本。该代价由确定性测试承担：
`test_module_constants_still_match_the_historical_script_literals` 断言模块常量与两个脚本里的
字面量逐字节相同，任一处被改动即转红。`holmes_baseline.py` 的字面量用 **AST 结构定位**
（按节点类型与出现顺序）而不是关键词匹配——它运行时依赖 `tmp/` 下被 gitignore 的
HolmesGPT checkout，CI 里不存在；且按内容定位会让「改动被检查的文字」变成
「采集失败」而不是「字节不符」，正好在该转红的时候失去意义。这一点是变异验证发现的（见下）。

按 C3 第 5 节「换一套工具它还成立吗」的判据，投影字段语义 8 句**属 L3**，
在模块里被标记 `tool_specific=True` 并只出现在两个历史 baseline 变体中；
`replay-candidate` 不含任何 `tool_specific` 段。**迁往工具描述本轮不做**——
目的地 `opspilot/tools/registry.py` 尚未进 main，见下方交接项。

### 验证证据

| 检查 | 命令 | 真实结果 |
|---|---|---|
| 开发检查全量 | `make check` | `1078 passed, 77 skipped, 2 xfailed in 26.67s` |
| 新增确定性测试 | `.venv/bin/python -m pytest tests/test_instruction_discipline.py -q` | `28 passed` |
| PG 持久化集成 | `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m1_durable_state_postgres.py -q` | `23 passed`（原 21 + 本轮 2） |
| 冻结哈希复算 | 见上方「收敛前先固定住硬约束」 | 收敛前后同为 `9648c6de…a3cfd4abc`，与 M0 证据一致 |

`make check` 的基线（本分支起点 main `b483a12`）为 `1050 passed, 75 skipped`；
本轮净增 **28 个通过用例**与 **2 个 skipped**（PG 用例未开 `M1_DURABLE_POSTGRES` 时跳过）。

### 变异验证：每条断言都确认过能转红

不做变异就无法区分「断言成立」与「断言恒真」。**16 个单元变异 + 2 个 PG 变异，各自至少让一条测试转红**（16/16）：

| 变异 | 转红的测试 |
|---|---|
| M1 模块里开场改一个词 | 冻结哈希、两处逐字节、漂移检查、revision bump（共 6 条） |
| M2 `candidate_runner.py` 的 `DISCIPLINE` 改一个词 | 漂移检查、候选臂逐字节 |
| M3 `holmes_baseline.py` 的投影段改一个词 | 漂移检查、baseline 逐字节（含无 scope 分支，共 4 条） |
| M4 候选臂窗口句挪到报告契约之后 | 冻结哈希、候选臂逐字节 |
| M5 哈希投影提前把 `{steps}` 填掉 | 模板字节非渲染字节 |
| M6 哈希投影漏掉 `tool_specific` 字段 | 投影字段同步 |
| M7 把 L3 投影段塞进候选臂 | 冻结哈希、逐字节、句数普查、L3 隔离 |
| M8 `discipline_revision` 不再标识变体 | 变体身份 |
| M9 纪律文本混入凭据形态 | 凭据形态（两个变体）＋ 5 条字节断言 |
| M10 `prompt_revision` 不复合 L2 | L2 换版必须 bump |
| M11 `render` 把预算数字当独立句追加 | 冻结哈希、逐字节、模板字节（共 5 条） |
| M12 未登记变体静默返回空序列 | fail-closed |
| M13 候选臂的无条件窗口句被误标为 scoped | 冻结哈希、逐字节、scope 条件性 |
| M14 `render` 回到静默丢弃服务列表 | 无槽位变体收到服务列表须拒绝 |
| M15 `render` 不再校验预算为正整数 | 预算校验（5 个参数化用例） |
| M16 `scoped` 段不再被跳过 | 无 scope 裁剪、scope 条件性 |
| P1 `prompt_revision` 只覆盖 L2 不覆盖 L1a | PG：模板换版必须 blocked |
| P2 `render` 丢掉授权服务列表槽位 | PG：实例值必须真的改变字节 |

**变异验证自身查出两个真缺陷，都已修：**

1. M3 最初以「collection error」而非断言失败收场——`holmes_baseline.py` 的字面量当时按
   关键词定位，改动被检查的文字会让提取器先找不到目标。已改为 AST 结构定位，
   并把提取改为惰性（`functools.cache`），失败落在具体测试而不是整个文件的收集阶段。
2. 「实例值不移动 revision」当时没有任何断言真正承重——`prompt_revision` 的签名本就不收实例值，
   结构上不可能失败。补了 `test_projection_carries_template_bytes_not_filled_values`，
   挡住「把占位符提前填掉」这类真实回归（M5/M11 证明它转红）。

**自测阶段另查出两处 fail-open，已修并补测**（不是审查提出的，是实现者自己探边界发现的）：

1. **给没有授权服务槽位的变体传服务列表会被静默丢掉。** 对 `replay-candidate` 传
   `authorized_services=("checkoutservice",)` 与不传得到**完全相同的字节**。
   调用方会以为查询范围已被限定，而模型拿到的是未限定的纪律。查询范围属 Controller 权限
   （PRODUCT-CONSTRAINTS：*Read identity, exact target resolution, query budgets,
   cancellation and human control decisions are scoped outside the model's authority*），
   不能悄悄落空。已改为 `ValueError` fail-closed（变异 M14 证明转红）。
2. **预算轮次不校验**：`model_requests=0 / -1 / "many"` 都被接受，会拼出
   `You have at most -1 model requests` 并照常送进模型。已改为「必须是正整数」，
   并按仓库既有 `validate_max_http` 的写法用 `type(x) is not int` 连 `bool` 一起拒
   （`True` 会拼出 `at most True model requests`）。变异 M15 证明转红。

同时把「无 scope 时窗口句整段不出现」从**渲染后按后缀裁剪**改为**结构判定**
（`Segment.scoped`）。原写法只要 segment 顺序一变，裁剪就静默失效，
失效的表现是送进模型的字节错了却没人报错。改后候选臂那句**无条件**的窗口句不受影响——
同一段文字在两类变体里条件性不同，这一点必须保住（变异 M13、M16 分别证明两侧都转红）。
`scoped` 已同步纳入哈希投影，故三个 `discipline_revision` 短码相应变化（上表已是新值）。

另有两个变异（P1、P2）最初没转红，复查确认是**变异构造得不对**而非测试有洞：
`prompt_revision` 的变体 id 同时出现在前缀与哈希载荷里，只去掉一处不足以让两个变体撞号；
P2 的两次渲染同时差在预算与服务列表，一个差异掩盖了另一个丢失。
改成忠实变异后两条均转红，PG 用例也相应改为**两类实例值分别断言**。

### C3 第 7 节七项确定性检查的逐项状态

上一轮记录把七项检查列为「实现时的验证设计，待实现任务承接」。本轮逐项结论：

| # | 检查 | 本轮状态 |
|---|---|---|
| 1 | 版本绑定三句（L3a fingerprint / L1a+L2 golden hash / 仅实例值不同须同号） | **部分完成**：后两句已实现（冻结哈希 + 实例不变性 + PG 用例）；L3a fingerprint 一句依赖注册表，未做 |
| 2 | `ToolRegistry` fingerprint 的工具层与参数层投影须覆盖 `description` | **未做**：依赖 `opspilot/tools/registry.py`（PR #20，未进 main） |
| 3 | `ToolDescription` 五字段结构完整性，注册期 `ToolContractError` | **未做**：同上 |
| 4 | 凭据形态正则 + `RESERVED_PARAMETERS` 参数键检查 | **部分完成**：凭据形态正则已对 L1 全部变体实现（形态匹配，非关键词）；参数键检查依赖注册表，未做 |
| 5 | `versions` 不一致的续跑进 `blocked(INCOMPATIBLE_STATE)`，需补 prompt/tool 维度 | **部分完成**：prompt 维度已补（真实 L1a 模板哈希）；tool 维度依赖注册表，未做 |
| 6 | 来源索引覆盖 L1 全集每一句且按变体校验 | **不实施，须用户确认**：见下方「发现的合同冲突」 |
| 7 | 三类变化各走各的机制 | **部分完成**：合同变更与实例变化两行已有 PG 断言；授权收紧一行以 domain 层断言覆盖（`check_scope_versions` 抛 `CONTROL_CONFLICT` 而非 `INCOMPATIBLE_STATE`，且授权范围变化不触动 `prompt_revision`）；目标重新绑定一行未做 |

### 发现的合同冲突（须用户裁定，本轮不自行选择版本）

**第 7 节第 6 项（来源索引静态检查）与已批准的归位决定相冲突。**

- 第 6 项要求：来源索引以结构化数据为载体、与 L1 单一来源模块同处存放，静态检查覆盖
  L1 全集 30 句每一句的来源字段。
- 但上方「未并入 C3 的材料及去向」记录的裁定是：L1 来源索引
  **不并入**，理由为「取材自 M0 实验脚手架，用户已明确该批内容无产品参考价值」。

即：第 6 项要求把来源索引做成产品模块里的结构化数据并加静态检查，而归位决定已判定该批内容
不进产品。七项检查所在的独立提案文件已删除，其第 6 项**未随 C3 第 5/8 节并入**，
因此在当前权威来源（C3 正文）里没有对应条款。

本轮按 AGENTS.md「权威来源冲突时指出具体冲突，在有意义的决策边界暂停依赖工作，
不选择更方便实施的版本」**暂停该项**，不实施也不删除要求，交用户裁定：
是承接第 6 项（需推翻「不并入」的裁定），还是确认第 6 项随独立文件一并作废。

### 交接：`ToolRegistration.description` 待 PR #20 合并后承接

C3 第 8 节「模型可见面」的五字段结构体（`returns` / `window_format` / `values_format` /
`limits` / `cannot_prove`）与第 7 节第 2、3、4（参数键）项，
依赖 `opspilot/tools/registry.py` 的 `ToolRegistration` 与 fingerprint 投影。
该文件在 **PR #20（`feature/m1-01-tool-executor`，经 `gh pr list` 核实仍为 OPEN）分支上，未进 main**。

本轮**不动 PR #20 的分支，也不在 main 上另建竞争的 registry**。承接条件与内容：

1. 前置：PR #20 合并进 main。
2. 在 `opspilot/tools/registry.py` 的 `ToolRegistration` 上增加 `ToolDescription` 结构体字段，
   注册期断言结构完整性（`returns`/`limits`/`cannot_prove` 非空，
   `window_format`/`values_format` 含占位符），失败抛 `ToolContractError`。
3. 把 `description` 与 `ParameterSpec.description` 纳入 fingerprint 的**工具层与参数层两处投影**
   （上一轮记录的落差 2：两处投影都会静默丢弃新字段），并分别断言。
4. 把本模块里标记 `tool_specific=True` 的投影语义 8 句迁往对应工具描述。
   **迁移会改变未来 Run 的 L1a 模板字节，须 bump `discipline_revision`**；
   历史脚本与其冻结哈希不受影响（历史变体常量不动）。
5. 补第 7 节第 1 项的 L3a fingerprint 一句、第 5 项的 tool 维度用例、
   第 7 项的「目标重新绑定」一行。

### 本轮未做与限制

- 未改产品运行行为：新模块目前无产品调用方，`ModelProfile.prompt_revision` 的**接线仍未完成**
  （上一轮记录的落差 1）。接线属 M1-01「Flash 调查 loop」子任务，本轮只提供可确定性重算的来源。
- 未改历史实验脚本的任何字节，未改 SPEC 门槛陈述、ROADMAP 状态判断、
  `feature_list.json` 的 `passes`、预算冻结值。
- `holmes_baseline.py` 的端到端 `prompt_sha256`（`023dae70…`）**无法在 CI 复算**：
  它覆盖 HolmesGPT 外层模板，依赖 `tmp/` 下被 gitignore 的固定 checkout。
  本轮冻结的是该脚本的 L1/L2 拼装字节（AST 提取比对），不是它的端到端哈希。
  可端到端复算并已冻结的是候选臂的 `9648c6de…`。
- 本轮未启动任何长期进程或服务，未发起任何模型调用或外部付费调用，未知费用为 0。
