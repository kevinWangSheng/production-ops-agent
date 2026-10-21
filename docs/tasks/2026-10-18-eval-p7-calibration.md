# 评测 P7 judge 真实校准

- 状态：进行中（驱动 #285 已合入；36 项已组装；P7-D9 约定随本 PR 写入 judge.PROMPT，合入后导出盲标包）
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
| P7-D8 | 两位标注者：Claude Opus 5.5 全新子 Agent 与 Codex `gpt-6-astra`，记录实际模型身份；均非候选 judge（隔离方式见[驱动接口](2026-10-19-eval-p7-driver-interface.md) Q8：Claude 以独立全新进程运行） | 用户 |
| P7-D9 | 保持现行严格口径（完整标签、`(location, quote)` 精确相等）；debug 阶段给标注者与三家 judge 同一份引文粒度与排序约定；debug 仍大量误判则停下交用户改规则 | 用户 |
| P7-D10 | 用户逐项裁决所有完整标签差异（最坏 36 项）；工具生成最终 BlindLabel 并绑定两份 annotation hash 与裁决记录 | 推荐 |
| P7-D11 | 先只对 debug 项调 `judge_once` 调优，冻结 prompt/config 后才跑正式校准；validation 暴露后修 judge 须换新的 validation 来源族 | 推荐 |
| P7-D12 | 校准与第一轮基线来源分离，先校准后基线 | 用户 |
| P7-D13 | 新增 `chore` 评测执行 PR：capture→CalibrationItem 导入、改写记录、盲标包导出与导入、差异裁决、debug-only 调用、正式 freeze/run/read-back、费用记录；不改判分、匹配与认证；数据出口只按 r2 的 SPEC 扩展 | 推荐 |
| P7-D14 | 结果为 pending（需选 Sol）时停在 pending 交用户，认证入口另行实现，不手改 selection | 推荐 |

## 合同（r2，2026-10-19）

| 编号 | 裁决要点 | 来源 |
|---|---|---|
| P7-D15 | 数据出口：SPEC 评测段加 2026-10-19 扩展，P7 盲标可由 Claude（Anthropic）与 Codex `gpt-6-astra`（OpenAI）接收与三家 judge 相同的输入，仅限开发评测与合成 lab 数据（PR #282 机器人 P1） | 用户 |
| P7-D16 | 执行驱动接口见 [P7 驱动接口](2026-10-19-eval-p7-driver-interface.md)；其 Q1–Q8 采纳推荐：调优只改 `judge.PROMPT` 走独立小 PR；1+3 分布不符时 freeze 拒绝并交用户；导入时 diagnoses 稳定排序、reason 保留；36 项原件须过 gate；裁决简报对用户同样盲；进过 formal 的 validation 族不得再用；来源轮次以 `eval-p7-` 命名并被忽略；盲标包复制到仓库外目录交给标注者 | 推荐 |

可逆默认（lead）：12 个来源一律新采（`--scenario all --n 2` 一轮），不复用 PR-C 与 #273 的两次 payment Run，理由是同一代码版本、一次覆盖六场景 live 路径，代价约 20 分钟 lab。

## 计划

1. 执行驱动 chore PR（P7-D13）：Opus 实现，sol 按接口写验收测试，astra 审。
2. 来源采集：协调 lab 后跑 12 次 live（约 2 小时），原件放 ignored 私有目录。
3. 改写：独立作者 Agent 按 P7-D4–D6 生成派生件与覆盖说明。
4. 盲标：Claude（隔离的全新进程）与 astra 各标 36 项；差异简报交用户裁决。
5. debug 调优（P7-D11），冻结后正式校准 180 次，读回认证。

## 证据

- 预审决定表原文保存在私有目录，未入库；本节随执行补充。
- **无效轮（保留原始记录，不作 P7 来源）**：`eval-p7-src-20261019`（12 槽）、`eval-p7-src2-20261019`（4 槽）、`eval-p7-src3-20261019`（1 槽后 SIGINT 中止，exit 130，故障已恢复）。启动时未载入调查侧 Prometheus 账号（docs/development.md:137），模型的 `metrics_range_query` 全部 401、显示为 `SOURCE_ERROR`，调查只能靠 trace 抽样（#284）。期间发现录制校验器误判 transport 发送前拒绝的调用（#283），以及 product-catalog 在只有 trace 小样本时常拿不到错误 span（匹配器核实无误）。宿主健康：checkout p95 仅在 cartFailure 注入窗内为 15 s（故障本身），swap 7.7–7.9 GB 未上涨。三轮调查模型余额差约 2.7 CNY。`eval-273-run-20261018` 同样未载入账号，只影响模型证据，不影响其中断验证结论。
- 重采轮 `eval-p7-srcb-20261019`（2026-10-19 09:11Z 起，dc/3e 确认空闲；用户放行在子 shell 载入调查侧账号，开跑前用该账号实测 metrics 查询 200）：09:11–10:53Z，12 槽 11 槽 `ran`，交付工具结果均为 `ok` 或 `no_data`，不再有 401。gate 通过 10 槽：cart-failure、normal-1、normal-2、payment-failure-2 各 2 槽；payment-failure-1 与 product-catalog-failure 各 1 槽。未通过 2 槽：payment-failure-1 第 2 槽录制校验 `IDENTITY_MISMATCH` 中止（#283），中止前故障已恢复；product-catalog-failure 第 1 槽 `SIGNATURE_ABSENT`。judge 未校准，其余槽 `JUDGE_NOT_RUN` 属预期。宿主健康：checkout p95 仅 cartFailure 注入窗（09:13–09:27Z）内 9–15 s，其余低于 300 ms；swap 7.4–8.3 GB。余额差 1.79 CNY（共享 key，可能含他线）。收尾：三个故障 flag 核实为 off，port-forward 与采样进程已停，occupation 已释放并通知 dc。缺口：payment-failure-1 与 product-catalog-failure 各缺 1 个 gate 合格来源，需补跑。
- 补跑轮 `eval-p7-srcc-20261019`（payment-failure-1 ×2）与 `eval-p7-srcd-20261019`（product-catalog-failure ×2；srcc 第二个场景因 `ROUND_EXISTS` 未启动，换轮次名重跑），12:11–12:51Z，dc/3e 确认空闲。4 槽均 `ran`：payment-failure-1 两槽 gate 通过；product-catalog-failure 第 1 槽 `SIGNATURE_ABSENT`、第 2 槽通过。checkout p95 最高 190 ms，未入 0.5–1.8 s 劣化区间；swap 8.4–9.2 GB（宿主偏紧，已记）。余额差 0.37 CNY。收尾同上。合计 gate 合格来源：cart-failure、normal-1、normal-2、payment-failure-2、product-catalog-failure 各 2，payment-failure-1 为 3。

- 组装（2026-10-19，私有工作区 `tmp/eval-p7/p7-cal-20261019`，未入库）：13 个 gate 合格来源全部导入；独立作者 Agent（`p7-rewrite-author-1`，`claude-opus-5-5`，全新上下文，与两位标注者分离）写 24 个受控改写，全部通过驱动机械校验 R1–R12；每场景一族供 debug（原件 + 1 改写）、另一族供 validation（原件 + 3 改写），payment-failure-1 第三个来源留作备用；validation coverage 并集为七名；`assembled/items.json` sha256 `16d778f26efa898240b857178bdc987a622b5551314192aaab339d9cb4787e5f`。作者自评可能引起标注分歧的点：cart debug 原件摘要中的 Redis 措辞、product-catalog validation 原件、以及只按严格引用规则才算错的 borrowed_citation / insufficient_evidence 负例。
- P7-D9 约定（lead 按推荐定，写入 `judge.PROMPT` 末段）：quote 取 location 字段全文逐字，不截片段；只列因果断言（假设记 factual_causal=false），不列非因果观察；同一字段多个原因按不同 service/operation 各列一条、quote 相同；按 summary、claims、gaps、next_steps 顺序排列。理由：`(location, quote)` 精确相等下，全文引用消除切分分歧；差异判定含 reason 文本，预计多数项需用户裁决（P7-D10 最坏 36 项）。
- debug 第一轮 `p7-debug-1`（2026-10-20）：zai 0/12 ok（6 超时、6 推理耗尽输出上限）；openai 2/12（5 代理断连、5 `linked_claim` 返回字符串）；deepseek 4/12（5 推理耗尽、3 `linked_claim` 返回字符串）。DeepSeek 余额差 1.39 CNY，zai/openai 未记余额。诊断：zai 与 DeepSeek 把 16384 输出上限全部用于推理（finish_reason=length、content 为空），zai 慢调用触及 360 s 超时；OpenAI 断连来自本机代理（已修复）；OpenAI/DeepSeek 有时把 `linked_claim` 写成 `"claims[N]"` 字符串。用户裁决（2026-10-20）：judge 输出上限 16384 → 65536、单次超时 360 → 900 s，保持零重试；`judge.PROMPT` 返回字段句增补「linked_claim 为 claims[N] 的整数 N 或 null」，解析侧不做强制转换，末段约定原文不变。
- debug 第二/三轮（新上限；`p7-debug-2` 跑 openai 与 deepseek，`p7-debug-3` 跑 zai，仅 12 个 debug 项）：DeepSeek 10/12 有效，verdict 一致 9/10，因果键完整 5/10，余额差 1.65 CNY；OpenAI 8/12 有效（4 传输断连），verdict 6/8，键完整 8/8；zai 5/12 有效（4 传输/HTTP 错误、1 超时、2 输出无效），verdict 4/5，键完整 2/5。断连来自本机网络。对照最终标签（只取 debug 项，validation 未读）：漏判 8 处——整句传播断言未列出 2（DeepSeek）；已列出、quote 一致，但把带方法说明、深层触发未知或「假设」标签的未对冲因果断言标成非因果 6（DeepSeek 3、zai 3）；位置相同而 quote 不同 0。多判 15 处——遥测观察或引用的错误文本被标为因果 8（OpenAI 4、DeepSeek 3、zai 1）；对冲或仅相关的归因被标为因果 4（OpenAI，含 1 处 summary）；只陈述未知量的 gap 被标为因果 2（OpenAI）。verdict 不一致 4 处，其中 3 处由上述分类偏差引起，1 处（OpenAI）是同一传播断言的 supported 判断与标签不同。调参第 1 轮（P7-D11/Q1-A，只改 `judge.PROMPT` 约定段之前的文字）：按字段主断言判 factual_causal——未对冲的因果或传播断言（含 gap/next step 中、带方法或深层触发说明、带假设标签的）算事实；对冲措辞、单纯遥测或引用的错误文本、未决未知量不算。约定段逐字不变。

- 调参后 debug（2026-10-20，p7-debug-4/5，PROMPT 调参第 1 轮）：DeepSeek 有效 10/12、verdict 10/10、因果键完整 10/10（调参前 5/10），余额差 0.70 CNY；OpenAI 有效 11/12、verdict 8/11、键完整 11/11，本轮无断连；zai 有效 3/12（3 项 verdict 与键全中），6 次连接中断、1 次 900 s 超时、2 次输出无效，单次 5–12 分钟。结论：调参对 DeepSeek/OpenAI 有效；zai 的长时非流式请求在本机网络下频繁断连，正式校准零重试下 zai 几乎必然不合格，待用户决定处理方式。
- 流式后 zai 诊断（2026-10-20/21）：p7-debug-7 zai 9/12 有效、p7-debug-8 7/12（2 次 900 s 超时、2 次断连）；OpenAI 12/12、DeepSeek 9/12 有效且无断连（p7-debug-6，DeepSeek 余额差 2.33 CNY）。逐字节时间线探针（6 次 debug 调用）显示：超时为真实慢——GLM-5.3 默认 reasoning_effort=max，推理 4–5 万 token、约 75 tok/s、无停顿；断连为单条连接中途停流 142 s 后被对端正常关闭，同时段并发流正常，非固定连接寿命，代理跳与服务端无法区分。用户裁决：zai 改 `reasoning_effort: "high"`（adapter `judge-adapters-4`，与传输重试合并），并在代理中为 z.ai 单独设规则；改后须一轮 zai 零失败 debug 才进入正式校准。
## 待决

- 无。
