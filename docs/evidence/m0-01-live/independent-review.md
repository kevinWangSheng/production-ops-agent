# 首条 live 入口独立实现审查

2026-09-09；独立 Agent `/root/review_live_implementation`，未参与实现，以新上下文启动。基线 `ad99e6a`，工作区 `production-ops-agent-m0-01`，审查未提交变更。仅写本记录，不修改实现。

## 结论与范围

发现一项期限边界缺陷，已由实现者修复并由本审查者复验；当前所审入口未发现未解决阻断项。结论仅覆盖本地 one-shot normal-1 实现及合成 HTTP、专属 PostgreSQL 验证，不证明 DeepSeek / LangSmith 实际连通、价格/账单、账号默认 workspace 或产品功能通过。真实运行仍需要相应审批和账号前提。

依据：完整 AGENTS、SPEC、ROADMAP，C3 §5/11/12/13、M0 执行计划及本目录 plan.md。原矩阵流式中断、持久私有协议恢复、平台恢复导出及产品级人工控制不在本次通过范围。未读取真实 `.env` / key，未发模型请求或上传 trace，未启停 PostgreSQL。

## 发现、处置与主动反例

**R1 / P2，已修复：提交请求额度后未复核出站期限。** 原 `scripts/m0/live.py` 的 `Wire.request` 先计算 remaining，再调用同步 `ledger.attempt`，提交等待跨越期限后仍使用旧 remaining 发送。独立反例以 MemoryLedger 包装 attempt：调用原 attempt 后 `time.sleep(0.08)`；Wire deadline 设为当前时间 + 0.03 秒；以 no_network + MockTransport 捕获请求。修复前实际打印 `sent_seconds_after_deadline: [0.051471]`，模型请求在截止后约 51ms 仍发出。

实现者在 attempt 返回后重算 remaining，同时检查 `asyncio.current_task().cancelling()`。独立复验同一反例得到 `LIVE_DEADLINE_OR_CANCELLED`、`http_requests: 0`。另在 attempt 中执行当前 task.cancel()，同样得到该固定错误和零 HTTP。复验对应 live.py 255–257 行；数据库额度可以保守占用，但不得越过期限后新出站。

**响应压缩反例未构成新缺陷。** 用 AsyncByteStream 返回 gzip 8175 bytes，解压内容为 8 MiB，收到 `LIVE_RESPONSE_TOO_LARGE`；tracemalloc 峰值 2166190 bytes。进一步读锁定 httpx2 `_decoders.py`，gzip 解码按 1 MiB 块生成，因此此反例未导致无界展开。131072 bytes 是可接受响应上界，不是严格进程内存峰值。

## 独立执行证据

- `.venv/bin/python -m pytest tests/test_m0_live.py -q`：20 passed；修复后重新执行仍 20 passed。涵盖账号 tenant 错配先于模型、重定向拒绝、响应超限、取消、错误工具、平台失败、404 回读次数、回读字段污染、审批错配及重启拒绝。
- `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_live_postgres.py -q`：2 passed；使用主 Agent 已启动的专属本地端口 55431。独立执行验证四并发仅一 claim、相同审批换 IDs 仍拒绝、连接重建不重置占用、过期拒绝、业务/outbox/unknown trace 和全额 unreconciled 2.00 同时保留。
- `make check`：doctor、锁文件离线检查、Ruff 与 format 通过；210 passed / 15 skipped。15 项是未开启 PG 标记的集成项；不将 skipped 计作通过，上述本次新增 PG 两项另行真实执行。
- 修复后 `.venv/bin/ruff check scripts/m0/live.py tests/test_m0_live.py` 及相同路径 format --check：通过。

静态边界核对：审批绑定代码/fixture/lock 摘要、两 key 指纹、endpoint、project/workspace 身份及固定专属 DSN；UUID、审批引用均持久唯一；出站固定路由与认证通道，trust_env=false、无重定向/自动重试；配置拒绝自动 tracing；LangSmith SDK 仅在内存 CaptureSession 序列化后经白名单发送。业务与 outbox 在一次数据库事务中保存，只有 completed 才尝试上传，费用未知保持全额占用。默认 workspace 的实现语义是省略 tenant override 并核对已批准项目 tenant；真实账号是否确为其默认 workspace 尚须账号证据，mock 不能证明。

## 审查版本

最终复验时 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| scripts/m0/live.py | 230a3dd28af4f6fad4864ad2f101568865d7accdfd121e9d046016c3f338558f |
| scripts/m0/live.sql | c6a593a5a4c0acf6f1cf75403ee903f227bf7cf84e19a4c01aae9bd67427f5ab |
| scripts/m0/__main__.py | 77680261a48e0c29821d45364d996cb48073390d046f43be2911ca5b1399d8c8 |
| tests/test_m0_live.py | 66375d7705fdd1f831c48efc5a9d79eeaab3541e160495a7af77d9a2be4ea816 |
| tests/integration/test_m0_live_postgres.py | 32e3128428a49aab2b1cb83a692aa7f69842e65d3aec3a8d9f104b702d4de7ac |

后续实质变更需针对受影响边界复验，不能沿用此快照的通过结论。

## 同日收尾差异复验

审查后实现增加发送前模型尝试计数、trace DTO 回读值类型匹配，以及期限跨数据库提交的永久回归测试。以上版本表已更新至此轮快照。

- `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_live.py tests/integration/test_m0_live_postgres.py -q`：23 passed（21 mock + 2 专属 PostgreSQL）；Ruff check / format 针对修改源码和测试均通过。
- 独立主动反例：将 trace 回读 `outputs.attempt` 从整数 1 改为布尔 true；业务 completed，trace unknown，模型尝试 2。没有因 Python 的 `True == 1` 错判通过。
- 独立主动反例：首轮响应 reasoning_content 扩为 20000 bytes，续接体积超过 16384 bytes；结果 failed / pending、request_count 1，第二模型请求未发出。计数是客户端开始发送的尝试数，不能据此推断服务端确实收到或已计费。
- 阅读当前 plan、task、development、ROADMAP 差异：明确本轮本地准备及 PR 交付、实际付费/trace 未执行、完整 M0 与产品门槛未开，未见将合成成功提升为真实成功的声明。

主 Agent 另报告只读账号 UI / 项目 GET 证据已补齐区域、default 项目与套餐；本审查者未重复访问账号，不能将该转交证据称为独立账号验证。DeepSeek 别名路由变化被明确作为实际运行前人工版本核对门槛：当前代码绑定请求别名与代码摘要，没有实现对服务端实际模型版本的强制核验。故本轮本地准备可交付，不意味着版本前提已经解除，后续真实运行须继续遵循计划中的核对与审批。

最后窄改动复验：`token_usage` 将响应 model 映射到固定已知枚举或 `unreported_or_unrecognized`，只保存本地 usage，不进入 trace。独立测试两种已知枚举、任意秘密标记字符串、None、dict、list、bool 共七个输入均通过；任意秘密标记未保留，布尔 token 和超界 token 未保留。再次运行定向测试 21 passed。`.env.example` 仅更正独立私有审批合同说明，与入口一致。最终 live.py hash 已更新如上；响应自报的 alias 或版本仍不是后端实际版本的独立证明。
