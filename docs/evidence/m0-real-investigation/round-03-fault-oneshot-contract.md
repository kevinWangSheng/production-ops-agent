# M0-03 一次完整故障案

一次做完：注入 → 保持 300s → 独立观察失败 checkout trace → 前提成立才发 Holmes（最多 4 HTTP）→ restore。不是「先观察不发模型」的单独小实验。

依据 v4：故障案须先有 ≥2 条本窗失败 checkout trace，再给调查者症状；调查者不看注入答案。LangSmith 上传不是本步前提。

- 环境：`m0-otel` 已 Running；`paymentFailure` 当前 off
- 注入：`development_fault.py inject --experiment-id m003d-fault-01`（环境 worktree）
- 账本：现有 `m0-03c`，fault 余 4 HTTP
- 停止：前提不齐则不发模型；到 round03 deadline 或预算尽则停
- 旧失败分母不改写
