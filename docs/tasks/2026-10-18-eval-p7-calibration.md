# 评测 P7 judge 真实校准

- 状态：新题正式校准 `p7-formal-3`（2026-10-31）为 `no_baseline`，三家均不合格，D16 无选择；待用户决定下一步（#295）
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
| P7-D8 | 两位标注者：Claude Opus 5.5 全新子 Agent 与 Codex `gpt-6-astra`，记录实际模型身份；均非候选 judge（隔离方式见[驱动接口](2026-10-19-eval-p7-driver-interface.md) Q8：Claude 以独立全新进程运行）。2026-10-28 补：slot 2 不可用时依次换 `gpt-6-sol`、Claude Fable 5.1，用 Fable 的批次披露两份盲标同属 Anthropic（[M3 决策稿第二轮](2026-10-27-m2-exit-m3-opening-decision.md#第二轮裁决2026-10-28) Q12） | 用户 |
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

## 合同（r3，2026-10-22，重做 #295）

依据：p7-formal-2 诊断（三个全新上下文 Agent 读取已保存观察并重放已作废的 validation 项抓取原始输出；结论见证据节）。

| 编号 | 裁决要点 | 来源 |
|---|---|---|
| P7-D17 | 诊断与标签按 location 匹配：关键因果诊断漏检改为「标签中每个 factual_causal 的 location 都出现在 judge 的 factual_causal 诊断 location 中」，不再比较 quote。约定段改为 quote 取该字段中点明原因的一段逐字短语（不超过 200 字符），输出校验仍要求 quote 是该字段文本的子串。理由：原约定下 quote 由 location 唯一决定，不携带匹配信息，却让模型照抄 300–1,700 字符并出错（Luna 10 次无效中 8 次） | 用户 |
| P7-D18 | verdict 取 judge 诊断经确定性规则重算的结果；模型自报 verdict 与重算不一致不再使答案无效，只记录（观察中保存模型 verdict 与是否一致）。取代 B3「P7 只计 judge 原始 verdict」条款；误通过按重算 verdict 计 | 用户 |
| P7-D19 | judge.PROMPT 的因果判定按现有最终标签口径写明：指出故障所在位置的陈述（「failing dependency is X」「confined to X」「dominated by X」）算因果；声明为推断且带方法限定的不算；传播断言记录起源服务与操作；被否定或排除的假设、与事故无关的「so」从句不算。新 validation 集的盲标说明使用同一段文字 | 用户 |
| P7-D20 | 可逆技术项（lead）：zai 使用 `response_format` json_schema strict（官方文档未列，2026-10-22 实测 4/4 遵守，调参轮继续验证）；按位置标注的报告字段块改为纯文本（不再 json.dumps 转义引号）；答案被拒时保存原始模型输出到私有工作区，并在观察中记录传输异常类名；adapter 升为 `judge-adapters-7`。不对无效答案重试（会掩盖不可靠并抬高分数） | 推荐 |
| P7-D21 | 进过 formal 的 validation 项可作为调参（debug）项重用，永不再作 validation；暴露登记照常阻止其进入任何 formal。取代 P7-D16「进过 formal 的 validation 族不得再用」中对调参用途的限制 | 用户 |
| P7-D22 | 修订 P7-D19「声明为推断且带方法限定的不算」：只有声明原因本身是推断、未确认的陈述不算因果；断言了原因、只补充推断方法（如「Causation is inferred from span nesting and durations within one trace only」）的仍算因果。依据：两位标注者均标为因果，标签冻结；第 1 轮 FlashX/Luna 各 1 次因旧措辞漏诊 | 用户 2026-10-29 |
| P7-D23 | 收窄 P7-D19「指出故障所在位置的陈述算因果」：只指本次事故的故障或变慢所在；只报告哪些服务、序列或 span 名带错误计数/状态的句子（「only X has error spans」、另一窗口内错误集中于 X）是观察，不算因果。依据：与最终标签一致；第 1 轮 FlashX 1 次、Luna 3 次因此多判 | 用户 2026-10-29 |
| P7-D24 | #312 选项 1：judge 合格门槛改为 verdict 一致率；逐字段因果诊断（KEY_DIAGNOSIS_MISSING）降为参考与复核材料，不再决定合格。实施细节（阈值、无效与误通过口径、对已跑轮次的回溯）开工前另做合同预审 | 用户 2026-10-30 |
| P7-D25 | FlashX 断连：正式校准前先跑一轮 FlashX 零断连的 debug；仍断连则改由 Luna 作主候选 | 用户 2026-10-30 |
| P7-D26 | D24/D25 细则（合同预审后，gpt-6-astra 预审）：(1) 门槛仍为 24 道 validation ×2 的一致数 ≥ 46/48；一致只看重算后的 verdict（D18）。任一无效观察（含断连）、P6 复核失败、误通过仍使该家不合格；误通过须为 0。漏诊仍计算、落盘、报告，只作参考，不入 `passed`。formal（`_counts`）与调参计分同用此规则。(2) 第 1–3 轮按新规则只读重算，原 score/triage 不覆盖；三轮仍计为已用。(3) 重跑按供应商：某家按新规则的剩余失败全部分诊为 eval_env 时，该家可用一次原样重跑（同 PROMPT、同标签包），重跑该家完整 60 次调用，不只补丢失的调用；原记录不改。第 3 轮 Luna 不符合条件，不重跑。(4) D25 先行：FlashX 对 12 道 debug 项各判 1 次，沿用现有一次断连重试；「零断连」= 没有调用最终以连接失败结束；不消耗调参轮。通过才重跑 FlashX 第 3 轮；不通过即停下交用户，Luna 不自动改为主候选。(5) 不改 C3 文本，不翻任何 `passes`。仍断言旧漏诊门槛的 6 处测试由未参与实现的测试作者按本条改写 | 用户 2026-10-30 |
| P7-D27 | 修订 judge-adapters-4/P7-D20 的传输重试：只对传输失败（连接失败或流在完整响应前结束，即 `JUDGE_TRANSPORT_ERROR`）最多尝试 4 次（原 2 次）；超时、HTTP 状态、超大、组装后文档的任何校验仍首次即终，无效答案仍不重试。单次尝试 900 s 上限不变，`attempts` 照记。adapter 升为 `judge-adapters-8`（config hash 随之改变）。依据：修网后预检 1/12 为流中途断开且重试也断 | 用户 2026-10-30 |
| P7-D28 | 新一轮盲标后 4 个场景（cart、product-catalog、normal-1、normal-2）debug 两项均为负例，cart validation 为 0 正 4 负：(1) `freeze` 的 debug 1+1 改为与 validation 同样可由书面接受放行（接受记录加 `debug_distribution`，debug 符合时不得出现）；debug 标签不进一致率与误通过，只要求观察有效且复核通过。(2) 接受 cart validation 0+4。(3) 36 项差异中 32 项非实质（仅措辞 18、仅因果位置 14，D24 下只作参考）取 Claude 标注（A）；4 项 verdict 分歧由用户逐项裁决。(4) 改动走 chore PR，测试由未参与实现的作者编写 | 用户 2026-10-30 |

调参规则：在 36 项（原 12 debug + 已作废的 24 validation）上调 `judge.PROMPT`，最多 3 轮；达标线为整套零无效、零漏诊、零误通过且一致率不低于 46/48 等价；不达标停下交用户。达标后才新采 validation 来源族、两份新盲标与用户裁决。

实施取舍（实现者，可逆）：P7-D21 用显式 `debug --tuning`——只额外放行已被 formal 登记暴露的 validation 族（不跳过 formal 暴露检查，未暴露的仍 `VALIDATION_EXPOSURE_FORBIDDEN`），并只跳过包约定与 PROMPT 的逐字绑定（`JUDGE_PROMPT_CHANGED`），items/标注绑定照查；登记行为 `kind=debug, tuning=true`，formal 路径不变。P7-D17 的 200 字符上限只写入 PROMPT、不入输出校验（入校验会使按旧约定整段引用的已有标签失效）。P7-D20 原始输出在 debug 与 formal 均落盘（`debug/<session>/` 或 `calibration/<run>/` 下 `<provider>-NNN.raw`）：formal 由驱动包装传给 `run_calibration` 的 transport，按串行顺序与逐次观察的 input_hash/attempts 对回，库不改。--tuning 下已 formal 暴露的族无论在本工作区属哪个 split 都可复用。

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
- 流式后 zai 诊断（2026-10-20/21）：p7-debug-7 zai 9/12 有效、p7-debug-8 7/12（2 次 900 s 超时、2 次断连）；OpenAI 12/12、DeepSeek 9/12 有效且无断连（p7-debug-6，DeepSeek 余额差 2.33 CNY）。逐字节时间线探针（6 次 debug 调用）显示：超时为真实慢——GLM-5.3 默认 reasoning_effort=max，推理 4–5 万 token、约 75 tok/s、无停顿；断连为单条连接中途停流 142 s 后被对端正常关闭，同时段并发流正常，非固定连接寿命，代理跳与服务端无法区分。用户裁决：zai 改 `reasoning_effort: "high"`（adapter `judge-adapters-4`，与传输重试合并），并在代理中为 z.ai 单独设规则；改后须一轮 zai 零失败 debug 才进入正式校准。- 廉价模型与输出格式（2026-10-21）：12 个 debug 项旧请求下有效数 GLM-5.3-FlashX 6/12、DeepSeek flash 0/12、GPT-6-Luna 0/12、GPT-5.6-Luna 0/12。原始回复探针定位根因：模型按 PROMPT 散文自拟字段名——`exact_quote` 代替 `quote`（GPT-5.6-Luna、FlashX）、`schema_version` 代替 `schema`（GLM-5.3）——以及 FlashX 的 markdown ```json 围栏；加格式约束（OpenAI 严格 json_schema、Z.ai json_object）后回复均可解析有效。用户裁决（费用）：主候选 GLM-5.3-FlashX、对照 GPT-5.6-Luna、`deepseek-flash` 不变（GLM-5.3、Sol 每轮约 $6）；PROMPT 改为精确 JSON 形状，adapter `judge-adapters-5`。单价（每 1M 输入/输出）：FlashX $0.37/$1.25，GPT-5.6-Luna $0.20/$1.20。同一探针还定位两类输入侧 JUDGE_OUTPUT_INVALID：report_raw 是 payload 里的 JSON 字符串，引号到模型处为 `\\\"`，照抄的 quote 带多余反斜杠（QUOTE_NOT_FOUND）；模型数错 claims 下标（claims[10] 与 claims[11]），致 QUOTE_NOT_FOUND 与 CITATION_NOT_IN_CLAIM。用户同日批准修复：user 消息在原 payload 后追加按精确 location 标注的报告字段块（claims 带 evidence_ids），payload 与 input_hash 不变，仍为 `judge-adapters-5`。改后须再跑一轮 debug 确认再进正式校准。

- 新配置 debug（2026-10-21，p7-debug-10，judge-adapters-5：GLM-5.3-FlashX + GPT-5.6-Luna + deepseek-flash，流式、传输失败重试一次、JSON 强制、按位置标注的报告字段）：FlashX 有效 12/12、verdict 12/12、因果键 11/12；DeepSeek 12/12、12/12、12/12；Luna 11/12、verdict 7/11、键 11/11；0 次断连、0 次重试，全轮 23 分钟。费用：DeepSeek 余额差 2.88 CNY（共享账户）；按官方价估算 FlashX ≈ $0.27、Luna ≈ $0.17（588K/566K 输入 token）。
- 正式校准 p7-formal-1（2026-10-21 11:32Z）在第 3 次调用后因 run_calibration 对 judge 自由文本 service 施加元数据标识符规则而整体中止（exit 5，SCHEMA_INVALID）；仅 1 个 validation 项（cart-failure 改写件）被 FlashX 判过 2 次，无调参；用户授权作废该次运行并复用同一 36 项（登记行移入 exposure-voided.jsonl）；修复后改为单条答案无效（#294）。该次 label-pack 副本仍在 `calibration/` 下会使 formal 报 VALIDATION_EXPOSED，已移至 `voided-calibration/p7-formal-1/` 保留并在作废记录登记。
- 修复后 debug（2026-10-21，p7-debug-11，judge-adapters-6）：FlashX 有效 12/12、verdict 11/12、键 12/12；DeepSeek 12/12、12/12、12/12；Luna 有效 9/12（2 次 JUDGE_HTTP_ERROR，其一在传输重试之后；1 次输出无效），verdict 7/9、键 8/9；36 条观察全部通过 `_metadata_identifiers` 严格检查（p7-formal-1 中止的同一检查）；OpenAI 账户限额 5000 RPM / 2M TPM，HTTP 错误非限流（状态码未记录，推断为服务端瞬时错误）。DeepSeek 余额差 2.91 CNY（共享账户）。
- 正式校准 p7-formal-2（2026-10-21，judge-adapters-6，180 次调用约 2.4 小时，pack hash `bcb83332…796b`）：`no_baseline`，三家均不合格，D16 无选择。计入一致 / validation 无效观察 / 误通过：FlashX 30/48、7、0；Luna 28/48、12、2；DeepSeek（对照）39/48、1、0。有效输出上 verdict 一致率高（FlashX 40/41、DeepSeek 46/47），主要失分为 KEY_DIAGNOSIS_MISSING：verdict 正确但漏列标签中的因果字段（多为 summary/claims 对同一原因的重述，约 6 项、三家重叠），FlashX 因此失 10 分；其次为无效观察（FlashX 3 次输出无效、3 次 verdict 与自身诊断不一致、1 次断连；Luna 8 次输出无效、3 次断连）。不计漏诊时 FlashX 40、Luna 31，仍不达 46。费用：DeepSeek 余额差 15.60 CNY（共享账户）；zai/openai 余额未记，按 token 与官方价估 FlashX ≈ $1.4、Luna ≈ $0.9。用户裁决（2026-10-21）：按合同记录不达标、不记基线，M3 暂以确定性断言推进；修 judge 并换新 validation 来源族另立 #295。
- r3 实施（2026-10-22，`chore/p7-r3-judge`，judge-adapters-7）：仅假 transport 的单元测试（`tests/unit/test_eval_b3_impl.py`、`tests/unit/test_eval_p7_r3_impl.py`）；未发真实调用；按 r2 行为写的验收测试待独立作者按 r3 重写。
- #398 调参计分器（2026-10-29，`chore/398-judge-tuning-scorer`，worktree `poa-398-impl`）：新命令 `tune --session S --round N --providers zai,openai,deepseek --judge-env E [--rerun-of S0]` 从冻结 label-pack 读标签（与工作区当前 items/标注/裁决不一致即拒绝），走现有 `debug --tuning` 路径（同一暴露规则、`debug/<S>/<provider>-NNN.{json,raw}`、余额与 `cost`），按 formal 的提供方与项顺序每家发 60 次（12 debug 各 1 次 + 24 validation 各 2 次）。计分复用 formal 逐条规则：一致率只计 48 个 validation 观察且受损观察不得分，无效、漏诊、误通过、已开始次数计全部 60 次（debug 项也计误通过）；`agreement ≥ 46` 且其余为零、started=60 才达标。FlashX 达标为 `passed`，仅 Luna 达标为 `pending_user_decision`（exit 6），DeepSeek 只报告。结果写 `tuning/<S>/score.json` 与 `triage.json`（每个非干净一致的已开始调用一行，带机器 hint，`category` 为空）；`triage-check` 按调试记录重算并校验分诊文件，输出每家 started 与各类计数。轮次登记在暴露行 `round`（与可选 `rerun_of`），按序 1..3，第 4 轮 `TUNING_ROUNDS_EXHAUSTED`；重跑沿用原轮次、每轮一次，要求提示词 hash 未变且原分诊全部为 `eval_env`，原记录不改。
  - 逐条计数从 `_counts` 抽为 `judged_observation`，formal 与调参共用；formal 行为不变（B3 单元与验收 717 项通过）。
  - P6 复核失败（重算 verdict 与完整 AND 不一致）计入 `invalid`、hint 为 `output_invalid`：formal 中它与无效观察同样不得分并使整轮不达标，计入才不比 formal 宽松。
  - 漏诊只计有效答案（formal 的 KEY_DIAGNOSIS_MISSING 同样只对 ok 结果）；每行一个 hint，优先级 transport/http/timeout/output_invalid > false_pass > missed_diagnosis > disagreement。
  - `score_tuning` 每次计一家；中途停止的记录按前缀计分，started 照实、不会达标；顺序或身份不符报 `IDENTITY_MISMATCH`。
  - 重跑只允许针对最近一轮；轮次检查在登记锁内复查一次，防两个 tune 并发同号。
  - 旧的无 `round` 的 `debug --tuning` 行不计轮次；轮次只看本工作区。
  - 冻结绑定按字节：工作区 items、两份标注与裁决文件须与 label-pack 中对应文件逐字节相同（均为规范字节写入），否则 `HASH_MISMATCH`。
  - `tune` 额外要求 `--judge-env`（与 debug/formal 同，否则无法加载配置）。
  - 验证（仅假 transport，未发任何真实调用、未查余额）：`tests/unit/test_eval_p7_398_impl.py` 14 passed；独立验收 `tests/acceptance/test_398_tuning.py` 31 passed（含测试作者按 P7-D20 修正的 .raw 用例）；P7 验收与 r3 单元 168 passed；其余 eval 单元 656 + 194 passed（5 项需 PG 跳过）；B3 单元与 judge 验收 717 passed；ruff、format、mypy 通过。
  - 真实第 1 轮（180 次调用）未运行：用户门，待用户审过合并后的计分器由 lead 运行。
- 调参第 1 轮（2026-10-29，只跑 FlashX + Luna，按合同 R4；`--providers zai,openai`）：
  - 首次运行 `p7-tune-r1`（12:20–13:02Z）环境无效：OpenAI key 过期（52 次 401）+ 8 次断连，FlashX 46/60 断连（ConnectError 成段出现），120 次只有 14 次拿到答案。用户 2026-10-29 授权作废重跑：登记行移入 `exposure-voided.jsonl`，记录移到 `voided-tuning/p7-tune-r1`，全部保留。
  - 重跑 `p7-tune-r1b`（13:55–15:17Z，提示词未改，计第 1 轮）不达标：FlashX 一致 25/48、无效 27（26 断连 + 1 输出）、漏诊 1、误通过 0；Luna 26/48、无效 23（9 断连 + 14 输出）、漏诊 4、误通过 2。分诊（全部已开始次数计入）：FlashX eval_env 26 / judge_error 3；Luna 9 / 21；real_failure 0；`triage-check` 通过。费用：zai/openai 无余额接口，记 unknown；按 token 与官方价估 FlashX ≈ $0.85、Luna ≈ $0.64。
  - 失败归因（全新上下文 Agent 按冻结标签重放原始输出，拒绝码与出错字段经重放核实，模式归类为推断）：quote 非连续逐字（省略号、丢括号、补字）Luna 12；引用其他 claim 的证据 ID 或抄错 FlashX 1、Luna 3；hypothesis 标签被当作对冲 Luna 3（含 2 次误通过）；断言原因并附推断方法被当作不算（P7-D22）各 1；只报告错误分布的句子被判因果（P7-D23）FlashX 1、Luna 3。疑似标签问题只记录、不改标签：同族改写件共享的 claims[8] 反复引发借用引用。
  - 第 2 轮改动（`chore/398-judge-tune`，只改 `judge.PROMPT`）：quote 为单段逐字子串、禁止省略；cited_evidence_ids 只取该 claim 行的 ID；hypothesis 标签不算对冲；P7-D22、P7-D23 措辞。独立审查 APPROVE（gpt-6-astra）；`test_d19_prompt_states_the_causal_rules_of_the_final_labels` 由未参与实现的测试作者（gpt-6.1-sol）按 D22/D23 改写，实现随后调整措辞位置（e2d80de6）；相关套件 918 passed。
- 调参第 2 轮 `p7-tune-r2`（2026-10-29 18:56–20:03Z，PROMPT e2d80de6，FlashX + Luna）不达标：FlashX 一致 39/48、无效 5（4 断连 + 1 输出）、漏诊 2、误通过 2；Luna 42/48、无效 3（均为输出）、漏诊 1、误通过 1。分诊：FlashX eval_env 4 / judge_error 7；Luna 0 / 10；real_failure 0；`triage-check` 通过。费用：余额记 unknown，按 token 估 FlashX ≈ $1.3、Luna ≈ $0.72。剩第 3 轮。
- 标签复核（2026-10-29，变更 J5）：用户裁决调参期提前执行 #312 选项 2（原 J5 = 标签冻结至 3 轮后）；第 1–2 轮按旧标签计分，与第 3 轮不直接可比，记录全部保留。全新上下文 Agent 对照 D19/D22/D23 复核 36 项与同族同句，用户裁决（2026-10-29，经 production-ops-agent-2b 转达）：同族四份「observed error is confined to the EmptyCart sub-call」统一为不是原因（b0b69a8a 删除该条目，verdict 不变）；c10c38e9 summary 保留为原因。按规则直接修正（verdict 均不变）：b3e2b269 claims[3] 传播起源改为 payment/grpc.oteldemo.PaymentService/Charge（与三份同句兄弟件一致）；7692229f claims[9] 只保留 currency 起源。32063c1d 不改，只记口径：报告声明方向未定时起源按报告字面记录。经 `adjudicate`（全部 36 项沿用原决定，3 项改为 revised 并追加决定记录）与 `freeze` 生成新 label-pack `7d6fa61e…6c24`，分布不变（debug 每族 1/1、validation 每族 1/3）；旧包（`bcb83332…796b`，formal-2 所用）与原决定归档在私有工作区 `label-history/v1-bcb83332/`。之后的裁决材料不附裁判模型的判法（P7-D16 盲规则）。
- 第 3 轮改动（只改 `judge.PROMPT`，依第 2 轮归因）：调查自身的限制（数据/trace/覆盖缺失）不算因果；D23 示例改为「另一时间窗内在 X 上看到的错误」并加入「the observed error is confined to the X sub-call」与基线由谁主导为观察；因果示例收窄为「the failure is confined to X」「latency is dominated by X」；summary 只有无 claim 陈述同一原因时 linked_claim 才为 null；传播起源按报告字面或回指，不从证据另取；句内对冲（not a proven root cause 等）仍算对冲；同一字段同一原因只出一条诊断；quote 只取本字段文字，拆分列表不重复共享词。
- 调参第 3 轮 `p7-tune-r3`（2026-10-30 02:47–03:46Z，PROMPT 52bd27dd，`judge_prompt_sha256` `f79d9e05…5606`，新 label-pack `7d6fa61e…6c24`，FlashX + Luna）不达标：FlashX 一致 34/48、无效 16（均为断连）、漏诊 3、误通过 0；Luna 38/48、无效 7（均为输出）、漏诊 1、误通过 0。分诊：FlashX eval_env 16 / judge_error 3；Luna 0 / 12；real_failure 0；`triage-check` 通过。存在 judge_error，按规则不可重跑。费用：余额记 unknown，按 token 估 FlashX ≈ $0.98（2.07M/0.17M）、Luna ≈ $0.70（2.68M/0.14M）。3 轮用尽，按合同转 #312；调参分支 `chore/398-judge-tune` 不合并、保留。
- D24/D26 实施接口（实现者自定，可逆）：`tuning.assess_tuning` 与 `calibration_flow._counts` 的 `passed` 不再含 `KEY_DIAGNOSIS_MISSING`，`missed_diagnoses` 字段保留。新只读命令 `rescore --session <调参会话>`：按当前规则重算该会话已落盘观察，写 `tuning/<会话>/rescore-d24.json`（含各家分数与剩余失败行，每行沿用原 triage 的 category），只写一次，不改 `score.json`/`triage.json`。`tune --rerun-of <会话> --providers <家>`：若原会话有 `rescore-d24.json`，按其中所列家逐家检查剩余失败行是否全为 eval_env，未列入 `--providers` 的家不检查；只对列入的家跑完整 60 次；每轮仍只许一次重跑。D25 预检用现有 `debug --tuning --provider zai --item <12 道 debug 项>`，不加代码，按观察中 `JUDGE_TRANSPORT_ERROR` 计数判定。
- P7-D25 预检 `p7-d25-preflight`（2026-10-30 13:19–13:23Z，PROMPT 52bd27dd，FlashX 12 道 debug 各 1 次，`debug --tuning`，不计调参轮）不通过：6/12 最终以 `JUDGE_TRANSPORT_ERROR` 结束（均为 ConnectError，13:21:50–13:22:41 连续 6 次，每次 2 次尝试），其余 6 次有效。按 D26(4) 停下交用户，未重跑第 3 轮。归因（推断）：第 2、3 轮与本次的断连均为建立连接失败（第 3 轮 15/16 ConnectError、1 ReadError），成段出现、每段约 1–2 分钟，第 3 轮约每 10 分钟一段（02:52、03:02、03:12）；请求未送达模型，原因在本机网络/代理或 z.ai 入口，未确认。费用：余额记 unknown。
- 用户修复 z.ai 网络后重跑预检 `p7-d25-preflight-2`（2026-10-30 16:09–16:23Z，PROMPT 与 config hash 同上）仍不通过：11/12 有效（均 1 次尝试，verdict 与因果键 11/11），1 次最终 `JUDGE_TRANSPORT_ERROR`（RemoteProtocolError，流中途断开，两次尝试共 369 s）。ConnectError 成段断连未再出现；剩下的是 10-20 诊断记录过的流中途停流。按 D26(4) 再次停下交用户。
- D24/D26 实施（b09d9272，`chore/398-judge-tune`）：测试作者（gpt-6.1-sol）改写旧漏诊门槛并补 rescore 与按供应商重跑用例（1e9cabe7），实现后发现 3 处测试错误由测试作者确认修正（c1b331dc）；P7 套件 71 passed；ruff、项目 mypy（strict，`opspilot/`）通过。独立审查（gpt-6-astra）首轮 1 项发现——「第 3 轮应禁止 Luna 重跑」——实现者按 D26(3) 原文答复为对实际数据的适用而非单独规则，审查者撤回，APPROVE。按新规则只读重算（`rescore`，各写 `rescore-d24.json`，原 score/triage 未动；用当前冻结包，verdict 标签未改，新规则下分数与包无关）：第 1 轮 FlashX 25/48、无效 27、误通过 0；Luna 27/48、23、2。第 2 轮 FlashX 41/48、5、2；Luna 43/48、3、1。第 3 轮 FlashX 36/48、无效 16（均断连）、误通过 0；Luna 39/48、7、0。均不合格；第 3 轮 FlashX 剩余失败全为 eval_env，具备按供应商重跑条件，Luna 不具备。
- P7-D27 后预检 `p7-d25-preflight-3`（2026-10-30 17:54–18:02Z，PROMPT 52bd27dd，judge-adapters-8，FlashX 12 道 debug 各 1 次）通过：12/12 有效、均 1 次尝试（未触发重试）、0 次断连；verdict 12/12、因果键 11/12。按 D26(4) 可进行第 3 轮 FlashX 重跑。
- 第 3 轮 FlashX 按供应商重跑 `p7-tune-r3b`（2026-10-30 18:43–19:16Z，`--rerun-of p7-tune-r3 --providers zai`，PROMPT 52bd27dd，judge-adapters-8，新规则）不达标：一致 46/48、无效 0、误通过 2、漏诊 4（参考）、已开始 60；60 次均 1 次尝试，0 次断连。唯一不合格原因是误通过：两次均落在同一道 validation 负例（两次重复），第 2 轮 FlashX 也在该项误通过。分诊：judge_error 6（误通过 2 + 漏诊 4），`triage-check` 通过。费用：余额记 unknown，按 token 估 ≈ $1.35（2.79M/0.26M）。第 3 轮重跑已用，3 轮调参用尽。同期全量测试 9974 passed、2472 skipped（PG 需显式开启）、2 xfailed；P7-D27 改动独立审查 APPROVE（gpt-6-astra，首轮 3 项发现已修复复验）。
- 新题来源（2026-10-30，main d1e9f9d0，私有目录 `docs/evidence/eval/eval-p7-src{e,f,g,h}-20261030/`，已忽略）：4 轮共 24 个 live 槽位，gate 通过 11（srce 7/12、srcf 2/6、srcg 1/4、srch 1/2），失败均为 `SIGNATURE_ABSENT`（报告未引用匹配签名的已交付证据；cart 2/9、product-catalog 2/6 最难采）。调查 Agent 费用按余额差约 ¥4.5（共享账户）。
- 新工作区 `p7-cal-20261030`：12 份原件（6 场景各一 debug 族、一 validation 族）+ 独立作者 `p7-rewrite-author-2` 的 24 份改写；assembled `e7612e2e…`，盲标包 `f07b4ea7…`。盲标：槽 1 Claude（`claude-opus-5-5`，每项全新进程，无工具）sha `0075e5da…`，$25.26；槽 2 Codex（`gpt-6-astra`，只读沙箱）sha `ce5db27f…`，34 份仅做格式归一（多余 schema 键、judge 形状绑定 item_id），5 份无效答案在全新进程重标（1 份两次），首次答案保留；归一与重标写入证据说明。简报 `7bf3559c…`：36 项差异，0 项完全一致；仅措辞不同 14、verdict 同而因果位置不同 18、verdict 分歧 4。
- 冻结（P7-D28）：4 项分歧由用户逐项裁决，36 项均取 A；分布 debug 仅两个 payment 场景 1/1，其余四场景 0/2；validation cart 0/4、其余 1/3（共 5 正 19 负）；接受记录含 `debug_distribution`。freeze 改动 #408（f75b998c，测试由 gpt-6.1-sol 按合同编写，gpt-6-astra 独立审查 APPROVE，机器人无发现）。label-pack `d873ce37…0288`。formal 前置 debug `p7-precheck-20261030`：三家各 2 道 debug 项全部有效。
- 正式校准 `p7-formal-3`（2026-10-31 02:17–05:11Z，main f75b998c，PROMPT 第 3 轮，judge-adapters-8，每家 60 次）：`no_baseline`，三家均不合格。一致 / 无效 / validation 误通过：FlashX 44/48、0、2；Luna 35/48、7（`JUDGE_OUTPUT_INVALID`）、1；DeepSeek（对照）46/48、0、1。FlashX 4 次失分落在 3 道 validation 项：normal-1 一道正例两次判不通过（该项为 4 道裁决分歧之一），payment-failure-1 一道负例误通过一次（同为分歧项），cart 原件负例误通过一次（改写作者曾提示其可能被读作负例，两位标注者均判负）。DeepSeek 达到 46 但 cart 原件误通过一次；另在 product-catalog debug 负例判通过一次（debug 不计误通过）。FlashX 60 次中 59 次 1 次尝试、1 次传输重试后成功，0 次断连。费用：DeepSeek 余额差 ¥4.64（共享账户）；zai/openai 余额未记，token 为 FlashX 3.29M/0.33M、Luna 3.13M/0.19M，按第 3 轮单价估 FlashX ≈ $1.6、Luna ≈ $1。本次 validation 族已 formal 暴露，不能再作 validation（P7-D16/D21）。

## 待决

- 重做 P7（修 judge、新 validation 集、新盲标与裁决）：#295。
