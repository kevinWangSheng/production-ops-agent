# PR16 strict输入来源证明缺失修复

基线`96e89e3`，comment `3977946891`。只改bridge、v4校验及对应离线测试；schema/显式legacy v3未改；没有模型、网络、backend、PG或环境操作，没有提交。

## 失败与修复

bridge原`if provenance and ...`将缺文件和{}跳过，原question缺失同时也可留下None/None；direct v4 checker将两项同时None当作一致而允许通过。malformed/空文件又会在加载阶段抛异常，使已有packet无法到strict seam。

先红命令：

```sh
.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py -k 'provenance_is_required or cannot_both_be_absent' -q --tb=short
```

结果`10 failed, 8 passed, 49 deselected in 1.19s`：missing/emptyobject四例错误返回无违规；空文件/malformed四例JSONDecodeError；直接original/actual同时None两例误过。已有missing hash、wrong hash和只缺original的部分路径原本已拒，回归保留这些防线。

修复不再按truthiness判定来源证明是否适用。严格要求metadata为object，原始和actual两hash分别匹配实际保存的内容，缺文件、空/非法JSON、非object、缺hash、错hash或原question无法读取/解码都生成`input-provenance`审计项和明确strict违规；保原/actual输入、完整Report及其原文、Artifacts/Actions/Deliveries，不通过generic exception丢弃。

v4共享`input_provenance_errors`用于最终checker与investigator_input导出：original/actual content/hash任何必需项None形成INITIAL_INPUT_PROVENANCE_UNKNOWN；既有hash错配与audit验证继续执行。DTO仍允许缺失值以表达unknown，不能凭这种表示声称contract-consistent。schema和旧v3行为不变。

CLI对有违规但仍含schema有效报告的packet，也在本地输出文件保留完整scenario/outcome/input/raw report；stdout排除所有完整业务字段，仅输出摘要。有效normal/import仍保持原合格报告，不伪造handoff或模型结论。

## 作者验证与边界

最终参数化实际CLI覆盖normal（含动态采集）与verified import两模式的valid、missing、empty object、empty file、malformed JSON、non-mapping JSON、missing original、missing hash、wrong hash；断言输入与完整报告不丢，Artifacts/Deliveries各1，normal Action仍1，import不伪造Action。另有direct checker original/actual双None及输入导出拒绝测试。

既有纯合成strict正例此前没有原始输入证明，现在明确提供synthetic原/actual content及其hash，并将实际内容纳入合成delivery；normal磁盘fixture保存相应原文件/provenance。只完善替身输入前提，不改真实历史工件、字段或验收结论。

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q
.venv/bin/ruff check scripts/m0/holmes_bridge.py scripts/m0/outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py
.venv/bin/ruff format --check scripts/m0/holmes_bridge.py scripts/m0/outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py
```

最终205 passed in 3.46s；ruff/format通过。中途提取共享helper遗漏局部initial绑定导致18项NameError，修正后定向18PASS；最后一个测试局部import空行lint修正后通过。上述调试失败不冒充原finding的红证据。

冻结SHA256，交fresh独立复验（不是作者独立认证）：

- scripts/m0/holmes_bridge.py：`beb8393a29a597d61286b85b51d33ea299f2463db649d1e8588038f30f9b9b72`
- scripts/m0/outcomes_v4.py：`25e89209e92a62e3913ebe590df4747f6ab4fc469d6c60d0fc86d8c703c9f4bf`
- tests/test_m0_holmes_bridge_v4.py：`3957a714d38c627223565358c86ae21082750ca9a617976a14e0036421e004a2`
- tests/test_m0_outcomes_v4.py：`0727f2a2b40feefd9024a0660fe075e2d7aeb47896b3034b5b1ca8a7852d03a8`

root负责整体检查、source快照、PR与记录导航；fresh reviewer另维护独立结果。本修复不新增真实模型通过或改变历史qualityFAIL。
