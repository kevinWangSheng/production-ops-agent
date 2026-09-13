# 初始证据发送前范围与无报告CLI独立复核

基线PR16 `ced7fd4d64c0f3c91ad969a849efccbd56fe4cb1`。2026-09-10，接续独立初始证据审查，未参与实现。已通过GitHub API读取原评论3977531749/3977531762，并直接读取importer、runner及CLI。

两项发现成立：初始导入只校目标，原raw查询/原scope窗口及当前接口未在发送前检查；后置Outcome拒绝不能撤销已发送的证据。无报告handoff已是合法失败结果，但CLI仍解引用None，无法交付结构化失败。

修复验收范围：当前接口与窗口必须在初始组装/任何模型、工具或凭据读取前强制执行；raw.query与原scope均有范围意义，不以policy或补造时间代替。合法scope正例不误拒。CLI的import blocked与非法报告分支须真实执行，输出违规、原始输入/报告审计（本地完整工件），不能仅验证load_packet。

当前为预审记录，待稳定源码与实际受影响探针后追加结论。无环境、模型、后端、trace、PG或真实私有数据操作。

## 稳定候选独立复验

固定SHA-256：

|文件|SHA-256|
|---|---|
|scripts/m0_environment/initial_evidence.py|2eda43af210ee9146451ebb01689960b5d69f1b57d7fdbeab2e44819dadf493f|
|scripts/m0_environment/holmes_baseline.py|f7db2c7401693c61ed962d350d8a01a09205aa263da80d1703e093216f528543|
|scripts/m0/holmes_bridge.py|6b2b1db13d63d6609ec728978089763fb5638149c8e0b05b66fa20c8de6811fb|

亲跑固定Holmes解释器及`initial_report_probe.py`（HOLMES_TEST_UPSTREAM指向5e983c17固定checkout，dotenv及pipe仅使用合成替身）：合法单请求report仍通过完整strict checker，1假模型步骤、0工具、0真实HTTP。实际wrapper缩窄window与禁止otel_logs接口两个负例均在发起前返回unknown；逐项断言transport调用0、dotenv读取0、model_http_requests=tool_queries=0，且没有产生initial-evidence拷贝目录。已有报告missing/bad-hash/duplicate/mixed四项保真负例在同次探针仍通过。

亲跑实际CLI subprocess回归：`.venv/bin/python -m pytest tests/test_m0_holmes_bridge_v4.py -k cli_reportless -q`，3 passed，0.40秒。import_blocked与invalid_report退出1、stderr为空；assessment/conclusion为null，保存的--output包含原/实际input、report=None、原始非法报告内容/hash、blocked/handoff及违规；stdout不含完整输入/报告。合法report退出0且assessment/conclusion不退化。本复验运行真正模块CLI，不只调用load_packet。

后续受影响组合：`.venv/bin/python -m pytest tests/test_m0_initial_evidence.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_outcomes_v4.py tests/test_m0_runtime_strict.py -q`，58 passed，0.51秒。覆盖初始/动态context、完整报告、来源unknown、CLI、当前interface一致性和既有runtime规则。

本审查者另构造6组原query/原scope/current scope输入并断言：

- 原scope较宽但实际query完整落在当前窗：允许，返回原query边界100–200，不裁剪或用scope补造。
- 实际query比current宽：INITIAL_QUERY_WINDOW_DENIED。
- 实际query超原scope：INITIAL_ORIGINAL_QUERY_SCOPE_MISMATCH。
- query缺真实起点：INITIAL_QUERY_SHAPE_UNKNOWN。
- current只许traces而导入logs：INITIAL_INTERFACE_DENIED。
- services无时间查询语义：INITIAL_QUERY_TIME_UNKNOWN，未将scope.window冒充实际查询窗。

源码核查确认门槛位于原manifest/raw/query/hash绑定之后、投影/复制/组装之前；current和原scope都参与判定，不能只靠报告time policy。logs/traces精确query及service必须在双方范围；metrics按既有代理支持语法和窗限制处理，未建立通用PromQL或freshness证明。缺interfaces只代表已有固定四接口，显式收紧同时作用于Toolset、invoke、HTTP guard及bridge AccessScope；没有只限制initial而动态面继续暴露接口。bridge只消费helper已验证query_window，不再回退原scope窗口。

## 结论

**上述稳定源码在发送前范围门槛与reportless CLI的离线范围通过独立审查，无本组未处理P1/P2发现。** 两项原问题均获得实际受影响入口复验；此前后置checker成功不再被当成发送前权限证明。

没有新增真实模型、工具后端、PG或凭据访问；本次只审查新候选，原raw/view、历史源码快照和报告未改写。正确scope正例及保真unknown路径保持，结构通过仍不代表真实模型效果、完整报告质量或M1验收通过。最终交付继续等待最新提交CI及已触发外部审查闭环。
