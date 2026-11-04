# F1 收尾 S7a：Q9 二值 judge 与判分汇总

- 状态：进行中（实现与离线验证完成，等独立验收测试与审查）
- 更新日期：2026-11-04
- 依据：裁决 `~/.agent/handoffs/opspilot-rulings-2026-11-04-f1-closure.md`（U3）；预审 `opspilot-prereview-f1-closure-plan-2026-11-04.md` §1 D、R10、R11、§3.3 第 2 类；M3 方向裁决 Q9、Q11（`opspilot-rulings-2026-10-31-m3-direction.md`）。F1 第 4 步。
- 工作区：`feature/F1-s7-binary-judge`，`/Users/shenghuikevin/dev/AI/poa-s7-judge`

## 做了什么

- `opspilot/evaluation/binary_judge.py`：两家 judge（z.ai GLM、OpenAI）各对「期望要素 + 报告文本」给一次过/不过和理由；两个系统用同一个 prompt 和同一份输入投影。OpsPilot 的 ReportV2 先渲染成文本，HolmesGPT 用 `holmes ask` 的 `result` 原文。Q11 先判：不过的尝试直接记为不过，也不送 judge。两家分歧、或有一家没给结论的，导出成给用户逐题裁决的清单 `disputes.md` 和模板 `rulings.json`。汇总给出单次通过率、pass^k、n、Wilson 95% 区间、两家一致率，另列 OpsPilot 专属的「根因证据引用率」，不进对比分母。
- `scripts/eval_binary_judge.py`：`judge`（读开发轮或保留集的 summary 与公开证据，可选按录制复核证据，可选读 HolmesGPT 输入清单）和 `summarize`（离线，读裁决后出 `summary.json`）。
- 旧 `judge.py`（P7 校准 judge）没有改，只复用了它的 HTTP 流式传输层。

## 可逆默认（Agent 自定）

| 项 | 默认 | 理由 |
|---|---|---|
| prompt | HolmesGPT `a0009195` `tests/llm/utils/classifiers.py` 的 strict 原文（A = 期望要素全部在场），后面加一句「只回 json：rationale + choice」 | Q9 要求「照上游」；上游用 autoevals 的 CoT 工具调用产出理由和选项，json 是两家端点都支持的等价写法 |
| 模型 | `glm-5.3-flashx`、`gpt-5.6-luna`；可以用 `OPSPILOT_JUDGE_MODEL`、`OPENAI_JUDGE_MODEL` 覆盖；candidate 模型 `deepseek-*` 被拒绝 | R10；与 P7 已批的端点和型号一致；环境变量名沿用 judge.py |
| 参数 | z.ai `reasoning_effort=high`；输出上限 16384；单次超时 600 s；传输错误或超时重试 1 次 | effort 照 P7；16384 是上游 classifier 的值 |
| ReportV2 渲染 | 按报告原顺序写 assessment、conclusion、summary、各 claim（类型标签 + 原文 + `E<n> (工具名)`）、gaps、next steps；不写内部 evidence ID | judge 能看出引用的是 trace 还是 metric 证据，但看不到对它无意义的 ID |
| Q11（OpsPilot） | 现场重算 `run_gate`：执行状态、报告 schema、引用与绑定、`read_only`、故障题的签名 | Q11 原文所列各项都在 gate 里；36 个槽位重算结果与存档的 gate 一致 |
| Q11（HolmesGPT） | `read_only` 取 runner 的代理日志结论；`completed` = `finish_reason == "stop"` 且 result 不为空 | Q11：HolmesGPT 只适用这两项 |
| 被否决的尝试 | 不送 judge，不算进一致率 | 一票否决优先，送了也不影响结果，只会多出要用户裁的项 |
| judge 报错 | 和分歧一样进入用户裁决清单 | 不悄悄补跑，不把报错当成不过 |
| 分母 | 每题 k 个槽位都算；少了的槽位记为不过；同一题超过 k 次就报 `REDRAW_DETECTED` | §3.3 第 2 类：所有开始过的尝试都计入 |
| 根因证据引用率 | `conclusion ∈ {supported, partial}`，且有 fact/hypothesis claim 带着事实绑定引用了签名命中的记录（复用 `report_signature_matches`，加 kind 过滤）；只对故障题统计 | §1 D 的规则原样落地 |
| HolmesGPT 输入清单 | `opspilot-holmes-judge-inputs-1`：每个尝试一行，写 `scenario_id`、`attempt_id`、`slot`、`holmes_json`（相对路径；没有产出时为 null）和 `read_only`；只收问题入口的题 | Holmes 的分母只含问题入口题；由 S7 的 HolmesGPT runner 产出 |

## 证据

- 定向测试：`tests/test_binary_judge.py` 21 项通过，其中端到端一项跑在 #432 的 36 个公开证据槽位上，用假客户端；ruff 通过，mypy 全仓 155 个文件通过。
- 离线验证（假 judge 客户端，没有发出真实请求）：在 #432 的 36 个槽位上加 `--recording-root poa-432-impl/tmp/eval`，32 份报告的证据都和录制重新推出的结果一致。另把 F14 HolmesGPT run1 的原始输出当作 dv2-2b214b 的第 1 次送入。结果：OpsPilot 6 次被 Q11 否决，与 #432 的 gate 30/36 一致；根因证据引用率 24/28 = 0.857；裁决清单的格式也核对过。这些数字只是演示管线，假 judge 的结论没有含义。

## 待决

- 真实 judge 运行（S11）前，用户确认 `ZAI_API_KEY` 和 `OPENAI_API_KEY` 有效、余额够（H6）。预审 §4.1 第 10 项记录当时环境里只有 `GLM_API_KEY` 这个名字；本次没有复核。本工具只读 `ZAI_API_KEY`。
