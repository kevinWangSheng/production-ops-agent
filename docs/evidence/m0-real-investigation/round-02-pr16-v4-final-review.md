# PR16 v4 严格接缝联合独立终审

2026-09-10 UTC。独立审查者未参与实现，在原有全新审查上下文中接续设计、活动反例与稳定候选复验。设计历史保留在 [设计审查](round-02-pr16-full-report-design-review.md)，runtime单独证据见 [运行时独立复验](../m0-real-environment/round-02-strict-runtime-review.md)。

## 覆盖的稳定版本

|文件|SHA-256|
|---|---|
|scripts/m0/outcomes_v4.py|ab47bf8c4e5f968771ff9980c200f9be54c68b61f528abccd54e2bee3b42896d|
|scripts/m0/holmes_bridge.py|bc257b2b6f0095b72148d1f790aefc352766b0f0d999701c3a65a898b9196f17|
|scripts/m0_environment/holmes_baseline.py|89e4a26d5c997f734cf6a0386733fb3e1528fc12bc82a7623ecb85d117328d85|
|scripts/m0_environment/report_contract.py|f5d0adf01c48b0ad404f3737ee9d9d81598f38c583f1f15f2c9b3b125267546e|

复验结束再次计算上述hash未变化。三份新schema与对应DTO的`model_json_schema()`逐对象精确比较一致：Scenario `2cbeb855298c9c29a4e3d6012b6dd73ee6affd1524382fb56fb20cb1e6e7046d`；Outcome `e019fee6a355f2c586389c991a2887bf37bfc1caec9fcc538eed852bf014b53a`；ModelReport `11508bf884c65c045aa0963bf5305ba2a943bda3e1aef821b7e2bef06e398d4b`。

`git diff --exit-code`确认旧v3 Scenario/Outcome、两份v1 Report schema、`outcomes_v3.py`、`step_store.py`未变。本记录不使用实现者的测试摘要替代下面亲自执行的证据。

## 亲自执行的确定性检查

```sh
.venv/bin/python -m pytest tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v3.py tests/test_m0_holmes_bridge.py -q
```

结果：62 passed，0.21秒。涵盖完整报告与原输出绑定、三类事实共同target/status/freshness、历史/当前、context实际交付、初始context、末次人控、严格/旧版桥接。runtime另亲跑10项定向测试与固定Holmes假传输4组，以及超大目录真实wrapper发起前拒绝，详见独立runtime记录。

此外以`strict_packet/checked`在内存独立构造并断言9组外部Scenario/Outcome输入（不改实现）：

|输入|实际结果|
|---|---|
|完成但无claims，所有报告时钟缺失|REPORT_TIME_UNKNOWN|
|hypothesis带不存在target/time引用|UNRESOLVED_TARGET_REF、UNRESOLVED_TIME_SCOPE_REF|
|current事件范围超出实际query/授权窗|FACT_FRESHNESS_OUTSIDE_QUERY_WINDOW|
|捕获报告hash篡改|REPORT_OUTPUT_BINDING_MISMATCH|
|改报告physical request|REPORT_DELIVERY_MISMATCH及可见性/输出绑定错误|
|目标不属于选定时间policy|CLAIM_TIME_TARGET_MISMATCH|
|最后accepted cancel仍完成|CONTROL_REPORT_NOT_AUTHORIZED、CONTROL_STATE_MISMATCH|
|合法历史窗在2035重放|无违规，未因复审时间被误判stale|
|current缺显式threshold|FACT_FRESHNESS_UNKNOWN|

活动预审发现的三个缺口均得到稳定版本复验关闭。源码核查表明完整summary/next_steps由嵌套report、原文/hash、可信capture和精确物理请求共同绑定；所有三类事实性声明走同一来源/目标/时效判定。存在引用的假设也检查引用可解析性，但没有把假设强行认证为事实。

## 真实旧报告保真与版本边界

亲自读取`tmp/m002-v4-replays/`下4份完整本地JSON，按run_id定位环境worktree原`result-business.json`，逐字比较`final_business_content`、解析对象、summary、next_steps，并重新计算原报告SHA-256；全部相等。

- normal03：summary 1218字符，next_steps 4条，16个原claims的target缺失保持缺失。
- fault01：summary 899字符，next_steps 4条，原claims未填造target/target_refs。
- 两份strict兼容与两份显式legacy回放均`strict_scope=unknown`、`strict_freshness=unknown`、`current_acceptance_pass=false`。
- 4份完整本地回放权限均0600；tracked同名JSON不含原文或完整report对象，仅元数据/计数/本地路径/hash。重新计算完整文件hash与tracked元数据一致，符合原始业务JSON不Git发布边界。

亲自再次执行`load_packet`与`load_legacy_packet`，读取两实际Run的安全业务工件；投影指定冻结`22a96b87cf6575c8246abeb7edbed5101e1c6951fcc62b5e8243b6e589c6efd8.py.txt`及其hash。结果：默认严格入口均拒绝`LEGACY_REPORT_REQUIRES_EXPLICIT_REPLAY`；显式旧v3投影与结构检查均通过，所有旧claim.target仍为None。结构重放成功不改变原完整报告质量FAIL，且没有重签旧view、重写原报告或把工程目标当模型结论。

## 冻结包与结论

v4候选包与v3逐行比较，正常/故障各2 Run、300秒窗、16 HTTP总上限、每Run/工具/字节/token/时间上限、P1/P2全报告要求、失败进入分母、非退化规则与后续72小时要求均保留。新增完整输出/目标/时效/人控规则没有降低原门槛，也没有默认批准新freshness数值。

**此稳定候选在所述离线合同、运行时替身及旧报告重放范围内通过独立审查；本范围无未处理P1/P2发现。** 可据此完成v4/schema冻结和PR检查闭环。

残余边界：新版真实模型、真实后端与新时钟采集尚未运行；初始raw完整来源不足仍不能靠timing sidecar补造；Prom sample age无证明时仍unknown；结构化target/时间正确不证明自然语言数值、因果或“currently”措辞正确。没有执行PG测试或启动环境，PG发起/采纳屏障不由本次离线成功替代。新模型请求、自然语言候选质量、M0/M1/产品passes均未获通过或授权。
