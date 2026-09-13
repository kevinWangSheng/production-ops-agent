# 初始证据与版本接缝独立终审

2026-09-10。独立审查者接续同一新上下文设计审查，未参与实现。依据[设计与活动发现记录](round-02-initial-evidence-design-review.md)复核稳定实现，不沿用实现者通过摘要。没有真实模型、后端、trace、PG、Colima操作或真实私有协议/凭据读取。store当前final指针的实际PG复验由[独立存储审查](round-02-final-pointer-review.md)负责，本记录不替代其证据。

## 固定版本

|文件|SHA-256|
|---|---|
|scripts/m0/holmes_bridge.py|765ecd5cc06a9aa08e2f31a179d66f5b3ac14c59bdc14f8f673d6e7a93b6c5b4|
|scripts/m0/outcomes_v4.py|880b924b9ebaaa03a1ee032e2e3753ef2ae312d27afd59b800ae47ceb6c2fb86|
|scripts/m0_environment/initial_evidence.py|3cbe4be735540b784a8b66a12c9fdac3d7b685dcf602804f5e568359221c0055|
|scripts/m0_environment/holmes_baseline.py|db7f76db0e751422d74d0b52270e52a962e2d93a8958ae9aafb89f0c289321cf|
|scripts/m0_environment/report_contract.py|0714c3ed612f362475a86af74c2dfe8034c8d05de709d03e22c0af409be47fda|

## 亲自执行的证据

定向命令：`.venv/bin/python -m pytest tests/test_m0_initial_evidence.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge.py tests/test_m0_outcomes_v3.py tests/test_m0_runtime_strict.py -q`。结果94 passed，0.40秒。

真实固定Holmes循环探针：使用环境worktree既有`holmes-venv/bin/python`运行`tests/fixtures/m0_environment/initial_report_probe.py`，`HOLMES_TEST_UPSTREAM`明确指向既有`holmesgpt-5e983c17f30e93099c7d775167266d4cd1d586c4`。pipe/dotenv均替换为本地合成；运行成功，1个假模型步骤、0工具查询、0真实HTTP，1个初始view通过runner→持久记录→bridge→strict checker，违规为空。

随后探针对同一已取得的有fact完整报告，分别破坏initial manifest为missing、bad-hash、duplicate、mixed：四项均保留完整report对象和原文，并得到UNVERIFIED_INITIAL_EVIDENCE。该负例没有改为无claims报告来回避资格失败。

另由本审查者在临时fixture独立构造并断言：

|反例/对照|实际结果|
|---|---|
|保留原文/capture，删除result.final_report解析副本|严格重建通过，完整fact报告保留|
|只改config.upstream_commit|版本向量改变|
|只改config.tool_schema|canonical tool hash改变|
|删除两项执行版本来源|EXECUTION_VERSION_UNKNOWN，报告仍保留|
|报告JSON重复key|report=None、原文原样保留，严格拒绝|
|finish_reason=length|report=None、原文原样保留，严格拒绝|
|报告含DSML标记|report=None、原文原样保留，严格拒绝|
|用lossy view替raw并更新自述hash|INITIAL_PROJECTION_MISMATCH|
|缺失原source文件|INITIAL_SOURCE_MISSING|
|改collection_completed_at违背原raw|INITIAL_COLLECTION_TIME_MISMATCH|
|原source改为CRLF并固定实际字节hash|精确复制与投影通过，未归一化字节|

原/实际question区分、未实际送达的user内容拒绝及非法/重复ID保全由上述定向运行覆盖。活动阶段另亲跑完整CRLF raw/view/manifest/source/question逐件字节比较成功，当前源码仍使用read_bytes/二进制copy及原内容hash；未通过新签名把裁剪view变原raw。

## 源码与兼容性核查

- manifest来源由显式可信CLI指定；验证原raw/view/manifest的字节hash、canonical hash、scope/query与冻结纯投影结果。新Run只复制原文件，不移动原数据，导入不生成query action。
- 单步report缺observations等于零动态查询；active缺动态记录仍不被默认为成功。初始view只从实际顶层user业务视图核对，不搜索任意遥测嵌套ID。
- 单一context同时绑定registry、source、dependency hashes；active与initial的依赖也比较，不仅比较wrapper hash。不兼容/缺失材料进入审计unknown，不伪造Artifact。
- 原question字节与manifest装入后的实际user内容分别保存hash；最高接缝核实际物理请求含该user内容。旧时钟语义保持，缺失采集完成时间不由导入时间补造。
- 原文解析与事实资格分离；schema合法但引用/来源资格失败仍保留解析候选。无解析副本可从已绑定的原文重建；存在副本必须相等。重复key、非stop、DSML及非法schema不能通过Pydantic last-wins升级为有效报告。
- blocked原始审计分支没有声称原文已获得响应来源认证；report=None且strict违规保留，不能成为有效候选通过。

亲自将当前三份Scenario/Outcome/ModelReport schema与DTO导出逐对象比较，完全一致。旧v3 Scenario/Outcome、v1/v2 ModelReport文件逐字比较基线6eab0a4不变；`runtime-sources/*.schema.json`旧内容寻址快照逐一核文件名hash及基线字节一致。v3源码仅新增默认空的私有初始ID参数，默认旧路径行为由本次旧回归实际验证；不能称当前源码文件完全没变。

## 结论

**上述稳定候选的初始持久证据、单请求报告重建、未知保真、实际输入可见性与执行版本绑定，在本次离线范围通过独立审查，无未处理P1/P2发现。** 活动预审的“资格失败缺解析副本导致丢报告”问题已复验关闭。

此结论不代表新版真实模型效果、自然语言报告质量、真实后端/当前freshness、产品验收或M1入口通过。混合context仍是显式有界不支持；缺来源、时间、版本或协议合法性仍unknown/失败。原真实报告质量FAIL和已用请求预算不改。本记录可用于本组PR审查闭环，最终交付仍须覆盖最新提交的CI及外部已触发审查。
