# #250 独立验收歧义

依据：`docs/tasks/2026-10-14-eval-pr-b3-interface.md` r11 与 r9。

1. **返回结果的 code 与异常链。** r11 写 `status=invalid、code=SCHEMA_INVALID、cause/context 为空`，但公开入口返回 observation，而非异常。测试沿用既有验收支持材料的公开观察面：`observation.result.status` 与 `observation.error_code`；任何抛异常均导致失败。正常返回对象不凭空增加异常链属性的断言。若需要检查内部被捕获异常的链，需另行定义公开观察面，不能读取实现私有异常。
2. **可选投影引用的 null 范围。** r9 允许“投影可选引用ID及service_version”为 null，但同段同时列出 `time_scope_refs[]` 与指纹，未逐项给出 nullable 表。当前逐一验证九个标量槽：evidence_id、operation_id、run_id、subject_id、target_id、trace_id、span_id、parent_span_id、service_version，并验证报告 time_scope_ref；不推定引用数组、数组元素或指纹也可 null。诊断 service/operation 属于响应而非当前 JudgeInput，不纳入 r11 请求测试。

## 输入与执行边界

- 六个 PR-A 冻结包使用合同所指 ledger 中的全部 76 个 evidence view，逐个核对冻结 view_sha256，然后放入既有 JudgeInput 构造器生成的规范外层；不发送 ledger 的其他容器。这验证全部冻结投影的兼容性，不声称重放原事故。
- 已认证路径经公开校准入口和既有独立支持材料生成真实 VerifiedCalibration，全部使用假 transport；未认证路径传 None。请求 transport 记录调用数和 body 字节，模块级网络防护阻止连接。
- 测试不标 xfail，不依据当前实现修改合同断言。仅执行新增测试文件；格式化后运行该文件的 ruff check 与 ruff format --check。
