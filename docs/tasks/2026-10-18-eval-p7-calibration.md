# 评测 P7 judge 真实校准

- 状态：进行中（合同 r1 已裁决，待实施）
- 目标：按 [评测任务记录](2026-10-12-eval-suite.md) P7 与 D16，用 36 份基于真实 Run 的样本、两份独立盲标和用户裁决，对 GLM-5.3、GPT-6.1 Sol、`deepseek-flash` 做一次正式校准，得出 D16 选择；合格后才进入第一轮基线（另记）。
- 依据：[PR-B3 接口](2026-10-14-eval-pr-b3-interface.md)（LabelPack、freeze、run_calibration、r8–r11）；[PR-A 接口](2026-10-13-eval-pr-a-interface.md)（JudgeInput、JudgedDiagnosis）；C3 第 388–398 行。
- 范围外：改判分规则、匹配口径或 D16 选择认证（如需改动，单列合同变更）；调查 loop、报告生成与恢复路径；翻任何 `passes`。

## 合同（r1，2026-10-18）

预审：Codex gpt-6-astra，全新上下文、只读，对照 main `57dd8eb`。用户裁决 4 项，其余按用户常设指示采纳推荐。

| 编号 | 裁决要点 | 来源 |
|---|---|---|
| P7-D1 | 允许受控改写：真实 Run 原件与改写派生件混合。派生件标为「基于真实 Run 证据的受控改写」，私有记录绑定源 Run、原报告 hash、改动与用途 | 用户 |
| P7-D2 | 来源族按原始录制及其全部派生闭包划分（replay、改写、换 ID 同族）；每场景至少两个独立 live 来源族，一族供 2 debug，一族供 4 validation | 推荐 |
| P7-D3 | 真实模型 replay 可在族内补报告，但不增加独立来源；结构重放不产样本 | 推荐 |
| P7-D4 | 改写只动报告层（结论、引用关系），只引用该 Run 实际交付的证据；不伪造 span、窗口、hash 或 Outcome；`input_hash` 由实际 P6 投影重算，不靠空白或 ID 噪声凑唯一 | 推荐 |
| P7-D5 | 每场景 validation 1 正 + 3 负（共 6 正 18 负），debug 1 正 + 1 负；最终标签由盲标与裁决决定，作者不能指定 | 推荐 |
| P7-D6 | 七类覆盖指 gate 合格、诊断语义错误的样本，不用坏 schema 或伪 ID 代替；正常场景的纯假设不是负例；作者对每项写覆盖说明，供独立复核 | 推荐 |
| P7-D7 | 改写作者与两位标注者分离。标注者拿到 P6 输入、expected answer、签名规则结果、判据与输出 schema；拿不到作者意图、coverage、split、来源关联、另一份标注和候选 judge 输出 | 推荐 |
| P7-D8 | 两位标注者：Claude Opus 5.5 全新子 Agent 与 Codex `gpt-6-astra`，记录实际模型身份；均非候选 judge | 用户 |
| P7-D9 | 保持现行严格口径（完整标签、`(location, quote)` 精确相等）；debug 阶段给标注者与三家 judge 同一份引文粒度与排序约定；debug 仍大量误判则停下交用户改规则 | 用户 |
| P7-D10 | 用户逐项裁决所有完整标签差异（最坏 36 项）；工具生成最终 BlindLabel 并绑定两份 annotation hash 与裁决记录 | 推荐 |
| P7-D11 | 先只对 debug 项调 `judge_once` 调优，冻结 prompt/config 后才跑正式校准；validation 暴露后修 judge 须换新的 validation 来源族 | 推荐 |
| P7-D12 | 校准与第一轮基线来源分离，先校准后基线 | 用户 |
| P7-D13 | 新增 `chore` 评测执行 PR：capture→CalibrationItem 导入、改写记录、盲标包导出与导入、差异裁决、debug-only 调用、正式 freeze/run/read-back、费用记录；不改判分、匹配、认证与数据出口 | 推荐 |
| P7-D14 | 结果为 pending（需选 Sol）时停在 pending 交用户，认证入口另行实现，不手改 selection | 推荐 |

可逆默认（lead）：12 个来源一律新采（`--scenario all --n 2` 一轮），不复用 PR-C 与 #273 的两次 payment Run，理由是同一代码版本、一次覆盖六场景 live 路径，代价约 20 分钟 lab。

## 计划

1. 执行驱动 chore PR（P7-D13）：Opus 实现，sol 按接口写验收测试，astra 审。
2. 来源采集：协调 lab 后跑 12 次 live（约 2 小时），原件放 ignored 私有目录。
3. 改写：独立作者 Agent 按 P7-D4–D6 生成派生件与覆盖说明。
4. 盲标：Claude 子 Agent 与 astra 各标 36 项；差异简报交用户裁决。
5. debug 调优（P7-D11），冻结后正式校准 180 次，读回认证。

## 证据

- 预审决定表原文保存在私有目录，未入库；本节随执行补充。

## 待决

- 无。
