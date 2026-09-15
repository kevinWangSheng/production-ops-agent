# 调查指令与工具接口合同

- 状态：PR 已就绪，待用户审核合并（PR #24）
- 更新日期：2026-09-15
- 依据：[SPEC.md](../../SPEC.md)「Model priority and design ownership」「Verification and delivery」；
  [C3](../design/technical-proposal-2026-09-07.md) 第 5、7、8 节；
  [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)；[ADR-0002](../adr/0002-context-driven-investigation.md)。
- 工作区：`chore/prompt-tool-contract` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-prompt-tool-contract`
- PR：[#24](https://github.com/kevinWangSheng/production-ops-agent/pull/24)。
  交付状态以 PR 当前 HEAD 为准，不在此复制提交哈希（避免记录随推送失效）。
  收尾时的状态：最新提交的 `checks` 与 `m0-postgres` 均 success，
  机器人审查 thread 全部已回复并 resolve，`mergeStateStatus` 为 `CLEAN`、`mergeable` 为 `MERGEABLE`。
  **合并由用户审核后执行，本任务不自动合并。**

## 目标与范围

产出[C3 第 5/8 节](../design/technical-proposal-2026-09-07.md)：
定义模型可见文字（L1 调查纪律 / L2 报告契约 / L3 工具描述）的归属、版本生成规则、
必填项与来源索引，并按用户要求核对 DeepSeek 官方文档后写入供应商绑定事实。

范围界限：仅新增设计提案文档与导航/任务记录。
不改产品代码、不改实验脚本、不做模型切换、不改 SPEC/ROADMAP/`feature_list.json`/门槛陈述/预算冻结值。

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
| 七项确定性检查 | 保留在本任务记录 | 实现时的验证设计，待实现任务承接 |

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

1. 用户裁决提案第 5 节 U1（`deepseek-v4-flash` 请求别名的弃用风险如何处置）与
   U2（并入 C3 还是保持独立文档）。
   U1 **不影响既有 M0 证据或冻结的校准值**（见提案 4.3 末：分界不落在 ROADMAP B2 校准集内）；
   待裁决的只是别名何时切换与既有 provider-identity 决定的依据是否按 Change Log 更新。
   现 §5 表只有 U1、U2 两项。原编号 U2「是否采用 strict tool mode」已整条撤销
   （C3 第 5 节已定默认关闭 strict beta，不是待决项），原 U3 顺延为现 U2。
2. 裁决后再实施合同本身（第 2 节的 `ToolRegistration.description`、第 7 节的确定性测试），
   并按提案第 6 节的方式收敛两份 DISCIPLINE——新建单一来源模块并保留字节完全相同的带版本常量，
   使 `prompt_sha256` 不变；本任务不执行该收敛。
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
