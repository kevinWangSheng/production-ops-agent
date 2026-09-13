# 初始持久证据 runtime 修复（离线候选）

本组依据round-02-initial-evidence-design-review.md已批准方案。未执行真实模型、后端、trace、PG或环境操作，未修改旧raw/view/schema/source，也未操作Git index。旧报告质量结论不变。

新增可信CLI参数--initial-evidence-manifest，版本m0-initial-evidence-v1；包含一个原ProjectionContext以及entries的原raw/view/manifest路径与exact SHA256、投影revision和可选独立Timing。路径由operator明确选择，不从日志内容读取。initial_evidence.py::verify_initial_entry供runtime和bridge共用：核ID、原file/canonical hashes、query/scope、原registry与源码/依赖、冻结投影重放、当前授权范围；可见事件Timing须匹配实际字段，已有raw时钟不能冲突。缺Timing时只取既有raw/可见事件字段，不用当前时钟或query window补造。

import_initial_evidence验证后将exact bytes复制到新Run initial-evidence/，源文件不移动、不改写。initial-evidence.json保留原与复制后的ProjectionContext及条目；单context外的组合明确unknown（active+旧source也不偷偷改用新算法）。原source/dependency按SHA复制，无一般存储或每artifact多context平台。

原question UTF8 bytes在question-original.txt保存；原manifest（即使无效）在initial-manifest-original.json保存。input-provenance.json分别记录original_user_content_sha256与actual_user_content_sha256。无原views时仅在合法显式manifest后装入business_tool_views；原question已有views则必须逐项一致。actual内容才进入input-business.json与真实wire/hash。

observations.json在Run开始就初始化[]，仅记录本Run动态查询；import不生成query action。缺失/重复/坏ID/不匹配/混合context产生initial-import-audit.json的location/content/hash/reason，不伪造Evidence ID或Artifact。runtime在不合格初始来源时保存input/config/result(incomplete、0HTTP)并在读凭据前停止；已有报告的保全文unknown由bridge审计路径负责。initial-timings-file不能替代raw来源。

另外按独立预审发现，解析报告与资格分离：schema合法报告先完整保存final_report，再校验refs/context；不合格仍incomplete，但完整summary/claims/gaps/next_steps及原文/capture不丢，不能由bridge填补目标。schema本身不合法时原正文仍保留。

## 验证

- 65项import/runtime/legacy/trace定向tests通过、Ruff通过。
- tests/fixtures/m0_environment/initial_report_probe.py通过真实固定Holmes循环/实际httpx.Request与假pipe，1模型替身响应、0工具查询，合法原raw+view+manifest经import，实际user初始view进入Scenario，最后check_outcome=[]。原CRLF question与原raw字节一致；observed_actions=[]，不把导入伪装查询。输出round-02-initial-report-only-probe.txt。
- 动态runtime假pipe继续通过；原无raw/manifest的初始view与仅timing情况改为保真unknown、模型前0HTTP停止，旧测试历史输出没有改写。错误target的schema合法报告仍保留解析对象。新输出round-02-initial-runtime-probe.txt。
- importer负例覆盖missing raw、missing manifest、duplicate/malformed ID、mixed source、scope拒绝及原manifest/输入保全。

实际新版模型调用未执行；这里是离线结构与fake-transport链路证据，不是新的模型质量或M0/M1通过结果。联合source/schema快照待实现独立复验后冻结，不能覆盖旧snapshot。

最终补核：active+initial同时比对实际wrapper及legacy/trace依赖SHA，不仅比主文件；report-only仍合法使用原producer context。定向回归现66 passed。真正report-only probe进一步在已生成的schema合法fact报告上逐一模拟missing index、bad raw hash、duplicate、mixed context：loader均保留完整fact report/raw content，最高checker均返回UNVERIFIED_INITIAL_EVIDENCE，没有FileNotFound或丢失报告。所有破坏仅对探针新建临时合成数据，原真实工件不动。parse_report共享入口也由bridge/v4复用，避免duplicate-key/DSML解析差异。新输出仍是离线fake-transport证据，0真实HTTP。
