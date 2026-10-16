# PR-B3 验收测试歧义清单

依据：`docs/tasks/2026-10-14-eval-pr-b3-interface.md`（下称 B3；初版按 sha1 `43b61df`，r2 的确认见文末「r2 处置」；初版含 provider/phase/balance_delta、凭据槽键、`destination/label-pack` 与扣除虚假一致数的更新）、PR-B 预审第 4 节（下称 C）、合同 r8 P6/P7/P9/D16。每条写原文、可能读法和测试采用的读法；采用的都是更保守的那一种，或者只断言不随读法变化的部分。

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

## r2 处置（2026-10-14）

B3 r2 第 68 行起给出明确约定。以下逐条对照，均未放宽断言：

- **第 1 条（调查侧隔离）**：实现方单元测试 `tests/unit/test_eval_b3_impl.py::test_answer_flag_and_path_do_not_change_actual_investigation_requests` 已覆盖（r2 第 4 项）。它用真实的 InvestigationLoop 和假模型，先比较首个 `serialized_request`，再比较整条物理请求序列。本文件不重复覆盖。
- **第 20 条（input_hash）**：r2 第 1 项把算法定为 `canonical_hash(json.loads(json.dumps(judge_request(..., outcome=outcome), allow_nan=False)))`，与本测试的读法一致，夹具不变。
- **第 22 条（annotations 字节）**：r2 第 2 项确认文件内容为 `canonical_bytes(annotation)`，与槽位和所在包无关，hash 顺序固定为 1、2。探针包取 hash 的做法成立。
- **第 23 条（认证路径）**：r2 第 3 项确认接受 destination 目录，与本测试的读法一致。
- **第 27 条（篡改强度）**：r2 第 8 项规定 loader 容忍 JSON 空白和键顺序的变化，只按语义重算。因此重写后仍被拒绝，就说明拒绝的是被篡改的内容，测试强度不再受格式影响。
- **夹具修正（r2 第 9 项）**：`test_selected_record_and_artifact` 的签名漏注入 `pack` fixture，导致拿 loader 的返回值去比较 fixture 函数对象。已补上该参数，断言不变。
- **导入排序**：按实现 worktree 的 ruff 规则排序，那边 B3 模块存在，被归为 first-party。在不含 B3 模块的 worktree 里单独运行 ruff 会报 I001，合并后不会出现。

## r2 收紧（2026-10-14）

凡 r2 写明了码或状态的位置，都已改成单一断言：

- **配置（r2 第 6 项）**
  - 最终路径是符号链接或目录 → `CONFIG_UNREADABLE`
  - 权限不是 0600、文件过大 → `CONFIG_PRIVATE_REGULAR_FILE_REQUIRED`
  - 未知键、重复键 → `CONFIG_SYNTAX`
  - 缺所选供应商的凭据或 Sol 的两个模型键、environ 想补齐缺失的键 → `CONFIG_INVALID`
  - 固定模型被改名 → `JUDGE_IDENTITY_MISMATCH`
  - 固定 BASE_URL 被改 → `CONFIG_INVALID`
- **请求与观察（r2 第 7 项）**
  - 非法 envelope（多个/零个 choices、index≠0、role 不对、带 tool_calls、响应体不是 JSON）一律 `invalid`；HTTP 失败一律 `error`
  - system 消息的 content 必须等于 `config.prompt`
  - 直接调用 judge_once 时 `phase == "evaluation"`
  - 超大响应：读到第一个超限块就停，消费量恰好等于 2 MiB 加 64 KiB
  - 凭据槽名单加入点号变体 `api.key`
- **标签与校准（r2 第 6 项）**：所有错误都必须是 `JudgeError`
  - 结构类问题（场景数、调试/验证数、未知 split、重复 ID）→ `SCHEMA_INVALID`
  - source_family 跨集合 → `CALIBRATION_SET_OVERLAP`
  - 负例不足 → `CALIBRATION_TOO_FEW_NEGATIVES`
  - 覆盖标签缺失 → `CALIBRATION_COVERAGE_INCOMPLETE`
  - input hash 错 → `HASH_MISMATCH`
  - 标签本身不合法 → `CALIBRATION_LABEL_INVALID`
  - 未裁决的分歧、空或空白的裁决记录、对一致项的裁决 → `CALIBRATION_UNRESOLVED`
  - 重签名后 pack.json 带未知字段 → `SCHEMA_INVALID`
  - 已有目标 → `OUTPUT_EXISTS`；run_calibration 必须在任何发送之前报出
- **认证（r2 第 3 项）**
  - pending 或 no_baseline 记录 → `CALIBRATION_UNRESOLVED`
  - 保存的派生值被篡改 → `DERIVED_RESULT_MISMATCH`
  - pack 不符 → `HASH_MISMATCH`

r2 没有给出单一码的位置保持原样，只要求是 JudgeError 并带固定码：

1. **重复 input hash**：可读作「结构」（SCHEMA_INVALID），也可读作「hash」（HASH_MISMATCH）。
2. **标注份数错、标注者身份重复、同一标注者换模型、标注缺项**：可读作「结构」（SCHEMA_INVALID），也可读作「标注」（CALIBRATION_UNRESOLVED）。
3. **裁决绑定的文件 hash 被交换或未知**：可读作 CALIBRATION_UNRESOLVED，也可读作 HASH_MISMATCH。
4. **候选 judge 或 Sol 作标注者**：r2 只规定了在哪一步拒绝，没有规定码。
5. **标签包里的符号链接文件**：r2 第 3 项的 EVIDENCE_UNAVAILABLE 只针对校准工件路径。
6. **已选定为 GLM 的记录配上 OpenAI 或 DeepSeek 的配置**：可以读作「未选定」，也可以读作「config 不符」。维持 `CALIBRATION_UNRESOLVED`/`JUDGE_IDENTITY_MISMATCH`。
7. **改了 prompt 的配置去读认证记录**：r2 第 1 项把 profile 校验放在 hash 之外，而合法的 zai 配置 hash 都相同，构造不出一个合法且 hash 不同的配置。接受 `HASH_MISMATCH` 或 `CONFIG_INVALID`。
8. **空凭据值（`ZAI_API_KEY=`）**：r2 写的是「缺凭据 = CONFIG_INVALID」，但没说空值是否算缺。维持 `CONFIG_INVALID`/`CONFIG_SYNTAX`。

夹具修正：`test_verified_calibration_bound_to_pack_and_config` 原先拿一个本身不合法的包（11 个负例）去测绑定，loader 因包本身不合法而先拒绝（CALIBRATION_TOO_FEW_NEGATIVES），根本没检查到绑定。现改用合法的不同包（第二位标注者换用同一模型身份，r2 第 2 项允许），pack 绑定断言恢复为 `HASH_MISMATCH`。

## r3 审查反例（2026-10-14）

按 r3 第 86 行起的五项补充，测试名前缀为 `test_r3_`：

1. **大小写与分隔符变体（r3 第 1 项）**
   - 测试的槽名：clientSecret、ClientApiKey、client-secret、clientAPIKey、AccessToken、x-api-key、userPassword、sessionCookie、reasoningContent、RedactedThinking、encrypted-content、modelReasoning。
   - 每个名字放在 view 顶层和嵌套行两处，期望 invalid、零发送、error_code 在白名单内。r3 没有规定具体用哪个码，所以只断言落在白名单内。
2. **异常链（r3 第 2 项）**
   - 配置凭据行末尾带非法 UTF-8 字节时报 `CONFIG_UNREADABLE`（r2 第 6 项）；带 NUL 时只要求是某个 CONFIG 码（r2 没有写死）。
   - 所有 JudgeError（含标签与校准的错误）都要求 `__cause__` 和 `__context__` 为 None，且完整 traceback 中没有 canary。
3. **transport 错误码白名单（r3 第 3 项）**
   - 所有失败观察的 error_code 都必须属于 r3 列出的 12 个码。
   - transport 抛出带任意 code 的 JudgeError 或自定义异常时，结果固定为 error/`JUDGE_TRANSPORT_ERROR`，repr 中没有 canary 和原始 code。
   - JudgeError 的构造签名没有公开，测试用 `JudgeError(code)` 构造，再尝试设置 `.code`。
4. **Sol 身份（r3 第 4 项）**
   - 配置的两个 OpenAI 模型键不是 `gpt-6.1-sol` 时报 `JUDGE_IDENTITY_MISMATCH`（r2 第 6 项「固定模型改名」）。带日期的写法在配置里同样拒绝，只允许出现在实际响应中。
   - 响应 model 为 `gpt-6.1-sol-YYYY-MM-DD` 且日期有效时接受，并保存该名字；其余（含 02-30 这类不存在的日期、无连字符的日期、大写、preview）一律 invalid/`JUDGE_RESPONSE_IDENTITY_INVALID`。
   - 对照模型全程用错名响应时，openai 不合格、不可选，也不能为它生成认证。
5. **原始记录才是来源（r3 第 5 项）**
   - 拿另一轮成功的 calibration.json 覆盖全部为 error 的那一轮，loader 必须拒绝。
     - **码是二选一**：r3 第 5 项写 HASH_MISMATCH 或 IDENTITY_MISMATCH，r2 第 3 项对「保存的派生值不符」写 DERIVED_RESULT_MISMATCH。三者都接受；按 lead 指示，两条 forged 测试仍保持 r2 的写法。
   - 每家恰有 60 个逐次记录。删掉一个之后，loader 报 `EVIDENCE_UNAVAILABLE`，原先拿到的认证也不能再让 judge_once 通过。
     - **文件名是推断**：按「文件名以 provider 名开头、不是 calibration.json」查找逐次记录，因为 r3 写的 `provider-001.json` 没说明 provider 部分是字面量还是供应商名。
