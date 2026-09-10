# PR16 严格报告 runtime 离线候选

状态：offline-only，等待联合独立实现审查；未调用真实模型、trace、后端或PG，未操作环境、修改Git index或旧真实工件。本轮20HTTP已耗尽，旧质量FAIL不变。候选代码/数据worktree为m0-01；实际Holmes依赖只在离线探针中显式借用已存在的只读固定checkout/解释器，生产wrapper的ROOT/UPSTREAM路径行为不变，下一真实实验必须另固定实际执行位置与合同。

## 运行时接缝

默认m0-report-v2，显式--report-version m0-report-v1保留legacy路径，未覆盖任何v1 schema/报告。新版使用共享outcomes_v4.ModelReportV2/build_context/validate_report_context，不另建schema或freshness平台。本文只涉及wrapper/report_contract及运行时测试；完整v4 checker/bridge由合同作者负责。

每个真实准备发送的模型请求从registered hash匹配的实际view构造EvidenceContext，并将同一个catalog、per-view target_refs/view_hash/Timing、可信time_policies作为user业务JSON写进实际messages。其字节与token一并经过原512KiB/98304估计输入/32768输出预算，不在已裁剪14KB工具view后绕预算外挂。初始和动态均走同一共享context构造；scope过滤由allowed_scope强制，不因registry存在更多容器而公开它们。

--time-policy-file接受共享TimePolicy的JSON数组，无文件即空目录，不编造默认阈值或全目标授权。whole-scope policy须显式all_authorized_targets与匹配scope_revision；目标和policy由模型显式引用，bridge不补造。严格scope须显式非负control_generation。

wrapper记录新的operation_started_at并保持新record.observed_at同一开始语义；完整工具响应返回后才写collection_completed_at。模型请求在可信client dispatch开始前持久时间、完整响应返回后记response_received_at；两者是客户端界限，不是精确模型读取时刻。旧records不回写，不从observed_at/回放当前时钟补完成时间。

source_timing仅从实际displayed_logs的aware ISO timestamp、sampled_spans的start_us+duration_us提取可见事件最小/最大区间，basis=event_time；这不是连续coverage或全raw覆盖。Prom instant vector求值时间不当作底层sample时间，保持unknown。未知/坏格式保守unknown。

可选--initial-timings-file接收同evidence-timings形状的独立可信记录：view_id→{view_hash,timing}。必须匹配初始view hash，event_time区间必须与实际可见事件字段相符；不读question自述capture时刻作为权威。缺文件时保留初始collection unknown。当前不接未实现的initial source_sample_time/source_coverage证明，需另有适配证据；也不以该sidecar补造初始raw Artifact/manifest，严格bridge缺原始来源仍应拒绝/unknown。

## 新的安全业务记录

- configuration.json：report版本、time_policies及hash、可信scope/control_generation。
- time-policies.json：同一可信policy目录，不发明阈值。
- evidence-timings.json：只有registered实际view的独立view_hash/Timing记录；catalog不是其反向来源。
- delivered-business.json：实际messages/hash、evidence_context/hash、actual request ID、control generation、dispatch_started_at/response_received_at。
- report-capture-business.json：完整非空安全报告原文/hash、Run/step/actual request/gen、完整response接收时间；与原响应及结果由v4 bridge交叉核对。

runtime只解析完整报告并校验已交付scope/status/ref；完整时间适用性、输出绑定、人控终态由共享public-v4最高seam检查。investigation_returned不是quality PASS，也不是当前状态或恢复认证。

## 已执行离线检查

58项runtime/trace定向tests PASS、Ruff通过。旧v1测试改为显式legacy，没有改写旧报告。真实Holmes循环+httpx.Request的假pipe探针覆盖：

1. 初始/动态catalog和policy确实出现在payload，actual Content-Length、full wire hash、业务hash/context hash均匹配；同Run全部synthetic private字段保留，不入业务导出。
2. 模型显式选择实际view支持的target/time_scope_ref，完整summary/next_steps及报告capture保留。
3. 伪造target被拒；越scope初始view在任何模型调用前拒绝（0HTTP）。
4. 初始collection无证明保持unknown；独立hash绑定初始Timing可用；动态collection→dispatch→response顺序与单独timing记录匹配。
5. 实际旧log事件时间可提取，但不补旧collection；Prom求值/收取时间不伪作source freshness。

探针：tests/fixtures/m0_environment/strict_runtime_probe.py；输出round-02-strict-runtime-probe.txt。所有传输被fake_child替换，dotenv_values被合成值替换，无真实网络或凭据读取。命令使用HOLMES_TEST_UPSTREAM指定既有Holmes checkout，仅用于测试注入；不修改生产路径。该探针不是完整初始raw provenance或真实新版模型效果证明。

联合源码快照需在合同作者冻结outcomes_v4/holmes_bridge后一起生成，包含实际runtime依赖和schema hash，不能只封存wrapper。未执行新版真实模型/时钟接入，不改变原实验结论。
