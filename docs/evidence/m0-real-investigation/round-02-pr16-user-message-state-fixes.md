# PR16 完整用户消息与完成状态修复

基线`65d3624`，comments `3978469715/3978469724`。本组先核实际runtime/projection/contract并由delivered_input_state_review独立批准有界设计，随后实施。root决定状态仅补单向约束，不从模型自述改可信状态。无真实模型、网络或环境操作；Git由root统一处理。

## 获审设计

- 当前v4 Delivery增加可空final_phase以保真表示缺metadata；值只能来自实际runtime发送记录，不能按wire中是否有指令猜测。旧缺失明确strict unknown，旧v3行为/schema快照不改。
- 配置/版本向量记录固定report_instruction(final=True,report-v2)的SHA256；checker同时比固定纯生成器的真实字符串/hash，不接受任意配置和任意注入字符串互签。
- 对每条持久delivery验证所有user消息的完整结构和顺序：actual原输入、本次完整EvidenceContext、仅final_phase可追加exact固定closed-report instruction。拒绝任何extra/重复/旧context/content blocks/附加字段。assistant/tool历史仍由既有业务投影与注册证据边界处理，不新增私有字段或内部顺序限制。
- envelope-v1只接受其明确的actual_user_content/context/evidence_views结构，合成用例显式final_phase=false；不借合成envelope逃逸额外输入检查。
- assessment_status=completed要求trusted与outcome execution=completed，涵盖partial/inconclusive。反向不强制：既有execution completed配incomplete/inconclusive/gaps/handoff代表有界执行已结束而调查仍不完整，继续合法；不从报告改写可信execution。

实施者分别负责：本Agent拥有v4/bridge/schema/tests；report_calibration仅接runtime config固定hash与每delivery真实final_phase字段。原source/schema内容快照与历史报告不重签。后续红绿与冻结hash在本文件追加，独立审查另记。

## 实现与红绿

v4新增Delivery.final_phase: bool|None，bridge仅从实际delivery记录读该字段（非法类型按unknown），版本向量从config读取report_instruction_sha256。checker强制该hash等于当前固定report-v2 pure generator的字符串hash；并逐个delivery核所有user消息的整个有序列表与完整role/content结构，不仅过滤相同actual content。Context按runtime实际固定序列化格式核对；额外字段、content blocks、已知指令出现在错误阶段、旧context或新增用户消息均不能获得放行。已有actual缺失/重复绑定检查保留，assistant/tool业务/私有协议处理不变。

状态只增加assessment completed⇒trusted/outcome execution completed。completed+partial/inconclusive仍可接受；反向组合execution completed+incomplete/inconclusive/gaps/handoff保留既有语义。本修复不根据报告assessment改写trusted execution，不伪造blocked/failed，也不修改原报告。

初次定向红`7 failed, 1 passed, 117 deselected in 0.36s`：4个completed partial/inconclusive+failed/blocked确实错误[]；额外user字符串确实错误[]（单独复跑亦1fail）。content blocks/extra字段用例用于新完整结构诊断，部分已有legacy DELIVERY_INPUT_MISMATCH拒绝，不能把所有新错误码缺失都说成先前绕过。单独合法completed执行+incomplete调查原本通过，并在修后保留。

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py -k 'all_delivered_user or completed_assessment_requires or completed_bounded_execution' -q --tb=short
.venv/bin/python -m pytest tests/test_m0_outcomes.py tests/test_m0_outcomes_v3.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py -q --tb=short
.venv/bin/ruff check scripts/m0/outcomes_v4.py scripts/m0/holmes_bridge.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
.venv/bin/ruff format --check scripts/m0/outcomes_v4.py scripts/m0/holmes_bridge.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py
HOLMES_TEST_UPSTREAM=/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4 /Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment/tmp/m0-environment/holmes-venv/bin/python tests/fixtures/m0_environment/initial_report_probe.py
```

最终六组278 passed in4.29s；ruff/format通过。新增ordinary/initial/reportless/multistep正反例、wrong/missing phase、missing/self-signed instruction hash、missing final instruction、old context、synthetic envelope额外字段；旧metadata不足保原packet并UNKNOWN，不从wire反推。

实际Holmes代码经fake pipe的report-only初始证据正例checker=[]，并保留4个缺/坏initial报告原文及2个范围/接口拒绝的0transport结果。全部0真实HTTP/模型。runtime作者已单独验证多步实际phase false…true与config固定hash，未修改body/private/history/execution；其记录独立维护。

schema首次生成误用系统python导致ModuleNotFoundError(pydantic)，未改任何依赖；改用既有.venv后生成成功。当前canonical Scenario schema新增可空phase，历史schema内容寻址快照保留；Outcome schema未变。旧合成正例显式提供固定hash/phase及runtime实际context序列化，不重签真实历史运行。

## 最终冻结

- scripts/m0/outcomes_v4.py：`ae3cda21b274bc61c17cfb28abbe406a5edebffe0680f61cf3f4d8a3ef3e0b69`
- scripts/m0/holmes_bridge.py：`e303cbadef1f4805b9a11695134bec2ffc44dc1219640e1d9cc7a978055100b6`
- tests/test_m0_outcomes_v4.py：`280f7c30fd7c7c52ac856b79c2938119802046208d6bffc81c6d4aec0a69c98d`
- tests/test_m0_holmes_bridge_v4.py：`86eb44fceeb9ee2e8a75bd082c0eaaf48d8cdc5bc959c016cac20bdf56a41d85`
- IncidentScenario.v4.schema.json：`b8b28f6fe59072388a128b70e97d21af0d2ea68c80bc97cd0d6a6ff9c10ca3c6`
- IncidentOutcome.v4.schema.json（未改）：`84ed7977a33b503abd13ea2591ab85dbdba8584038ce0ddc438bb5a00864ad8b`

以上为作者验证并已交delivered_input_state_review独立复验；不自称独立通过或真实模型验证。root负责整体fullcheck/快照与PR。
