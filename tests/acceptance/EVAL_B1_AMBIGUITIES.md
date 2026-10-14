# PR-B1 验收测试歧义清单

依据：`docs/tasks/2026-10-14-eval-pr-b1-interface.md`（下称 I，含 lead 更新后的第 82 行）。每条写接口原文、两种读法和测试采用的读法。

1. **StorageError 的导入位置**
   - 原文 I:8「以下均从 `opspilot.evaluation` 导出」；I:78 的 `StorageError(ValueError)` 写在代码块外面。
   - 读法 A：StorageError 也从 `opspilot.evaluation` 导出。读法 B：只在 `opspilot.evaluation.storage` 里。
   - 采用：先从 `opspilot.evaluation` 导入，失败时再从 `.storage` 导入（`eval_b1_support.py`）。

2. **一个文件同时违反多条规则时报哪个码**
   - 原文 I:80-89 只列了各个码的触发条件，没有规定优先顺序。
   - 读法 A：按某个固定顺序报第一个违反项。读法 B：由实现自定。
   - 采用：每条测试只制造一处违反。派生字段由测试侧的 `summary_doc()` 用 PR-A `evaluate()` 重新封好；`test_summary_file_layout_matches_contract` 核对这个辅助函数的输出与 writer 输出逐字段相等。唯一的假设是严格解析（类型、bool、未知字段）先于外置 ref 内容比较。不先解析就无法比较，所以这个假设成立。

3. **来源角色 trace 被标为 required 时的错误码**
   - 原文 I:72「trace.entry 属于来源角色……时不得 required」。
   - 读法 A：IDENTITY_MISMATCH（I:86 的 trace 绑定错）。读法 B：SCHEMA_INVALID（I:82 的 DTO 约束）。
   - 采用：两者都接受（`test_required_source_trace_rejected`）。

4. **顶层 trace.entries 与 capture.traces 不一致时的错误码**
   - 原文 I:58「loader 严格核对其结构与一致性」，没有给码。
   - 读法 A：SCHEMA_INVALID。读法 B：IDENTITY_MISMATCH 或 DERIVED_RESULT_MISMATCH。
   - 采用：只断言抛出 StorageError（`test_trace_entries_must_project_capture_traces`）。

5. **delivery ref 的字节和 hash 正确、但对象内容与 capture.delivered 不同时的错误码**
   - 原文 I:62「必须与 capture.delivered 同键且对象内容完全一致」，没有给码。
   - 读法 A：IDENTITY_MISMATCH。读法 B：HASH_MISMATCH 或 DERIVED_RESULT_MISMATCH。
   - 采用：只断言抛出 StorageError（`test_delivery_ref_content_must_equal_capture`）。

6. **reader 遇到本地路径是符号链接时的错误码**
   - 原文 I:68「reader 拒绝本地路径的任一符号链接及非普通文件」，没有给码。
   - 读法 A：EVIDENCE_UNAVAILABLE（文件不可读）。读法 B：SCHEMA_INVALID（ref 路径格式）。
   - 采用：两者都接受（`test_symlinked_summary_path_rejected`）。

7. **summary 的冻结 spec/answer 与对应 journal 不同时的错误码**
   - 原文 I:74「summary 必须有相同 started=True journal，冻结 spec/answer、身份/槽位相同」，没有给码。
   - 读法 A：IDENTITY_MISMATCH。读法 B：SCHEMA_INVALID。
   - 采用：两者都接受。journal 与 summary 的 product_run_id 不一致属于 I:86 明列的「产品Run」，所以那条测试固定断言 IDENTITY_MISMATCH。

8. **基线 Markdown 表的列序和单元格写法**
   - 原文 I:95「每scenario/version/mode列 planned、attempted、not_started、passed/attempted、failed、质量率、replay_miss/attempted、miss率、失败分类；零分母率为 `null`」。
   - 读法 A：列按原文顺序排，比值写成 `1/3` 这种形式。读法 B：列序和数字格式都由实现自定。
   - 采用：前三列依次是 scenario、version、mode，其后按原文顺序排列；`passed/attempted` 和 `replay_miss/attempted` 写成 `x/y`；零分母率与 structural 质量率写成 `null`。非 replay 列的 miss 率只断言「不是数字」。比率只断言能解析成数字，不限定小数位或百分号。

9. **writer 输出的文件是否必须是 canonical 字节**
   - 原文 I:68 定义了 canonical JSON，但没有明说 writer 写出的文件就是 canonical 字节。
   - 读法 A：文件字节等于 canonical(doc)。读法 B：只要求 JSON 内容一致。
   - 采用：只比较解析后的 JSON，不断言字节格式。

10. **load_calibration 返回结果里的两个 hash 从哪来**
    - 原文 I:73「固定调用 `calibrate()` 默认 P7 参数……与保存 result 全字段比较」。
    - 读法 A：用 capture 的两个 hash 传给 `calibrate()`。读法 B：结果里不带 hash。
    - 采用：读法 A。round 用的校准结果必须带 hash，否则 PR-A `aggregate()` 会判定 JUDGE_NOT_CALIBRATED。

11. **自身不自洽的保存校准结果（例如 passed=True、agreement=0）报哪个码**
    - 原文：旧版 I:82 把「校准计数不自洽」列在 SCHEMA_INVALID 下；lead 更新后，SCHEMA_INVALID 只覆盖原始校准输入的判定次数不合法。
    - 读法 A：DERIVED_RESULT_MISMATCH。读法 B：SCHEMA_INVALID。
    - 采用：按新版，校准文件里的保存结果与重算不符一律报 DERIVED_RESULT_MISMATCH；判定次数不对报 SCHEMA_INVALID；直接传给 `aggregate()` 的 DTO 报 `AggregateError("CALIBRATION_INCONSISTENT")`。

12. **writer 发现 journal 场景版本不一致时的错误码**
    - 原文 I:91「每个 writer 先验证全部数据」，没有说 journal 不一致报哪个码。
    - 读法 A：IDENTITY_MISMATCH。读法 B：SCHEMA_INVALID。
    - 采用：只断言抛出 StorageError，并且没有任何文件被发布。

13. **旧格式里的单个 trace 对象如何转成展示项**
    - 原文 I:76「旧 trace 单对象转换成一个展示项」。
    - 读法 A：原样放入。读法 B：转换后字段有所变化。
    - 采用：只断言恰好得到一个展示项。entries 数组的情形原文写的是「原样」，测试断言内容逐项相等。

14. **缓存 aggregate 的 round_id 或 manifest_hash 被改时报哪个码**
    - 原文 I:72 写在身份段，I:88 写在派生段。
    - 读法 A：IDENTITY_MISMATCH。读法 B：DERIVED_RESULT_MISMATCH。
    - 采用：这一项不单独测。缓存行计数和 baseline_complete 被篡改的情形测的是 DERIVED_RESULT_MISMATCH。

15. **datetime 带非 UTC 偏移或不带时区**
    - 原文 I:64「datetime 是 UTC ISO8601 字符串，reader 接受 Z 或 +00:00」。
    - 读法 A：`+08:00` 和不带时区的时间都按类型错误处理，报 SCHEMA_INVALID。读法 B：自动换算成 UTC。
    - 采用：读法 A。

16. **frozenset 数组没有排序**
    - 原文 I:64「frozenset 排序、不得重复」。
    - 读法 A：reader 拒绝未排序的数组。读法 B：只要求 writer 排序。
    - 采用：只测重复项被拒绝，不测排序。
