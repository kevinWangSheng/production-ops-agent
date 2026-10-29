# P7 合同 r3 独立验收：歧义与采用的读法

依据仅为 `docs/tasks/2026-10-18-eval-p7-calibration.md`「合同（r3，2026-10-22，重做 #295）」P7-D17–D21，以及它修订的 PR-B3 接口与 P7 驱动接口。读过 r3 之前的源码以了解公开接口，未读 r3 实现。r3 未给出的名字集中在 `eval_p7_support.py` 末尾一个块里（`MODEL_VERDICT_FIELD`、`VERDICT_MATCH_FIELD`、`TRANSPORT_EXCEPTION_FIELD`、`tuning_debug`），实现若选了别的名字或机制，lead 只改这一块。共 **11 条**。

## 1. D18 观察字段的名字与位置

- 原文：「观察中保存模型 verdict 与是否一致」。
- 采用：放在 `JudgeObservation` 上，`model_verdict: bool | None` 与 `verdict_matches: bool | None`；没解出有效答案时两者为 None；`absent_ok`，旧工件解码为 None。`result.verdict` 改存重算值。
- 未断言：放在 `JudgeResult` 或 `CalibrationObservation` 上的写法。

## 2. D18「重算 verdict」指哪一个值

- `recomputed_verdict` 现为完整 AND（诊断规则、事实证据资格、gate）。D18 写的是「judge 诊断经确定性规则重算」。
- 采用：`result.verdict` 等于诊断规则重算（PR-A `_diagnosis_pass`）。测试只用 gate 通过、证据合格的样本，两种读法在这些样本上结果相同。r8 第 130 行「原始 True、复核 False 记 P6_RECHECK_FAILED」在资格或 gate 失败时还要不要保留，没有测试。

## 3. D18 与 PR-A `evaluate`

- `runs.evaluate` 对手工构造的 JudgeResult 仍做 verdict 复核，报 JUDGE_VERDICT_MISMATCH（`test_eval_contract.py:996` 未改）。`judge_once` 产出的 JudgeResult 现在本身就带重算值，不会再触发这条。D18 只取代 B3 的「P7 只计原始 verdict」，因此没有放宽 evaluate。
- `OBSERVATION_ERROR_CODES` 白名单保留 JUDGE_VERDICT_MISMATCH，这样旧工件仍能读回。

## 4. D17 的 200 字符上限

- 原文：「quote 取……一段逐字短语（不超过 200 字符），输出校验仍要求 quote 是该字段文本的子串」。
- 采用：200 只是 PROMPT 里的指引，不是输出校验。测试只要求 PROMPT 写明「verbatim」与「200」，并且旧的整字段约定措辞已删除。超过 200 字符的子串引文是否判无效，没有测试。

## 5. D17 也用于驱动 debug 的 `label_comparison`

- r3 只写校准时的漏诊判定。调参达标线「零漏诊」要在 debug 上量，所以测试要求 `label_comparison.key_hit` 同样只按 location 判（`test_d17_debug_label_comparison_matches_by_location`）。lead 如果认为这超出 r3，可以删掉这一条。

## 6. D19 的措辞断言

- 只断言稳定的关键词：「failing dependency is」「confined to」「dominated by」（合同原文给的英文示例）；`infer` 与 `method` 同时出现；`propagat` 与 `origin`；`rejected`、`ruled out`、`excluded` 三者之一；用引号括起的 so，或「so clause」「so-clause」。另外断言调参第 1 轮的「caveat on method」已删除，因为它与 r3 规则 2 矛盾。
- 未测：新 validation 盲标说明使用同一段文字（盲标包已经复制整段 `judge.PROMPT`，不需要单独的接口）。

## 7. D20 报告字段块的纯文本格式

- 采用：仍用 `REPORT_FIELDS_HEADER` 与 `REPORT_FIELDS_FOOTER` 定界，`eval_b3_support.user_payload` 仍按 `report_fields_block(sent)` 校验整块。块内只断言以下几点：各 location 标签按报告顺序出现；每个字段原文逐字出现在它的标签之后；claim 的 evidence_ids 出现在对应段内；块内没有 `\"`。不规定分隔符和行格式。

## 8. D20 传输异常类名

- 采用：`JudgeObservation.transport_exception: str | None`，取值为异常类名（裸名，或以 `.类名` 结尾的限定名都接受）；不记录异常消息；该值要通过校准元数据标识校验；成功时为 None；旧工件解码为 None。
- 未测：先断连、重试后成功时是否记录第一次的类名；超时时记录什么。

## 9. D20 原始输出落盘的范围与位置

- 原文：「答案被拒时保存原始模型输出到私有工作区」。
- 采用：debug 与 formal 都要保存；文件在 `tmp/eval-p7/` 私有根下，权限对组和其他用户关闭，并且被 git 忽略；stdout 不出现原文。不规定文件名和目录。如果实现只在 debug 落盘，`test_d20_formal_keeps_rejected_raw_output_in_private_workspace` 会失败，需要 lead 判断合同是否要求 formal 也落盘。
- 「被拒」只测了 JUDGE_OUTPUT_INVALID 这一类（内容不是 JSON）。

## 10. D21 调参的 CLI 机制

- 采用：沿用 `debug --item ...`。已被 formal 暴露的 validation 项可以作为调参项发送；从未进过 formal 的 validation 项仍报 VALIDATION_EXPOSURE_FORBIDDEN 并零调用；调参使用照常登记为 `kind: debug` 行；之后任何 formal 仍报 VALIDATION_EXPOSED。另一个工作区把 formal validation 族作为 debug 族的情形，由原来的拒绝改为允许（已修改 `test_eval_p7_driver.py` 中对应用例）。
- 如果实现改用显式开关或单独命令，只改 `tuning_debug`。

## 11. D21「旧约定工作区」

- lead 任务书：「old-convention workspace does not block tuning debug but formal/blind-packet integrity checks still hold」。r3 原文没有提到约定。
- 采用：盲标包 `convention` 不在当前 PROMPT 中时，debug（调参）不再报 JUDGE_PROMPT_CHANGED；formal 仍报 JUDGE_PROMPT_CHANGED；debug 对 items 或盲标包字节被篡改仍报 PACKET_STALE（即使约定是旧的）；export-blind 的 CONVENTION_MISMATCH 不变。因此删除了 `test_debug_exported_packet_bindings_refuse_without_side_effects` 的 `prompt` 参数，改为 `test_d21_old_convention_workspace_tunes_but_never_runs_formal`。
