# Holmes 162 复验：带常驻指令跑 3 遍（#780，租机 2026-11-25）

这是在租机上做的有界真实运行，不是生产证明。只跑 OpsPilot 侧，Holmes 侧没有重跑。DeepSeek 真实调用（`deepseek-flash`），judge 用 Z.ai `glm-5.3-flashx`（strict），`--trace lab`。

- 主机 `opspilot-tool-1`。代码在独立检出 `~/poa-162`，即 `origin/main` `c6810a2c`（含 #847 常驻指令与 #854 eval 驱动导入）；`make setup` 退出 0。上游 Holmes `a0009195`。
- 环境：从主检出用 `kind_lab.py up` 拉起 kind otel lab，`k8s-token` 重发 token，health ok，frontend span 速率 1.74/s。开跑前集群里没有 `app-NNN` 命名空间，也没有其他评测进程；`kind-sregym` 闲置，没有动它。
- 命令：主机脚本 `/home/opspilot/rental-logs/162/run162.sh`（不入库），内容是 `scripts.holmes_evals.driver round --round r1124-162 --questions 162_get_skills --trials 3 --trace lab --k8s-env-file …`，端口从 35000 起，跑完接 `driver scores`。轮次 07:16:30–07:25:24 UTC，两步都退出 0。
- 导入：每遍开跑前，驱动把题目的 `global_instructions.json`（294 字节，`fits: true`）经工作台提交并批准为目标的常驻指令；题目结束后与 runbook 一起撤销。导入前活跃条目为空，导入后只有这一条。

## 结果

0/3。三遍的 Run 都 `completed`，judge 都正常完成（category `ok`），选的都是 B，都判不通过。

| 遍 | Run | 模型请求 | 常驻指令进了 Run 输入 | prompt_revision | trace |
|---|---|---|---|---|---|
| 1 | `0840ed66` | 13 | 是：`run_fact` = 导入的 entry `aa861359`，revision 1 | `…-8d01461bc500` | [t1](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f5ad5d6d-16e3-470c-a811-c5ea59c4394d/r/00000000-0000-0000-5fdb-a756b113a51b?poll=true) |
| 2 | `a6173706` | 10 | 是：entry `7a445f4c`，revision 1 | `…-8d01461bc500` | [t2](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f5ad5d6d-16e3-470c-a811-c5ea59c4394d/r/00000000-0000-0000-e86f-672582669b2b?poll=true) |
| 3 | `9c6611ff` | 13 | 是：entry `15b11152`，revision 1 | `…-8d01461bc500` | [t3](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/f5ad5d6d-16e3-470c-a811-c5ea59c4394d/r/00000000-0000-0000-96d7-f6ade2cadf5a?poll=true) |

- 常驻指令：驱动在每遍里核对 Run 的 `standing_instructions` fact 与导入条目一致，不一致就报 `STANDING_INSTRUCTIONS_MISMATCH`，本轮没有出现。核对结果记在各遍 summary 的 `holmes_eval.knowledge.standing_instructions`。disclosures 里有 `GLOBAL_INSTRUCTIONS_IMPORTED_REVIEWED`。
- prompt_revision：三遍 Run 行的 `versions` 都是 `prompt-replay-candidate-k8s-8d01461bc500`。在本检出上，`prompt_revision_versions("replay-candidate-k8s", knowledge_tool=True, standing_instructions=True)` 算出的也是它；不带常驻指令时是 `566b5c603da3`。`tool_schema_revision` 为 `otel-demo-ext-0c472f7617de`。
- 对比：11-20 全量（`full-o-c2`）与 11-22 重跑（`rerun-o-c2`）OpsPilot 都是 0/3，当时常驻指令没有导入（`GLOBAL_INSTRUCTIONS_NOT_IMPORTED`）；Holmes 在 11-20 全量（`full-h-c2`）是 3/3。这次导入了常驻指令，得分没有变化。
- 未做：没有分析未通过的原因（这次只做复验），也没有重跑 Holmes 侧。

## 费用

- DeepSeek（token × 单价，三遍 summary 的 `run.tokens`）：缓存命中 1 053 184、未命中 122 144、输出 46 297 token。按 deepseek-flash 每百万 token 命中 $0.003、未命中 $0.15、输出 $0.60 算是 $0.049。单价沿用 11-22 记录，本次没有重读定价页。Run 时刻 07:16–07:24 UTC 是周三，在高峰时段（周一至周五 01–04、06–10 UTC）内，价格翻倍，约 **$0.099**。
- Z.ai judge 调用 3 次，每次请求约 15 KB。summary 里没有 usage 字段，所以没有金额。

## 披露

- 凭据扫描：用 rental.env、prometheus-investigator.env、k8s-investigator.env 里的每个值搜索本目录。只有 `LANGSMITH_WORKSPACE_ID` 命中，即 trace 链接里的组织 ID，与以往证据的格式相同。其他都是 0 命中。
- 主机与本地的证据文件 sha256 逐个一致。原始 ledger 不入库。
- 跑完后：驱动启动的进程都已退出，集群里没有残留的 `app-NNN` 命名空间。otel lab 已用 `kind_lab.py stop` 停下，`kind-sregym` 照旧运行。
