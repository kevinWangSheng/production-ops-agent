# #676 下一轮提示词发布：事件读法、重启找触发配置、summary 措辞、告警背景

- 状态：进行中
- 更新日期：2026-11-16
- 依据：issue #676；#664（F3 第 2 步 summary 一致性）；#310（调查方法指导）；全量证据 [lite-full-1116](../evidence/itbench-lite-sre-lite-full-1116/run.md)；上一任务 [#646](2026-11-15-itbench-lite-root-cause.md)
- 工作区：实现 `feature/676-investigation-guidance`（`../poa-676-impl`）；测试 `test/676-investigation-guidance`（`../poa-676-tests`）

## 目标与范围

把 lite-full-1116 独立失败分类里可通用修复的三处（事件只在应用命名空间读、模板未变的重启被当根因、入口告警前已有的背景错误被当范围）与 #664 的 summary 措辞打包成一次提示词发布。只动 L1 `KUBERNETES_GUIDANCE` / `INVESTIGATION_METHOD`、共用 L2 报告段和 K8s 工具描述；不改权限、驱动、报告校验，不写环境特定知识。

范围由全新上下文决策者定（不做项及理由见 #676），lead 在租机快照上补核：故障注入的描述性事件在应用以外的命名空间，应用命名空间只有以 Pod 命名的对象。

## 前提与完成条件

- 前提：用户 2026-11-16 授权 lead 自行分析并修复（分析、取舍、审查用隔离上下文）；提示词改动由 Agent 决定（租机评测惯例）。
- 完成条件：本机测试全绿（含新增验收与合同测试，由未参与实现的 Agent 编写）；独立审查完成；租机冒烟 2 题各 2 次过行为门 → F3 第 3 步 D9 + 第 5 步两 Run 两名审查者判定（不过即停，不上全量）→ 集成批 8×2 四门 → 全量 105；F3 `passes` 翻转由用户裁决。

## 合同（r2）

完整决定表见 [2026-11-16-676-investigation-guidance-contract.md](2026-11-16-676-investigation-guidance-contract.md)。全部按预审推荐，lead 依用户授权定；唯一用户门项 D8-b（确定性 summary 检查）选不做。

- D1 全集群读事件在现有授权内，无需扩权。
- D2/D3 `involvedObject.kind` 与分组示例写进 L3 工具描述（jq/count 共用段与 tabular 加「事件例外」），L1 只用大白话；不改工具输出合同。
- D4 不动 #628 的 RESULT_TOO_LARGE 句，K8s 段另加一句「事件太大先分组计数，不按命名空间或 type 收窄」。
- D5 本机测试只保证聚合查询不超限。
- D6 模板未变的重启是机制、不是施加者；「只有事件支撑用 partial」放 K8s 段。
- D7 「偏离默认」改为「名称或取值表明注入故障、错误、延迟或失败的设置」；不写「优先于遥测」。
- D8 第 4 项只改提示词：认定原因的 claim 是 hypothesis 时 conclusion 用 partial。
- D9 第 4 项进共用 L2，第 5 项扩写 INVESTIGATION_METHOD 现有句，第 1、3 项只进 KUBERNETES_GUIDANCE；base revision 变，F3 必须重跑。
- D10 实现者只改钉值，不改断言逻辑；base 的 tool_schema_revision 不变。
- D12–D14（r2，租机后）：L2 根因句改为「supported 需要 fact claim 点名该对象，单有 hypothesis 不够」；「告警是入口」句移到仅 ext；#678 no_data 句并入。见决定表 r2 节。
- D11 旧证据按 #653 惯例移到 `prompt-a6b4dfd02030/`，3 条 F3 证据测试在租机证据入库前失败，证据与代码同一 PR。

## 进展

- 2026-11-16：独立失败分类、范围决策、合同预审完成；开实现与测试 worktree。
- 2026-11-16：实现提交。base `prompt-replay-candidate-14eb2a0a6afd`（原 a6b4dfd02030），ext `prompt-replay-candidate-k8s-f09a033198ca`，ext tool_schema `otel-demo-ext-c63f058afb18`（原 d467c4b436b0），base tool_schema `otel-demo-a3a95a0f888e` 不变；实现期间新冒出的合同问题 0。自定一项：D9 第 5 项在 INVESTIGATION_METHOD「无关错误」句后另起一句，不拉长原句（句短便于匹配）。本机 swap 紧，全量 acceptance 探针跑到约 460/3876 条后停（慢在无关评测测试），改跑引用 revision/报告契约/K8s 工具的 134 个测试文件。
- 2026-11-16（租机，代码 `e8b98507`）：冒烟 `lite-smoke-676`（S19、S15 各 2 次，[记录](../evidence/itbench-lite-sre-lite-smoke-676/run.md)）：门② ③ 过，门① 不过（S19 t2 被产品拒收；只读重放为 `cites_non_ok_view`，即引用 no_data 的日志视图，加上 id 抄错）。lead 裁决按基线偶发率继续。F3 第 3 步 D9：Run `b3a2e9f0`，inconclusive，U6 通过。第 5 步两次：Run `a10cf725`（partial，根因 checkout）、`f3b73c46`（partial，根因 payment），确定性判定全过、15/15 测试通过；两次都没有否定这次部署，rubric 由审查者判（[F3 live](../evidence/f3-acceptance-live/run.md)「#676 重跑」）。集成批 `lite-batch-676`（[记录](../evidence/itbench-lite-sre-lite-batch-676/run.md)）：门① 不过（`REPORT_INVALID` 2/16；重放为缺 `next_steps` 后重试时 id 截断，或 `cites_non_ok_view`）；② 12/14；③ 过；④ 结论移向 partial（supported 3/14，#646 批为 12/15），root_cause_entity_pass 4/16（#646 批为 3/16）；附加门不触发。是否上全量由 lead 定。
- 2026-11-16：r2 实现（D12–D14、D11 搬迁）。base `prompt-replay-candidate-8688f6e52275`，ext `prompt-replay-candidate-k8s-0b066c7cd6d1`；ext tool_schema `otel-demo-ext-c63f058afb18` 与 base `otel-demo-a3a95a0f888e` 不变；base 的 discipline revision 回到 #646 的 `l1a-replay-candidate-9a45deb526fe`（INVESTIGATION_METHOD 逐字恢复 83b3b968）。#676 第一版 F3 第 5 步与 f3-acceptance-live 的 run-1/run-2 移到各自 `prompt-14eb2a0a6afd/`。新冒出的合同问题 0。

## 待决

- F3 `passes` 是否改回 true：租机重验后由用户按两名审查者结论裁决。
