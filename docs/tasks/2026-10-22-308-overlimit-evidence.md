# #308 超限结果存为未采纳证据 + 按 ID 读片段

- 状态：合同 r1 已裁决；PR1 实施中，PR2 待 #318（#307 A）与 #322（#309）合并后开工
- 更新日期：2026-10-22
- 依据：issue #308（含更正评论）；C3 §5「大结果保存为持久证据，通过 ID 和片段读取」；`docs/research/upstream-design-audit-2026-10-22.md` 第一、三节；[合同 r1](2026-10-22-308-overlimit-evidence-contract.md)
- 工作区：PR1 实现 `/Users/shenghuikevin/dev/AI/poa-308`（`feature/308-overlimit-evidence`）；PR1 独立测试 `/Users/shenghuikevin/dev/AI/poa-308-tests`（`feature/308-overlimit-evidence-tests`）

## 目标

PR1：视图超过 25k token 被拒时，把脱敏后的 raw 与拒绝视图登记为 `adopted=false` 的证据，模型视图逐字节不变、不升 revision。PR2：拒绝视图带 evidence_id 与基数摘要，新增 `read_evidence(evidence_id, offset, limit)`。

## 合同

预审：全新上下文 Claude Opus 5.5 子 Agent，只读，基线 `aa57f9a`。U1–U7 由用户 2026-10-22 按推荐裁决；R1–R10 为可逆默认。

## 执行进展与证据

（待填）

## 待决

无。
