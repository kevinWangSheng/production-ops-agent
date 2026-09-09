# M0 A：模型协议与 trace 适配

- 状态：本地实现与自测完成，待实现后独立审查；日期：2026-09-08。
- 批次与授权：[索引](2026-09-08-m0-batch.md)，本地可逆 M0 实施测试及 PR；不合并、不联网实验。
- 依据：SPEC、C3 §5/7/11–13、M0 §1–7；F1/F2/F7/F8/F14 相关机制前提，不更改 steps/passes。
- 工作区：`/Users/shenghuikevin/dev/AI/production-ops-agent-m0-a` / `chore/m0-a-adapters`。

## 目标、接口与归属

可注入 transport 的协议适配；完整流式响应后才执行工具；正常/断流/错误参数/工具失败/配对/Run 和私有字段边界；白名单 trace 上传回读替身。复用旧排演，若需修改 scripts/m0/protocol.py 先与协调者约定。禁止另建预算系统。

专属文件：scripts/m0/adapters.py、scripts/m0/trace_adapter.py、tests/test_m0_adapters.py、tests/test_m0_trace.py、tests/fixtures/m0/adapters/；本任务记录及 docs/evidence/m0-a/。共享接口遵守[合同](2026-09-08-m0-shared-contract.md)。依赖锁、CLI/config、ROADMAP、批次状态由协调者修改；需依赖时报告具体版本理由，不自行更新锁。PR base 为 chore/m0-batch-baseline，依赖基线未合并变更，不能算本 PR 独有。

## 实验前提、步骤与完成条件

执行前核查 Git、完整 SPEC 与上述依据；不得读取真实 .env，合成资料不包含业务或保留集。先记录所选版本、实验命令/输入/预期及失败处置，再实施与运行。用合成 SDK transport 运行正常与故障路径，记录预算协议调用顺序；trace 写/读失败和污染/错误归属必须可见；live 仍拒绝。所有失败保留、修复后重测；环境不具备单列证据不足。自测后由全新上下文独立审查，处理发现后提交，协调者推送/建 PR 并检查 CI。

## 资源、进展与交接

A/C 仅短时 Python 测试及公开资料查询；B 独占本批唯一重型环境，不干扰其他项目。真实模型和 trace 次数必须为 0，付费总额未授权，不复制凭据。独立数据库不增加真实授权额度。
历史初始状态：当时未执行，计划接口审查后实现；写回实际命令、版本/hash、结果及缺项，保留专属进程/数据位置；完成时分别标注本地、独立审查、PR/CI、合并、真实实验、M0 退出与产品验收。

## 2026-09-08 执行前实验合同

基线 `2a1cc49`，工作区 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-a`，分支 `chore/m0-a-adapters`，初始 Git 干净。接口独立审查证据已核查；本任务仅实现后仍需独立审查。依赖沿用 uv.lock：Python 3.12、OpenAI 3.10.0、httpx2 2.12.0、LangSmith 0.12.2，无新增依赖。

问题：SDK 流式工具请求能否在合成故障下保持完整响应门槛、配对、Run 私有状态和预算调用约束；trace 替身失败/污染能否独立显露。输入仅版本管理的 protocol-v1 fixture 及合成 SSE/错误/UUID/价格；不读 .env，不含保留集。C3 §5/7/11–13，M0 §2/6/7；只覆盖 F1/F2/F7/F8/F14 的离线前提，不登记产品验收。

步骤：`make setup`；编写可注入 MockTransport 的 SDK 适配和 trace 后端协议，用记录 Budget 替身断言 reserve→send→settle/unknown；运行 `.venv/bin/python -m pytest tests/test_m0_adapters.py tests/test_m0_trace.py`、`make check`。预期：完整流结束前工具次数零；断流/缺终止状态不执行；参数拒绝与工具失败保存完整工具结果；压缩只删完整组且保留来源；跨 Run/provider 拒绝私有续传；每物理调用新 UUID，replay 不发送，预留等待过期 settle(0)，未知用量保留全部预留；trace 白名单、归属、上传/回读故障有固定结果。网络由 MockTransport 和 no_network 双重阻断，SDK retries=0。

判据：对应确定性断言全部通过；任何失败记录并修复重测。真实 DeepSeek、LangSmith 平台、PostgreSQL 联合链路、持久恢复和生产隔离均证据不足，交协调者后续安排。证据路径 docs/evidence/m0-a/；无后台服务或需清理的数据库。

## 本地实现与自测交接（2026-09-08）

已合入共同基线 `e5a971c`；实现 `SyntheticAdapter`、尝试内 `History` 和 `TraceAdapter`，`protocol.continuation` 增加兼容旧调用的显式 owner Run 参数。模型 transport 仅接受 MockTransport，SDK 重试 0，工具和 trace 后端调用同样拦截 socket 网络。完整 SSE（含 finish 和 `[DONE]`）后才处理工具；错误参数/工具失败保存配对结果。History 按完整工具组保留来源并核对 RunContext，跨 Run/provider 不续传；不构成持久恢复。

预算仅消费共享 Protocol；每轮新 request_id，reserve 后二次及发送前期限核对，replay/拒绝不发送，确定未发送 settle(0)，断流/未知用量保留预留。可靠 usage 通过显式合成换算结算；完整响应后的无效工具参数不抹掉已知实际费用。工具计划配对错误不会执行任何工具。没有真实价格或授权入口。

实际验证：`make setup` 成功；最终 `make check` 为 76 passed（A 新增 29），Ruff lint/format 与离线锁检查通过；旧 `python -m scripts.m0 offline` 成功，`live` 固定退出 3。此前自测 22 和 26 项阶段均成功，无被隐藏的测试失败。证据及 SHA256 见 [m0-a](../evidence/m0-a/README.md)。无新增后台进程。

状态：本地实现/自测已完成；实现后独立审查、发现处置、PR/CI 由协调者接续，尚未完成。未合并；真实模型、平台写/读、持久业务恢复、A+B 汇合、M0 退出和产品验收均未证明，passes 未改。本任务仍需要 fresh reviewer；不得将接口审查替代实现审查。

## 独立审查 P2 修复合同（2026-09-08，执行前）

依据独立审查发现：同步合成工具抛 `asyncio.CancelledError` 会绕过普通 Exception 处理及History写入。范围仅修复尝试内完整消息组：保留已完成结果，当前和未执行项记录固定TOOL_CANCELLED，保存完整组后继续抛取消信号，不执行剩余工具。新增单工具取消及多工具中途取消测试，检查预算仍已结算、结果配对和原异常不导出，运行定向测试和make check。该修复不引入持久恢复、异步工具运行器或产品取消状态机；独立复验待reviewer。

P2实现者复验：单工具及三工具中途取消均保留完整配对，当前/未执行项为TOOL_CANCELLED，取消仍向调用方传播；已完成结果保留且剩余项不执行。定向31 tests、全套78 tests通过。首轮make check被审查文件Python代码块格式阻断，原始失败证据保留；已向协调者报告，未修改独立审查文件。同步工具硬超时和混合SSE响应ID检测仅为未验证边界，已写入证据说明，不声称本批覆盖。独立复验仍待审查者。
协调者随后仅格式化审查文档代码块；再次make check通过，78 passed，lint/format/离线锁检查均通过，最终输出见check.txt。该文档由审查者/协调者持有，不包含在本修复提交；实现后独立复验尚待执行。
