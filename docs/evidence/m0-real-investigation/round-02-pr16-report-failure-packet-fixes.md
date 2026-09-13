# PR16 报告失败共同 packet 修复

基线`e5357a8`，comment `3977733293`。有界设计由root/fresh reviewer确认。0模型、backend、trace、PG操作；未提交；未跟踪error.log原样保留。旧真实原报告、证据与内容寻址schema快照不改。

## 失败证据

新增实际CLI subprocess矩阵：无initial（含已采集动态证据）、verifiedinitial、unverifiedinitial，分别组合malformed、实际empty字符串和length。每例经`python -m scripts.m0.holmes_bridge --output <临时文件>`。

先红`9 failed, 23 deselected in 0.90s`：无initial三例输出缺handoff（generic FINAL_RESPONSE_REQUIRED），其余丢Scenario/动态状态，empty被归一化成None。原因是所有报告失败都跳到_initial_handoff_packet，该helper依赖initial audit并清空Artifacts/Actions/Deliveries。

## 最小共同路径

删除_initial_handoff_packet旁路，共同_load_common核输入/scope、原始或导入Artifact、实际Action与各次业务Delivery，然后单独形成报告资格结果。strict解析仍复用现有parse_report；nonstop、empty、DSML、duplicatekeys、非法结构不能成为有效Report。报告失败时report=None、execution=blocked与明确handoff原因，既有证据和实际输入照留，不伪造completed。

逐次Delivery从原记录和安全response的run/ordinal、complete、identity核对确定状态；未完成/未知响应保留prepared或dispatched，不标response_committed。完整HTTP响应中业务报告非法，仍可保留已提交的响应交付事实，但不能成为合格ModelReport。绑定矛盾继续拒绝，不能用报告失败掩盖不一致。

ReportCapture.content与IncidentOutcome.report_content仅原始承载字段从非空Text改str：实际空响应保持`""`及SHA256空bytes；无响应保持None/hash None。ModelReport自身字段及协议不放宽。当前canonical v4 schema记新hash，旧快照未覆盖。

CLI reportless输出文件新增完整Scenario/Outcome，连同原有原report/input/hash、handoff和violations供审计；stdout排除完整scenario/outcome/input/report原文，仅保留摘要。没有把原始业务工件写进本记录。

## 作者验证

```sh
.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k report_failure_matrix -q
.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q
.venv/bin/ruff check scripts/m0/holmes_bridge.py scripts/m0/outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
.venv/bin/ruff format --check scripts/m0/holmes_bridge.py scripts/m0/outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
```

矩阵绿`9 passed, 23 deselected in 0.97s`；最终完整相关集`184 passed in 1.44s`，ruff与format通过。还覆盖先前Delivery保留、未知响应不committed、无响应None与空响应区分、原有效报告/显式legacy兼容。

共同路径使之前7个合成失败fixture的遗留矛盾暴露：只改result，却留成功response/capture，或声称模型前退出却留成功capture。测试现同步合成同attempt的result/response/capture，模型前用例删除其虚构响应文件；实际绑定拒绝没有放宽，真实工件没有修改。此前旁路忽略这些文件的行为未被继续保留。

最终冻结SHA256（交fresh独立复验，不以作者自测自称独立通过）：

- holmes_bridge.py：`5eda13d28d9d273688e6ffcabaffeb00c59929ff520da4c5b4a1ffacabb9ae9b`
- outcomes_v4.py：`15fe3c71db54574b5091f5d98d9ed49092bb5fb24b33f9d6810763de2ab8aeb7`
- tests/test_m0_holmes_bridge_v4.py：`51f1483a2d65196953a93f7b25e5520ccdafcb3b718145ac9544519b4cb5c347`
- IncidentScenario.v4.schema.json：`0468e2f7805aef189acfeea6196098ed6d5f97dcbed58ac40e6d51e3c19f81a0`
- IncidentOutcome.v4.schema.json：`84ed7977a33b503abd13ea2591ab85dbdba8584038ce0ddc438bb5a00864ad8b`

独立审查由fresh reviewer另写；root负责Git/PR与整体导航。当前仍是离线候选修复，不新增真实模型通过或改变历史qualityFAIL。
