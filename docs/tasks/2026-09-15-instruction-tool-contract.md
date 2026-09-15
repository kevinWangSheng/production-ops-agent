# 调查指令与工具接口合同

- 状态：进行中
- 更新日期：2026-09-15
- 依据：[SPEC.md](../../SPEC.md)「Model priority and design ownership」「Verification and delivery」；
  [C3](../design/technical-proposal-2026-09-07.md) 第 5、7、8 节；
  [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)；[ADR-0002](../adr/0002-context-driven-investigation.md)。
- 工作区：`chore/prompt-tool-contract` @ `/Users/shenghuikevin/dev/AI/production-ops-agent-prompt-tool-contract`

## 目标与范围

产出[调查指令与工具接口合同（提案）](../design/instruction-and-tool-interface-contract-2026-09-15.md)：
定义模型可见文字（L1 调查纪律 / L2 报告契约 / L3 工具描述）的归属、版本生成规则、
必填项与来源索引，并按用户要求核对 DeepSeek 官方文档后写入供应商绑定事实。

范围界限：仅新增设计提案文档与导航/任务记录。
不改产品代码、不改实验脚本、不做模型切换、不改 SPEC/ROADMAP/`feature_list.json`/门槛陈述/预算冻结值。

## 前提与完成条件

- 前提：只读核查仓库与官方文档，无模型调用、无外部付费、未知费用为 0。
- 完成条件：三条落差各有仓库内证据；来源索引每条 L1 约束可回指具体记录；
  DeepSeek 事实均引官方页面；冲突与决策点上报用户而不自行选择版本。
- 不在完成条件内：用户对 U1–U3 的决定、合同的实现与实现后的确定性测试。

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
   `candidate_runner.py` 的 15 句是 `holmes_baseline.py` 24 句的真子集，0 句独有。

2. **来源索引已建立**，见提案第 3 节：11 条 L1 约束逐条回溯，
   区分规范派生 / 失败派生 / 设计派生。其中
   `The authorized query window is fixed by the trusted runner` 查无失败来源
   （`docs/evidence` 中无 `tool arguments denied` 记录，round-07 `tool-calls.json` 的 params 均为 `{}`），
   标注为设计派生。

3. **DeepSeek 官方事实已核对**（2026-09-15，均为 api-docs.deepseek.com 一手页面），见提案第 4.1 节。

4. **发现并上报模型代际冲突**，见提案第 4.3 节。官方 2026-09-10 声明 V4 Flash 已退役、
   `deepseek-v4-flash` 临时路由到 V4.1 Flash。仓库实测一致：

   ```
   round-07-upstream-runs/*/response-{1,2}-business.json  共 8 份
   created 1789241365 → 2026-09-12T19:29:25Z
   请求名 deepseek-v4-flash，响应 "model": "deepseek-flash"
   ```

   `scripts/m0_environment/round03.py` 的 `reported_models` 同时接受两个名称，故未被拦下。
   该冲突牵连 SPEC 门槛引用的冻结 v4 验收包与 ROADMAP B2 冻结的校准值，
   **处置属用户决定，本任务不选择版本**。

### 验证

| 检查 | 命令/方式 | 结果 |
|---|---|---|
| 提案内引用的评审原文 | 逐条 `sed -n '<行>p'` 核对 9 处 | 全部命中，引文与原文一致 |
| 仓库内模型回报名统计 | 遍历 `docs/evidence/**/*.json` 取 `model`+`created` | 8 份带时间戳响应全为 `deepseek-flash` |
| 硬编码模型名范围 | `grep -rl deepseek-v4-flash`（排除 docs/.venv） | 28 个文件；本次一个未改 |
| 内部链接与锚点 | 见下方链接检查 | 通过 |

**未执行**：`make check`。本次为纯文档新增，未触及 Python 代码；
该 worktree 无 `.venv`，运行需 `make setup` 下载依赖，与改动不相称。

## 下一步与交接

1. 用户裁决提案第 5 节 U1（`deepseek-v4-flash` 请求别名的弃用风险如何处置）与
   U2（并入 C3 还是保持独立文档）。
   U1 **不影响既有 M0 证据或冻结的校准值**（见提案 4.3 末：分界不落在 ROADMAP B2 校准集内）；
   待裁决的只是别名何时切换与既有 provider-identity 决定的依据是否按 Change Log 更新。
   原「strict tool mode 是否采用」已撤销：C3 第 5 节已定默认关闭 strict beta，不是待决项。
2. 裁决后再实施合同本身（第 2 节的 `ToolRegistration.description`、第 7 节的确定性测试），
   并按提案第 6 节的方式收敛两份 DISCIPLINE——新建单一来源模块并保留字节完全相同的带版本常量，
   使 `prompt_sha256` 不变；本任务不执行该收敛。
3. 独立审查已完成（全新上下文 Agent，未参与撰写），提出 F1–F8 共 8 条发现，
   全部已复核并处置，逐条记录见下方「独立审查与处置」。
4. 本任务未启动任何进程或服务，无未提交的用户 WIP。
   worktree `chore/prompt-tool-contract` 在 PR 合并且确认整合后再清理。

## 独立审查与处置

审查者以全新上下文启动，仅获目标、约束、批准合同与待审工件，未获实现者结论。
8 条发现全部采纳，其中 2 条我独立复核后修正了审查自身的论据错误。

| # | 发现 | 判定 | 处置 |
|---|---|---|---|
| F1 | §0.3 漂移方向写反 | 成立 | 逐句差集确认 candidate 15 句是 baseline 24 句真子集、0 句独有；重写为「L1 混入 L3 内容」 |
| F2 | start/end 约束误判为「无失败来源」 | 成立 | 来源在 `investigation-outcome-review`（窗口被抄早 0.533447 秒，无错误码）；§3 重建并新增 3.1 回溯方法 |
| F3 | §4.3 漏掉仓库已有处置 | 部分成立 | `round-02-provider-identity-decision` 及其独立审查已处理；§4.3 重写为「别名弃用时钟」，删去无证据的「校准横跨代际」。**审查的支持论据「无响应回报过 deepseek-v4-flash」经复核为误**（实有 6 处），但其结论仍成立，已另循 ROADMAP B2 取材路径证实 |
| F4 | §1.1 规则 2 与规则 4 互斥；权限收紧会强制取消在途调查 | 成立 | L3 拆为 L3a 模板 / L3b 实例，新增 §1.3 |
| F5 | §2.3 禁止项与 D1/D3 冲突，且误用 `RESERVED_PARAMETERS` | 成立 | 拆为 (a) 保密 / (b) 权限，§7 第 4 项改为凭据正则 + 参数键检查 |
| F6 | §0.1 承重前提是推断 | 部分成立 | 三分支搜索确认绑定未实现；更正为「字段已声明、栅栏已实现、绑定未实现」，并据此加强论点；§1.2 补记 blocked 唯一出路是 cancel |
| F7 | strict mode 被当作待决项 | 成立 | C3 第 5 节已定默认关闭 strict beta；§2.4 重写，U2 撤销 |
| F8 | 压缩器 `reasoning_content` 被称「未决张力」 | 成立 | C3 第 5 节已有「不兼容则阻塞交接，不猜测删除协议字段」；§4.2 改为实现核对项，不需用户决定 |

另处置两点保留：任务记录的 hold 范围已按 F3 结果收窄；§1/§2 补「批准前不生效」限定。
§7 编号顺序错误（自引入）已修。
