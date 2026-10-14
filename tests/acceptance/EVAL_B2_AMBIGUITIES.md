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
