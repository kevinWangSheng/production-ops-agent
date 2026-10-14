# PR-B3 验收测试歧义清单

依据：`docs/tasks/2026-10-14-eval-pr-b3-interface.md`（下称 B3，按 sha1 `43b61df` 版本，含 provider/phase/balance_delta、凭据槽键、`destination/label-pack` 与扣除虚假一致数的更新）、PR-B 预审第 4 节（下称 C）、合同 r8 P6/P7/P9/D16。每条写原文、可能读法和测试采用的读法；采用的都是更保守的那一种，或者只断言不随读法变化的部分。

## 未覆盖

1. **C:158 隔离第 1、2 条（调查客户端首请求、固定假回复后的全请求序列）**
   - B3 没有提供从场景驱动调查 loop 的公开入口；驱动属 PR-C，录制属 PR-B2。
   - 本文件只覆盖 judge 一侧的对应项：改 flag/variant、把场景目录换路径后，judge body 逐字节不变，且不含 flag、场景 ID、路径（`test_answer_flag_and_scenario_path_do_not_change_body`）。调查侧两条需要 lead 决定放进 B2 还是 C 的验收。
2. **judge 内部 360 s 总时限、HTTPJudgeTransport**
   - 不用真实时钟或 socket 无法验证。测试只断言传给 transport 的 `timeout_seconds == 360`，以及 transport 抛出 `TimeoutError` 时结果为 timeout。HTTPJudgeTransport 的禁重定向、分块读和单调钟都没有测。
3. **「VerifiedCalibration 只能经 load_verified_calibration 得到」**
   - 构造器没有公开，无法测试「不能伪造」。

## 错误码

4. **calibration_flow 的异常类和错误码**
   - B3 只给出了配置错误的 `JudgeError.code`；标签包、校准用到的码散在正文里（OUTPUT_EXISTS、CALIBRATION_UNRESOLVED、HASH_MISMATCH）。
   - 采用：要求异常带固定码，即 `.code`，或 ValueError 的正文本身就是大写下划线码。只在以下情况断言具体码：已有目标 → OUTPUT_EXISTS；pack hash 不符或文件被改 → HASH_MISMATCH；非选定或待裁决记录 → CALIBRATION_UNRESOLVED 或 JUDGE_IDENTITY_MISMATCH。结构类拒绝只要求有码。
5. **配置错误码的对应关系**
   - 未知键、重复键、缺键、空凭据：CONFIG_SYNTAX 或 CONFIG_INVALID 都接受。
   - 0644：固定为 CONFIG_PRIVATE_REGULAR_FILE_REQUIRED。其他非 0600 权限、符号链接、目录：该码或 CONFIG_UNREADABLE 都接受。
   - 超过 16384 字节：任一 CONFIG 码。
   - 模型名或 BASE_URL 被改：CONFIG_INVALID 或 JUDGE_IDENTITY_MISMATCH。
   - environ 中有文件缺失的键：INVALID、SYNTAX、ENV_CONFLICT 都接受，只要求拒绝且不泄露。
6. **异常链**
   - B3:30 写「异常链隐藏」。采用 B1 I:80 的全模块规则：JudgeError 的 `__cause__` 和 `__context__` 都为 None，完整 traceback 文本里不出现任何 canary。

## 配置

7. **符号链接的范围**
   - 「无符号链接」可以指最终路径，也可以指每一级父目录。只测了最终路径；临时目录先 `resolve()`，避开 macOS 的 /var 链接。
8. **BASE_URL 是否必填，单个文件放几家的键**
   - 测试文件总是包含白名单全部 9 个键，BASE_URL 都取固定值，同一个文件按 provider 分别加载三次。缺键测试只删凭据键和两个 OPENAI 模型键。
9. **environ 与文件值相同**
   - 「只检查冲突」时，同值算不算冲突没有写，未测。
10. **judge_config_hash 的字段覆盖**
    - 测试用 `object.__setattr__` 绕开构造校验，逐个改 dataclass 的每个非 api_key 字段，要求 hash 都改变；只改 api_key 时 hash 不变。不断言具体的 canonical 字节，因为 360 与 360.0 的序列化会不同。

## judge 请求与响应

11. **user 消息的形状**
    - 采用：恰好一条 user 消息，content 是 JSON 字符串，解析后等于规范化的 `judge_request(..., outcome=...)` 输出（9 个键）。system 消息只要求非空，不要求等于 `config.prompt`。
12. **凭据槽键的范围**
    - 按 B3:26 测了：api_key、API-Key、ZAI_API_KEY、authorization、password、passwd、token、access_token、refresh_token、secret、client_secret、credential(s)、cookie、bearer。
    - 每个键放在四个位置：view 顶层、view 嵌套行、report_raw 顶层、report claim 内。期望 invalid，transport 调用为 0。
13. **report_raw 解析失败后的私有键检查**
    - report 多了未知键时产品解析会失败，但 B3:26 要求 report_raw 内的 JSON 也查。采用：仍判 invalid，且 transport 调用为 0。
14. **outcome=None**
    - 采用：在构造时拒绝，或 judge_once 拒绝或判不通过，两种都接受。任何情况下 transport 调用为 0。
15. **gate 失败时是否调用 judge**
    - 「每报告恰一次调用」和「gate 失败可跳过」都成立。采用：调用 0 次或 1 次都接受，只断言 `passed=False`。其他 Run 的证据那条也接受 0 次或 1 次调用。
16. **error 与 invalid 的边界**
    - 多个 choices、0 个 choices、index≠0、role≠assistant、带 tool_calls、200 响应体不是 JSON：error 或 invalid 都接受。
    - 模型名不符（含带后缀的版本）、HTTP 4xx/5xx/302：固定为 error。
    - finish_reason≠stop、content 的 schema、定位、引文、verdict 错：固定为 invalid。
17. **usage 的形状**
    - Mapping 或带属性的对象都接受。缺失、负数、bool、字符串一律视为 unknown（None）。
18. **直接调用 judge_once 时的 phase**
    - 签名里没有 phase 参数。接受 evaluation 或 calibration；`balance_delta` 必须为 None。
19. **超大响应的读取量**
    - 要求读取到超过 2 MiB 后很快停止：实际消费不超过 2 MiB 加两个 64 KiB 块。

## 标签包与校准

20. **input_hash 的算法**
    - 采用：对 `judge_request(..., outcome=...)` 的输出先做 JSON 往返规范化，再用产品的 `canonical_hash`。如果实现改用 body 字节或别的规范化，包相关测试会整体失败；需要实现者在接口里写明算法。
21. **「恰六场景」与样本来源**
    - 使用 `evals/scenarios` 下 D1 的 6 个真实场景，配测试作者写的最小反例报告。source_family 每项唯一（`authored/<item_id>`）。
    - 每个验证项恰带 7 个覆盖标签中的一个，调试项 coverage 为空。未知标签是否拒绝没有测。
    - `test_fixture_labels_pass_pr_a_recheck` 用已合并的 PR-A `run_gate`/`evaluate` 自检：全部标签合法，24 个验证项中 12 个负例，7 个标签全部覆盖。
22. **Adjudication 绑定的文件 hash**
    - 冻结之前算不出 annotations 文件的 hash。采用：先冻结一个把同一 AnnotationSet 放在 1 号位的探针包，取其 `annotations-1.json` 的 sha256。
    - 这假定文件字节只取决于 AnnotationSet 本身，与槽位和所在包无关。
23. **校准工件的路径**
    - 采用：`load_verified_calibration(path=...)` 接收 run_calibration 的 destination 目录，calibration.json 在其中。标签冻结件在 `destination/label-pack`，可以用 `SelectionRecord.pack_hash` 加载，且与 `freeze_label_pack` 对同一个包返回的 hash 相同。
24. **无效包在哪一步被拒**
    - 要求 run_calibration 抛出带码异常，三家 transport 都未被调用，且不产生 calibration.json；另外 freeze 或 load 至少一处拒绝。
    - Sol 作为标注者只在 run_calibration 一层断言，因为只有那里拿得到 OpenAI 配置。
25. **run_calibration 的目标已存在**
    - 只断言 OUTPUT_EXISTS 且原文件不被改动，不断言在调用之前就检查。
26. **调用失败或漏诊后的计数**
    - 按 B3:51 的更新：负例项上 1 次 error/timeout/invalid/oversize/漏诊 → agreement 47、false_passes 0、该家不通过。1 次有效的误通过 → agreement 47、false_passes 1。
27. **篡改 calibration.json 的测试强度**
    - 篡改后用 `json.dumps` 重写，格式也会变，所以测试只能断言拒绝，分不清是因为内容还是格式被拒。
    - 两处篡改：把 pending 改成选中 Sol；把误通过计数洗成 0 并选中 GLM。
