# 首次小规模 Holdout Blind 结果

执行日期：2026-09-12。依据 [`round-07-holdout-contract.md`](round-07-holdout-contract.md)。容器镜像无宿主挂载；开发目录、答案文件和凭据路径拒绝/不存在证据见 [`round-07-container-isolation-refusal.txt`](round-07-container-isolation-refusal.txt)。K8s RBAC 仍环境缺测。

## 执行摘要

| 场景 | 独立 packet hash | HTTP | 容器结果 | 状态 |
|---|---|---:|---|---|
| normal 变体 | `7b4f5d19c6b73d90a3e2e97ae98bf53192aca5e1c74022269282533cf9982de5` | 2 | `final_returned` | 待独立 evaluator 汇总 |
| fault 变体 | `ac737766a1d3ddb1cc3b0cb9dcec0d43612290c903c979b9910b67555659a9e1` | 2 | `final_returned` | 待独立 evaluator 汇总 |

候选结果只在无宿主挂载容器内生成；本主会话不读取 holdout 答案/注入参数。封存 ledger 见 [`round-07-holdout-ledger.json`](round-07-holdout-ledger.json)，4 HTTP、known cost upper 0.133803 CNY、trace 0，未超 6 CNY 上界。

## 独立评分

由生成变体的全新上下文 evaluator 按冻结 rubric 评分；完整 response/tool 工件已移至 ignored `tmp/holdout/result-normal-r07/` 与 `tmp/holdout/result-fault-r07/`，主会话只接收不含案例身份、答案或注入参数的汇总，见 [`round-07-holdout-blind-independent-summary.md`](round-07-holdout-blind-independent-summary.md)。本轮是首次小规模盲测，不是正式保留集评测，不产生泛化结论。
