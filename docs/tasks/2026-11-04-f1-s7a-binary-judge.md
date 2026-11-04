# F1 收尾 S7a：Q9 二值 judge 与判分汇总

- 状态：进行中（实现完成；独立验收 32/32 通过；待审查）
- 更新日期：2026-11-04
- 依据：裁决 `~/.agent/handoffs/opspilot-rulings-2026-11-04-f1-closure.md`（U3）；预审 `opspilot-prereview-f1-closure-plan-2026-11-04.md` §1 D、R10、R11、§3.3 第 2 类；M3 方向裁决 Q9、Q11；收口合同 r1 六类表的「判法」列（`2026-10-27-m3-closure-contract.md`）。F1 第 4 步。
- 工作区：`feature/F1-s7-binary-judge`，`/Users/shenghuikevin/dev/AI/poa-s7-judge`

## 做了什么

- `opspilot/evaluation/binary_judge.py` 提供验收约定的入口：`JUDGES`、`judge_applicable`、`judge_messages`、`project_opspilot_report`、`project_holmes_output`、`root_cause_citation`、`summarize`。另有 provider、裁决清单与模板。
- 两家 judge（z.ai GLM、OpenAI）对「期望要素 + 报告文本」各判一次过/不过，并给出理由。两个系统用同一个 prompt、同一份输入投影。
- Q11 确定性检查先判，不过的直接记为不过。收口合同里标「只确定性」的行不送 judge。
- 两家结论不一致，或有一家没给结论，导出成给用户逐题裁决的清单 `disputes.md` 和模板 `rulings.json`，按 `attempt_id` 填写。
- 汇总给出单次通过率、pass^k、n、Wilson 95% 区间和一致率，另列 OpsPilot 专属的「根因证据引用率」，不进对比分母。
- `scripts/eval_compare_scoring.py` 是验收入口模块，也是命令行，有 `judge` 和 `summarize` 两个子命令。OpsPilot 的输入是开发轮或保留集的 summary 加公开证据，可选按录制复核证据。HolmesGPT 的输入是 S7b runner 的轮次 summary（`opspilot-holmes-dataset-round-1`）加各尝试的 `report.md`，并核对 sha256。
- 旧 `judge.py`（P7 校准 judge）没有改，只复用了它的 HTTP 流式传输层。

## 可逆默认（Agent 自定）

| 项 | 默认 | 理由 |
|---|---|---|
| prompt | HolmesGPT `a0009195` `tests/llm/utils/classifiers.py` 的 strict 原文（A = 期望要素全部在场），后接 Q9 问句（有注入故障：根因实质找对；没有：不误报），再接「只回 json：rationale + choice」 | Q9「照上游」。上游用 autoevals 的 CoT 工具调用产出理由和选项，json 是两家端点都支持的等价写法 |
| 有无故障怎么判 | 看答案是否带 `signature`，不看 `polarity` | dev-v2 的 polarity 指「应当发生 / 不应发生」，不是「有故障 / 正常」：dv2-28535a、7261fe、dc387a、fa0e58 都是 negative，但都注入了 paymentFailure |
| 模型 | 写在代码里：`glm-5.3-flashx`、`gpt-5.6-luna`（P7 已批的型号）；不读模型环境变量 | R10；冻结后评测配置固定在代码里，不随环境漂移 |
| key | `ZAI_API_KEY`，没设时读 `GLM_API_KEY`；`OPENAI_API_KEY`；base URL 固定 | 前三个变量名沿用 judge.py；lead 确认用户环境里的 z.ai key 叫 `GLM_API_KEY` |
| 参数 | z.ai `reasoning_effort=high`；输出上限 16384；单次超时 600 s；传输错误或超时重试 1 次 | effort 照 P7；16384 是上游 classifier 的值 |
| ReportV2 投影 | 按报告原顺序写 assessment、conclusion、summary、各 claim（类型标签 + 原文 + `E<n>`）、gaps、next steps | 原文照搬，不暴露内部 ID |
| OpsPilot 的 checks | `q20.*` 取轮次 summary 里已复核的 Q20 期望；judge 适用的行再加现场重算的 `gate.*` | 「只确定性」行按 Q20 判；gate 要求有报告和签名，套在这些行上会把合同允许的行为也否决掉 |
| HolmesGPT 的输入 | 读 #470 `summarize` 的真实轮次 summary：`slot["raw"]` 指向 `holmes.json`，路径必须在 attempts 根目录之下，sha256 要和该尝试自己的 summary.json 对上；送 judge 的只有 `project_holmes_output` 取出的 `result` | 审查第 1 轮 X2：之前读的 slot 形状是自己夹具编的 |
| HolmesGPT 的 checks | `final_state`：按 slot 的 `final_state` 对照答案的 `expected_outcome.final_states` 重算；`read_only`：取 slot 自己的 `checks.read_only`，没开始的为不适用；judge 适用的行再加 `report`（`result` 不为空） | Q11：HolmesGPT 只适用「只读未越权、调查跑完」；机器人 4470679556 |
| 没开始的槽 | 两边都用合成 id（`opspilot-not-started-<题>-<槽>`、`holmes-not-started-<题>-<槽>`），计入分母，记为不过 | 审查第 1 轮 2；`eval_dev_round` 的这种槽 `attempt_id` 为 None、checks 为空 |
| 对比分母 | `comparison` 行只含两边都有尝试、且 judge 适用的题，即问题入口；`systems` 行是各自全部题的成绩；「只确定性」的行单独放在 `deterministic_only`，各系统分开报，不进对比 | lead 裁决（审查第 1 轮 3、4）：Holmes 没有 Q20 检查，进对比会白拿分 |
| 作废尝试 | 读 #474 的 `eval_pair_schedule` 日志和 `check` 输出，按 `voided[].window_id` 和 `systems` 在日志里取 attempt_id；这些尝试不送 judge，不进裁决、不进任何分母，列在 `voided_attempts`；去掉作废的之后再查同一题是否超过 k 次 | 审查第 1 轮 5（X3） |
| 裁决清单 | 不写系统名，按类别排序 | 审查的可选项：让用户只看报告本身来裁 |
| 分母 | 每个开始过的尝试都算；不足 k 次的题算 pass^k 不过；同一题超过 k 次报 `REDRAW_DETECTED` | §3.3 第 2 类 |
| 根因证据引用率 | `conclusion ∈ {supported, partial}`，且有 fact/hypothesis claim 带事实绑定引用了签名命中的记录（复用 `report_signature_matches`，加 kind 过滤）；没有签名的题记为不适用 | §1 D 的规则原样落地 |

## 证据

- 独立验收 `tests/acceptance/test_s7a_binary_scoring.py`（提交 eca22936，以及测试作者的修正 9ca39154）：32/32 通过。
- 实现侧 `tests/test_binary_judge.py` 22 项通过。端到端用例跑在 #432 的 36 个证据槽位上，加一份用 #470 自己的构造函数（b19e8a22）生成的真实轮次 summary（`tests/fixtures/s7a-holmes-round/`，生成脚本在同目录），用假客户端。ruff 通过；mypy 全仓 155 个文件通过。
- 离线验证（假 judge，没有发出真实请求）：在 #432 的 36 个槽位上加 `--recording-root poa-432-impl/tmp/eval`，32 份报告的证据都和录制重新推出的结果一致。F14 HolmesGPT run1 的原始输出包成 S7b 的轮次格式，当作 dv2-2b214b 送入。结果：
  - 「只确定性」8 次，正是 #432 gate 不过的那 4 题；
  - Q11 否决 0 次；
  - 送 judge 的 OpsPilot 28 次，加 HolmesGPT 1 次；
  - 根因证据引用率 24/28。

  假 judge 的结论没有意义，这些数字只说明管线能跑。

## 待决

- （已解决）验收原来要求按 polarity 选问句；lead 核对后裁为按有无 signature 选，测试作者在 9ca39154 改了断言，实现不变。
- （已解决）HolmesGPT 侧的 `read_only`：#470 每个 slot 自带 `checks.read_only`。
- （已解决）H6：用户已确认两家 judge 的 key 有效，余额各 ≥ $20；z.ai 走 `GLM_API_KEY` 回退（lead 转达，2026-11-04）。
