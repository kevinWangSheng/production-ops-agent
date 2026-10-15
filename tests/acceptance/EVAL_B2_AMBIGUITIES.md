# PR-B2 验收测试歧义记录

依据 `docs/tasks/2026-10-14-eval-pr-b2-interface.md`（下称 I）与 PR-B 预审第 4 节（下称 C）。每条：原文、两种读法、测试采用的读法。

1. **不完整包由哪一步拒绝**
   - 原文：I:41「完整性失败仍落 recording.json（complete=false）……便于检查中断」；I:80「RECORDING_INCOMPLETE … 不得当完整包使用」。
   - 读法 A：`load_recording` 直接抛 RECORDING_INCOMPLETE。读法 B：load 可以返回 `complete=False` 的 Recording 供检查，`replay` 再拒绝。
   - 采用：两者都接受（`verify_code` 先 load；如果返回不完整包，就要求结构回放抛 RECORDING_INCOMPLETE）。不完整包被当作完整包使用，判失败。

2. **篡改由 load 还是结构回放发现**
   - 原文：I:69「reader … 逐件核 bytes/SHA」；I:75「结构模式重新 load 源包，核字节…」。
   - 读法 A：blob 与独立 hash 都在 load 时核对。读法 B：load 只核对 recording.json，其余留给结构回放。
   - 采用：只断言 load 加 structural 组合后的固定错误码。

3. **record() 遇到完整性失败时的错误码**
   - 原文：I:41「随后抛固定码」；I:80 把「超大小上限」「driver 中断」归入 RECORDING_INCOMPLETE；I:56 规定 control 抛 SCHEMA_INVALID。
   - 读法 A：record 把 hook 原有的错误码透传出来。读法 B：record 统一改报 RECORDING_INCOMPLETE。
   - 采用：超限与 RESPONSE_TOO_LARGE 断言 RECORDING_INCOMPLETE；control 与人控终态断言 SCHEMA_INVALID（I:56、I:83、I:94）。时钟倒退、finish 之后再加事件、未绑定目标、非 synthetic 包放在 tmp/eval 之外：接口没给码，只断言抛 RecordingError，且包未标为完整。

4. **工具键的 canonical 输入**
   - 原文：I:71「SHA256(canonical JSON({fixture_hash, binding, tool_name,target_ref,params,window}))」。
   - 读法 A：binding 按 ToolBinding DTO 的全部字段（dataclass 字段名）展开，canonical 采用 B1 算法。读法 B：binding 用其他投影，例如只取 tool/version，或者先对 binding 求 hash。
   - 采用：读法 A。测试用 `asdict(ToolBinding)` 和 B1 canonical 独立复算 key，并断言 request_ref 中的 `binding` 等于该 dict。

5. **context.versions 与 manifest 维度的比较形式**
   - 原文：I:71「manifest tools_schema/registry/profile/projection/permissions 与 context.versions 对应值一致（必须已冻结为 hash/version，unknown/none 不兼容）」。
   - 读法 A：只比较 `DimensionValue.value` 与 versions[name]，kind 必须是 hash 或 version。读法 B：versions 中保存带 kind 前缀的字符串。
   - 采用：读法 A（versions[name]="v1"，对应 DimensionValue("version","v1")）。

6. **不兼容判定的时机**
   - 原文：I:79「当前绑定/冻结版本不兼容」→ REPLAY_MISS。
   - 读法 A：运行 driver 之前判定。读法 B：第一次工具查找时判定。
   - 采用：只断言 attempt 为 aborted、replay_miss=True，并且没有返回任何工具结果；不断言模型调用次数。

7. **miss 之后 driver 吞掉异常继续调用模型**
   - 原文：I:71「缺键/耗尽/绑定不兼容停止该 Run」；用户裁决 B1 =「缺键即 REPLAY_MISS，并停止调用」。
   - 读法 A：只是把错误抛给 driver，由 driver 自行停止。读法 B：代理在 miss 之后拒绝任何后续模型调用。
   - 采用：读法 B。driver 捕获异常后再调用 `session.model.complete`，investigator 仍不得收到这次调用。

8. **代理拒绝私有字段、未知 raw、旧 reasoning_content 的表现**
   - 原文：I:58、I:92「不得由 driver 复制进新 Run」「未知私有协议字段拒绝」。
   - 读法 A：replay 抛出固定码（例如 SCHEMA_INVALID）。读法 B：返回 capture，其中 error=DRIVER_ERROR。
   - 采用：两者都接受；必须满足的是被拒字段从未到达 investigator，新包里也没有源 reasoning_content。

9. **replay_source.run_id 指哪个 Run**
   - 原文：I:90「回放新包 initial_state.replay_source 固定 {run_id,recording_hash,id_map}」。
   - 读法 A：源 Run 的 ID。读法 B：本次回放 Run 的 ID。
   - 采用：读法 A（字段名为 replay_source，并与 recording_hash 指向同一个源）。

10. **capture_http 的注入方式与调用参数**
    - 原文：I:54「注入 `DeepSeekClient.capture_http`，只接 body/response/status」。
    - 读法 A：可赋值的实例属性，默认 None。读法 B：构造参数。
    - 采用：读法 A（`client.capture_http = fn`；默认值用 `getattr(..., None)` 检查）。回调参数按位置或关键字都接受。传输层失败（URLError）时断言 response=None、status=None（I:65 的 `unknown`）。

11. **传给 replay 的 attempt 的初始状态**
    - 原文：I:44「attempt 是执行后副本」，没有规定输入状态。
    - 采用：传入 started=True、status="ran"、replay_miss=False 的 AttemptRecord；replay 模式下 product_run_id 为非源 Run 的 ID。

12. **未配对交付的错误码**
    - 原文：I:67「必须与后续模型请求工具消息相符」；I:80 把「未配对」列为 INCOMPLETE；I:82 把「工具调用与交付身份不符」列为 IDENTITY_MISMATCH。
    - 采用：交付内容与后续请求的工具消息不一致，判 IDENTITY_MISMATCH（record 时或结构回放时报出都接受）。

13. **结构模式的「不读取凭据」如何观察**
    - 原文：I:44「不读取凭据」。
    - 采用：替换 `os.environ`，断言调用期间没有读取名称含 KEY/TOKEN/SECRET/DEEPSEEK/PASSWORD 的环境变量。实现如果通过其他途径读取凭据文件，这项测试覆盖不到。

## 独立审查 3 项 P2 补测（2026-10-14，语义由 lead 确定，接口文档待同步）

14. **跨事件身份/窗口不符在哪一步报出（Review P2-1）**
    - 原文：lead 裁决「结构核验必须报 IDENTITY_MISMATCH，模型回放不得使用这个包」；I:41 允许 record 落 complete=false 后抛固定码。
    - 读法 A：record 照常完成，结构核验报 IDENTITY_MISMATCH。读法 B：record 当场抛 IDENTITY_MISMATCH 并留下不完整包。
    - 采用：两者都接受。读法 A 时模型回放须抛 IDENTITY_MISMATCH；读法 B 时须抛 RECORDING_INCOMPLETE。两种情况下 investigator 都不得被调用。
    - 「没给窗口时等于由授权框推出的默认窗」：测试用 binding.default_window（12:05–12:10）作为默认窗。为了不依赖「默认窗是 default_window 还是授权框本身」这一点，反例取 12:01–12:03，它和两者都不相等。另有一条对照：显式窗口和结果窗口相等时核验通过，用来证明判定标准是相等，而不是「落在授权框内」。

15. **重复调用拒绝的录制与回放（Review P2-2）**
    - 用真实 `InvestigationLoop`、`tests/m1_investigation_support.assemble` 和 `metrics.range_query` 复现审查场景，没有用合成 investigate。绑定按真实 ToolOutcome 冻结（tool/target/source=prometheus，授权窗和默认窗都取 scope 窗口），source_run_id 取 loop 的 run_id。
    - 「原样重现」的读法：回放中的拒绝 delivery 的完整视图和实际消息内容，去掉 operation_id 后与源包逐字段相等；tool_call_id 也相同。operation_id 由新 Run 的 step 派生，I:73 只允许新 operation ID 不同，所以不比较它。
    - 「拒绝有自己的 operation_id」通过拒绝视图里的 operation_id 观察：它非空，并且与第一次真实调用的 operation_id 不同。「不调用 facade」通过两点观察：源包和回放中 facade 都只被调用 1 次，tool 事件也只有 1 个。事件结构按 I:111–114 定稿断言：拒绝 delivery 的 parent 指向 kind='refusal' 事件，该事件的 parent 指向第一次 tool 事件，hashes 恰为 {view_sha256}，request_ref 恰为 {operation_id, tool_call_id, duplicate_of_evidence_id}（均非空，operation_id 与第一次调用不同、等于拒绝视图的 operation_id）。回放产生的新包按同一条规则，只断言拒绝 delivery 的 parent 是 refusal 事件。
    - 回放 driver 自己建立隔离的 Run 状态（I:58），不把 session.tools 当完整执行器用：I:50 的属性转发只适用于录制代理，session.tools 只提供冻结的工具结果。request、scope、store 和 evidence_context 的 run_id 统一使用 attempt 的 product_run_id（replay-run-1，I:44、I:102）。loop 和 store 使用 session.clock。executor 是一个 adapter，提供冻结的 scope，并把 execute 转发给 session.tools.execute；「facade 只调用 1 次」的计数在这个 adapter 上。scope 的其余授权配置取自固定夹具 assemble()，不从冻结上下文恢复。这一点只适用于本合成测试，真实 driver 应从冻结上下文恢复这些配置。

16. **错误正文截断（Review P2-3）**
    - 原因码名称尚未确定，测试只断言 complete=false、incomplete_reasons 非空，以及 load 加结构核验得到 RECORDING_INCOMPLETE。record 当场抛码（任意固定码）或正常返回都接受。测试没有引用其他测试模块里的私有 opener，而是在本文件内重新实现了一个等价 opener：HTTPError 503，读取正文时抛 IncompleteRead(b'partial')。

## 复验 3 项 P2 补测（2026-10-14，语义由 lead 确定，接口文档待同步）

17. **混合拒绝、逐次 refusal、未执行拒绝的窗口（Recheck P2-1 至 P2-3）**
    - P2-1 用真实 loop 复现。「B 的 tool 事件和 B 的调用对得上」分三层观察：
      - 第二个 tool 事件的 request.params 为 `{"expr":"down"}`；
      - tool_call_id 为 b 的 delivery 的 parent 就是这个 tool 事件；
      - b-duplicate 的 refusal 的 parent 也指向它，duplicate_of_evidence_id 等于 B 的 evidence_id。
    - P2-2 的篡改照复验脚本的写法：删掉第二个 refusal，把它的 delivery.parent 改为 null，视图和 operation_id 改为复用第一个拒绝的，消息和后续模型请求的 body、messages hash 都改成自洽，seq 和 parent 重新编号。
      - delivery_sha256 按「实际消息的 canonical JSON 的 SHA256」重算。这个算法接口没有逐字写明，是从复验脚本推得的。如果实现采用其他算法，这条篡改会先触发 HASH_MISMATCH，那时应改的是测试的篡改方式，不是判定。
    - P2-3 把上一轮 P2-1 的窗口规则收窄为只适用于「已执行查询、有 operation.window」的结果。operation.window=None 且没有源查询的产品拒绝（INVALID_PARAMS、WINDOW_TOO_LARGE）必须能完整录制。P2-1 的反例保持不变。
      - 驱动是一次最小的「模型、工具、交付、模型」序列，没有接入真实 loop。工具执行器、注册信息和请求用 `tests/m1_tool_support` 的公开夹具；binding.default_window 为 None。

18. **已执行、没有证据、却带数据的结果（Recheck2 P2）**
    - 依据 lead 裁决：只要 dispatched=True 就核窗口；dispatched=True、evidence=None，但 view 带 rows/evidence_id 时，判为自相矛盾，报 IDENTITY_MISMATCH。
    - 测试把 binding 的授权窗和请求窗都设成 23:00–00:00，这是为了照搬复验反例的时间。事件业务钟仍然是夹具的 12:00–12:10，这两组时间互不相关。
    - 结果的 status 为 ok，source_contact 为 confirmed。(a) 结果窗口是 23:01–00:00，(b) 结果窗口等于请求窗。
    - 和第 14 条一样，record 当场报 IDENTITY_MISMATCH 也接受；这种情况下，模型回放应抛 RECORDING_INCOMPLETE。

19. **RESULT_TOO_LARGE 的 message（Recheck3 P2）**
    - 依据 lead 裁决：message 必须与产品固定模板逐字相等，模板由 view 中的限制字段重建（`_refusal_detail` 用 max_result_bytes，`_view_refusal_detail` 用 view_tokens/max_view_tokens），不相等时报 IDENTITY_MISMATCH。
    - 反例 (a) 只让 message 偏离模板：max_result_bytes=1024，窗口一致（请求未给窗口，结果等于 binding 默认窗），dispatched=True，evidence=None，content=null。和第 14 条一样，record 当场报 IDENTITY_MISMATCH 也接受。
    - 正向对照 (b) 用真实 ReadOnlyToolExecutor 覆盖两种模板：一种是字节上限（返回体为 5000 字节），另一种是视图 token 上限（max_view_tokens=20，token_counter=len）。测试不断言模板全文，只断言前缀能区分两种模板，以免把产品文案抄进测试；逐字相等的比较由实现完成，测试只要求包完整、能通过核验。

20. **无证据结果的 query 来源（Recheck4 P2）**
    - 依据 lead 裁决：query 必须等于 canonical(请求参数经产品校验后的值)，即 executor.py:692–705 的构造方式；只比较 operation.query 和 view.query 两者彼此一致是不够的。
    - 反例 (a) 用真实 ReadOnlyToolExecutor 生成结果：max_view_tokens=50，token_counter 恒为 100，所以 view_tokens=100，message 是真实模板。然后只把 operation.query 和 view.query 同时改成 rows 载荷，其余字段保持真实产品值。
    - 模型回放用夹具里的普通 replay driver。源包在回放前的核验阶段就会被拒绝，调用 driver 之前已经停止，所以 driver 的请求内容无关紧要。
    - 正向对照 (b) 复用第 19 条的 view-tokens 用例，没有重复写。

21. **受约束的运行审计事实（Ruling 2026-10-14）**
    - 依据用户 2026-10-14 的裁决。每个反例都以真实 ReadOnlyToolExecutor 的合法、无证据结果为底，各做一处篡改。底有两种：一是视图 token 超限（max_view_tokens=50，view_tokens=100），二是源错误（source_status=500，结果为 error/SOURCE_ERROR）。
    - 「一处」按逻辑改动计：
      - (a) 第二例同时改 view_tokens 和 message，让二者一致，只违反「大于上限」；
      - (d) 同时改 max_view_tokens 和 message，让二者自洽，只违反注册上限。
    - (c) 用例的篡改位置：
      - ToolOutcome 构造时会校验 status/reason 是否在词表内，所以「不在词表」只改 view 字段；
      - 「与 dispatched 的组合非法」用 dispatched=True 配 source_contact=none 表达，outcome 和 view 一起改。
    - 断言范围：
      - 不读取注册快照的内部结构，只断言错误码；
      - record 当场报码也接受，但必须是 IDENTITY_MISMATCH，且包不完整。
    - `_too_large_message` 照抄了产品模板。它只用来构造自洽的篡改，以及在正向用例里核对真实结果与模板逐字相等。如果产品文案改了，正向用例会提示需要同步。

22. **反例迁移到真实执行器加快照（2026-10-14）**
    - Review P2-1、Recheck2、Recheck3 (a) 的反例不再用假工具夹具。现在的底是真实 ReadOnlyToolExecutor 的结果，加上 `bind_tool_snapshots` 冻结的合规 ToolSnapshot，每个用例只做一处篡改。
      - 改 operation 时，同步替换 EvidenceRecord.operation。
      - 改 view 时，同步替换 EvidenceRecord 的 view 和 view_sha256。这样只留下被测的那一处矛盾，不会因为 hash 不一致先被拒。
    - 反例的时间改用 m1 执行器的授权框 00:00–01:00，不再照搬第 14、18 条里 12:00/23:00 的数值，但反例的形状不变。
      - 「省略窗口」用例：facade 请求不带窗口，执行器实际按 00:01–00:03 执行。binding 默认窗为 None，所以默认窗就是授权框。
      - Recheck2：请求窗是授权框 00:00–01:00；(a) 的结果窗是 00:01–01:00；结果去掉 EvidenceRecord，view 保留 evidence_id 和非空 content，并加上 rows。
    - 新增对照测试 `test_real_snapshot_bases_verify_untampered`：同样的底和快照、不做篡改时必须能完整录制并通过核验。这证明反例被拒是因为各自的篡改，而不是因为缺快照或夹具本身不合规。
    - Review P2-1 的正向对照（窗口相等时通过）仍用假夹具。它带完整 EvidenceRecord，不属于受限分支，没有快照也能通过。

23. **快照冻结时序：driver 如何替换 hooks.context（最终复验 P2）**
    - 原文：接口第 114、119、121–130 行要求 ToolSnapshot 在首次模型/工具调用前冻结，且独立于响应。接口没有定义 `CaptureHooks.context`，也没有给替换方式。
    - 读法 A：hooks 暴露一个可赋值的 `context`。读法 B：hooks 不暴露 context，driver 无法替换。
    - 采用：两种都覆盖。driver 在第一次模型调用之后，先读 `getattr(hooks, "context", 入口 context)`，用 `bind_tool_snapshots` 绑定新快照，再尝试 `hooks.context = 新 context`；如果抛 AttributeError 或 TypeError，就当作无法替换，继续执行。不论哪种读法，都要求 record 抛 IDENTITY_MISMATCH 且包不完整；load 加结构核验也必须拒绝。
    - 「合规快照」的构造：入口快照 A（max_view_tokens=50）和迟到快照 B（max_view_tokens=40）各取自用自身 DTO 构建的真实执行器；真正执行的是 B。迟到快照因此与随后的受限结果（100/40 的 RESULT_TOO_LARGE）一致，只有冻结时序违规。
    - 对照：在 record 之前就冻结 B，录制完整、核验通过。

24. **夹具按「PR #229 机器人第 5 轮」三条规则迁移（2026-10-14）**
    - 工具配对规则：assistant tool_call 的 arguments 改成 `json.dumps(实际 ToolRequest.params)`，涉及 `s.tool_call`、`s.reply(params=...)` 和 `source_driver`；`{}` 只用于不带工具调用的回复。回放用的 `Investigator` 由 `run_replay` 调用 `bind(requests)`，按回放 driver 实际要发出的请求填充 arguments。这样「查找键不一致」类用例仍然报 REPLAY_MISS，不会先因为配对失败被拒。
    - 配对目标取冻结的 stimulus.target_id：真实执行器和真实 loop 的 context（`_snapshot_context`、`_plain_context`、`_loop_context`）把 stimulus.target_id 设成 checkout-prod，不再沿用夹具默认的 target-demo。
    - outcome 引用规则：`source_driver` 在 tail_delivery 时不再引用最后那条只生成、未送进模型的证据；其余 driver 原本只引用已交付并已送模的证据，或者不引用任何证据。
    - 带证据结果的状态与时间不变量没有改夹具：真实执行器的结果本来就合规，对照用例已能通过；假工具的 `tool_outcome` 在当前实现上也能通过。
