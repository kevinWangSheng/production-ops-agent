# M0 A 独立审查（2026-09-08）

审查者为未参与实现的全新上下文 Agent。受检 HEAD `bb0ed2faaeb7a77f4952b7f28fd4e6d6b1dd43a0`，相对 `chore/m0-batch-baseline`；初始工作区干净。已读 AGENTS、完整 SPEC、ROADMAP、C3 §5/7/11/12/13、M0 计划、A 任务及共享接口合同，并检查协议/适配/trace/预算类型、测试与原始证据。未读取真实 .env、启动数据库或进行任何模型/trace/云/生产调用；仅合成替身。

当前结论：**原 1 项 P2 已于修复提交 `546148e86c526f48bcf7f5946431a458cdfa1c35` 独立复验关闭，本批 A 合成协议范围独立审查通过。** 真实兼容、持久恢复、完整产品取消和工具超时仍不由此证明。

## 原 P2：工具取消丢失完整工具组（已关闭；以下保留首次发现）

位置：`scripts/m0/adapters.py:187–206`。工具循环只捕获 `Exception`，`asyncio.CancelledError` 属于 `BaseException`，从 `tool(...)` 直接穿出，跳过 `history.append`。完整模型响应已收齐且费用已结算，但工具计划及其取消结果没有留在尝试内历史中；多工具计划还会丢失已执行工具的结果。C3 §7 要求工具保存成功、失败、取消或未知记录，重建不得留悬空配对；本任务自身也承诺错误/失败完整配对。

最小反例（在任务 worktree 执行，无网络）：

```python
import asyncio, runpy

m = runpy.run_path("tests/test_m0_adapters.py")
a, r, h, f, events, _ = m["setup"]()


def cancelled(*args):
    raise asyncio.CancelledError()


try:
    m["execute"](a, r, h, f, tool=cancelled)
except asyncio.CancelledError:
    print(len(h.messages(r)), [e[0] for e in events])
```

实际输出：`0 ['reserve', 'send', 'settle']`。

最小修复建议：保留已完成工具结果；取消时给当前及尚未执行工具生成固定取消/未执行结果，写入完整配对组，再向调用者保留取消信号。不要执行剩余工具或输出原始异常。新增单工具取消、多工具中途取消及后续消息配对断言；不需要扩展为持久恢复系统。

## 独立检查与边界

运行 `.venv/bin/python -m pytest tests/test_m0_adapters.py tests/test_m0_trace.py tests/test_m0_contracts.py tests/test_m0.py -q`：**67 passed in 0.91s**。覆盖既有完整流/分片/断流/缺 DONE/非法 finish、参数拒绝、正常失败续接、消息配对、Run 隔离、私有字段过滤、reserve-before-send、replay 拒绝、deadline 复核、未知占用、429 零隐式重试、trace 白名单及上传/回读故障。证据 manifest 的全部列出文件 SHA256 与当前文件匹配；原始离线及 live-refusal 工件没有冒充真实调用。

额外探索保留为边界，不新增阻塞发现：

- 同步注入工具 `sleep(0.05)`，调用 `timeout=0.01`，仍返回 `('completed',)`，约 0.056 秒。当前 timeout 只约束模型流并在工具前检查 deadline，不是工具执行超时；本批固定合成工具尚不证明生产工具超时或协作取消。后续实现须单独验证。
- 将单个 SSE chunk 的响应 id 改为不同 id，仍执行工具；目前收集器不校验所有 chunk 响应身份一致。这是合成协议覆盖缺口，尚无真实 SDK/provider 故障证据，不升级为当前阻塞。
- `[DONE]` 后同一字节块附加 SSE error，SDK 按已结束流处理并返回成功；该 error 位于结束标记之后，未据此认定违反完整流合同。

未改实现或验收清单，未写 passes，未打开 M0/产品门槛。协调者更新任务和 PR 状态；以上边界仍保留。


## 修复后的独立复验（2026-09-08）

受检修复 HEAD `546148e86c526f48bcf7f5946431a458cdfa1c35`，工作区仅本独立报告未跟踪。检查修复 diff：工具取消时保留先前结果，为当前及未执行工具补齐固定 `TOOL_CANCELLED` 结果，append 完整组后重新抛出不含原异常内容的 `CancelledError`；没有执行余下工具，也未改变模型预算结算。

独立执行：

- `.venv/bin/python -m pytest tests/test_m0_adapters.py tests/test_m0_trace.py tests/test_m0_contracts.py tests/test_m0.py -q`：**69 passed in 0.88s**，包括新增单工具取消和三工具中途取消回归。
- 重跑原单工具反例并加入确定性断言：取消信号仍传出且参数为空；历史为 assistant + tool 两条完整配对消息，结果为 `TOOL_CANCELLED`；输出 `original_repro_fixed 2 ['reserve', 'send', 'settle']`。
- 独立自拟四工具计划，在第三个工具取消：前两个结果保留原 evidence，第三和第四个均有取消记录，第四个工具未调用；四个 call ID 与四条结果一一配对。再次调用 round，直接核对第二个 SDK 请求中携带完整取消组、无私有异常文本、每轮 reserve/send/settle 次序保持。输出 `independent_multitool_and_continuation_PASS`。

P2 已关闭。本复验为尝试内合成协议证据，不验证 OS/进程取消、同步工具执行超时、持久恢复或真实模型/平台兼容。未读取 .env、没有真实外部调用、未改实现。
