# #309 调查循环：压缩后重置去重、同轮工具有限并行

- 状态：合同 r1 已裁决，实施中
- 更新日期：2026-10-22
- 依据：issue #309；C3 §5、§13（工具并行数 2）；`docs/research/upstream-design-audit-2026-10-22.md` 第二、三节；[合同 r1](2026-10-22-309-loop-dedupe-parallel-contract.md)
- 工作区：实现 `/Users/shenghuikevin/dev/AI/poa-309`（`feature/309-loop-dedupe-parallel`）；独立测试 `/Users/shenghuikevin/dev/AI/poa-309-tests`（`feature/309-loop-dedupe-parallel-tests`）

## 目标

压缩被接受后清空同工具同参去重集合；同一轮多个工具调用有限并行（默认 2，`OPSPILOT_TOOL_PARALLELISM`），按 index 提交，模型可见内容不变，cap=1 与改动前逐字节等价。

## 合同

预审：全新上下文 Claude Opus 5.5 子 Agent，只读，基线 `0b4b820`。U-1–U-5 与 R-1–R11 按推荐采纳（用户 2026-10-17 常设指示；默认 2 与 C3 §13 一致，不改 C3）。

## 执行进展与证据

（待填）

## 待决

无。
