# M0 A 离线协议与 trace 证据

日期 2026-09-08；[任务与执行前合同](../../tasks/2026-09-08-m0-a-adapters.md)。测试者为实现者，不是独立审查者。

- [check.txt](check.txt)：最终 make check，76 passed；29 项新增协议/trace测试，其余47项基线回归。
- [manifest.json](manifest.json)：dirty受检脚本、测试、共享合同、fixture、依赖的SHA256及基线；未把基线HEAD当成全部受检代码。
- [legacy-offline.json](legacy-offline.json)：原SDK非流式两轮及LangSmith序列化离线排演通过。
- [live-refusal.json](live-refusal.json)：真实入口退出3，LIVE_NOT_ENABLED。

版本：Python3.12.13、OpenAI3.10.0、HTTPX2 2.12.0、LangSmith0.12.2，完整依赖以锁与manifest为准。没有读取.env、真实模型调用、真实trace上传/回读、云或生产操作；无付费额度。make setup下载依赖属于开发环境准备。

## 可复现与汇合接口

在任务worktree运行 `make setup`、`make check`，定向测试为 `.venv/bin/python -m pytest tests/test_m0_adapters.py tests/test_m0_trace.py`。

`SyntheticAdapter(transport=httpx2.MockTransport(handler), budget=budget, clock=clock)`；`await adapter.round(run, History(run), fixture, upper_bound=100, synthetic_price=lambda usage: usage["total_tokens"])`。fixture沿用 `tests/fixtures/m0/protocol-v1.json`。合成换算将token整数直接作为微元，仅为注入测试，与真实价格无关。Budget来自共享Protocol，可在协调者汇合测试中换成B账本；A替身只记录调用，不做余额算法。每round是一次物理请求；调用方再次调用即新request_id，并仍使用原RunContext期限。

`History(run)`是尝试内受限私有状态；messages只接受原RunContext，keep_groups按完整组裁剪，不改变来源；不是报告、知识或judge输入。旧protocol.continuation保留原默认身份行为，新增expected_run_id只供经owner校验的封装复用。

`TraceAdapter(backend).export(run, raw)`，backend提供upload(trace_id, payload)/read(trace_id)，返回TraceResult固定status/code；输入raw只提取白名单，回读严格比对字段、类型、experiment与Run。沿用旧排演request_count 0–2和固定合成subject/evidence元数据，供本次两轮排演；真实subject/attempt映射尚待产品合同。后端失败保留调用方原结果，可显式重试；没有后台重试、持久outbox或真实平台恢复证明。

## 覆盖及边界

正常SDK流、分片参数、分片DONE、工具往返；缺finish、缺DONE、断流、截断finish、超时、429无隐式重试；参数拒绝、工具异常配对续接、重复ID整计划拒绝；压缩完整组/证据来源、不允许悬空结果；Run隔离、未知私有字段过滤；reserve/send/settle顺序、重复预留拒绝发送、费用未知保留、期限等待跨界零发送；trace上传/回读故障、污染/错误身份/错误类型、恢复重试；工具与trace网络调用被阻断。

这些是合成SDK/合同测试，未执行DeepSeek真实兼容、LangSmith真实回读、持久恢复、真实目标权限、生产隔离、模型质量或72小时soak。安全边界只覆盖这个本地Python排演，不是可对抗不合作代码的OS隔离。工具为固定合成read_fixture，只读运行时和完整Controller提交门槛不在本实现内。实现后的独立审查、A+B汇合以及PR/CI仍待协调者执行，M0和产品门槛保持关闭。
