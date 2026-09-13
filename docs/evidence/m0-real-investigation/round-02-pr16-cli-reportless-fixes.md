# PR16 reportless CLI 保真修复

基线`ced7fd4`，comment `3977531762`。仅离线CLI/合同回归，无PG、模型、trace或backend调用；未提交。当前scope导入前校验由另一实现者处理，本文不替代其证据。

实际CLI子进程（`python -m scripts.m0.holmes_bridge ... --output <临时文件>`）复现：import blocked和schema非法报告均已由loader形成structured Outcome，但CLI解引用`outcome.report.assessment_status`抛AttributeError，输出文件未落盘。先红`2 failed, 1 passed, 18 deselected in 0.40s`；有效报告分支原本通过。

最小修复给当前strict的reportless摘要两项值写null，不用completed/incomplete等默认值冒充模型结论。输出文件保留execution、handoff及原因、report:null、实际原report_content/hash、完整agent_input与原有violations；stdout继续排除完整原文/input，只输出摘要。有效报告和显式legacy分支字段行为保留。相邻CLI所有`outcome.report`解引用已核，仅这两处，都受存在性保护；未扩大通用异常捕获掩盖问题。

```sh
.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k cli_reportless -q
.venv/bin/python -m pytest tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py -q
.venv/bin/ruff check scripts/m0/holmes_bridge.py tests/test_m0_holmes_bridge_v4.py
```

绿：3 passed / 18 deselected in 0.50s；相称合同50 passed in 0.55s；ruff通过。测试均使用合成证据与临时输出文件，保原CRLF input、实际assembled input、schema非法报告原文/hash、UNVERIFIED_INITIAL_EVIDENCE及MISSING_REPORT_OR_HANDOFF，并确认stdout不含完整业务字段。有效报告保持原assessment/conclusion及空violations。

CLI初次稳定SHA：bridge `26692132dfc9090c2042b7517501bb054fd5cc8a4101d2ebb1b35baf1922efd0`；test `fd339e454a4d6610bc9265244358ef5eb9ff0d762743570172925e4154e34272`。已交fresh reviewer独立复验；此处为作者自测，不自称独立通过。

## 与当前scope导入门槛对齐后的最终冻结

为配合comment3977531749的导入前验证，初始Artifact.window仅消费共享verify_initial_entry返回的query_window（精确原raw.query区间），移除raw缺字段时退回原scope.window的推断。AccessScope.interfaces保留当前scope明确收紧的接口列表，缺省仅沿用固定四个只读接口。source授权门槛由runtime作者的helper执行，bridge不另起竞争时间解释。

导入检查加强后发现旧合成trace正例仍使用metrics的query字段形状，2个正例被拒；只修正合成fixture为真实trace的service/start/end结构，不改历史真实原记录。新增接口收紧正反例并断言Artifact窗等于原实际query。

最终相称命令为前述bridge/v4合同再加`tests/test_m0_initial_evidence.py`，结果`63 passed in 0.62s`；ruff通过。最终代码交fresh独立复验，替代前面的CLI初次快照：

- holmes_bridge.py: `6b2b1db13d63d6609ec728978089763fb5638149c8e0b05b66fa20c8de6811fb`
- tests/test_m0_holmes_bridge_v4.py: `55618a66b4bb64416e41b3c76bb6e423315de2e459bdc5dd8508ca6e530073c6`

没有schema/PG/预算变更，也未重新访问旧报告的私有响应。
