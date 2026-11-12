# F1 第 5 步判分流程校准：冻结登记（#295，路径 B1）

本目录是盲标开始前的冻结登记。此刻没有任何盲标标签、裁决或对照结果。任务记录见 [2026-11-11-f1-judge-recal.md](../../tasks/2026-11-11-f1-judge-recal.md)。

| 文件 | 内容 |
|---|---|
| `freeze.json` | 门槛、分母规则、9 份非盲裁决的 attempt id、盲标说明与盲标包各文件的 sha256、冻结的裁判身份，以及考试归档还原核对的计数结果 |
| `freeze.json.sha256` | `freeze.json` 的登记 hash；`labels` 与 `compare` 先核对传入的冻结文件与它一致 |
| `instructions.md` | 盲标说明全文。两句问题逐字取自判分代码里的 Q9 问题（`opspilot/evaluation/binary_judge.py` 的 `FAULT_QUESTION`、`NORMAL_QUESTION`），三条标准逐字取自已公布的 `f1-s10-scoring-2026-11-06/summary.json` 的 `disagreements.standards`；「Mechanics」一节只写格式与独立作答，不含判据 |
| `reconstruct_check.py` | 前提 1 的核对脚本：不调用裁判，从考试归档重建 111 份裁判输入，与正式判分的 `items.json` 和已公布摘要逐份比对 |

不入库：盲标包正文（报告原文、期望要素、注入 canary）、身份映射 `identity-map.json`、之后的标签与裁决。它们在仓库外私有目录 `~/.agent/eval-probes/f1-judge-recal-2026-11/`，沿用考试判分的私有目录惯例。

`scripts/eval_judge_recal.py` 的 `labels` 与 `compare` 先核对冻结文件与登记 hash、盲标包与冻结文件登记的 sha256，以及包内条数（111）、流程分母（102）和其余恰为登记的 9 份裁决，任一不符就拒绝运行；`export` 拒绝写进任何 git 工作树，目录 0700、文件 0600。
