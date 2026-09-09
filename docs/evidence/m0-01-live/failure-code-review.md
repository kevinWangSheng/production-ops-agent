# M0-01 错误分类独立复核

2026-09-09；独立 Agent，未参与实现。针对 `c8a8ced` 后 `scripts/m0/live.py` 与 `tests/test_m0_live.py` 的窄修复，核查远程 comment `3968000660` 所指出的业务错误被统一误标为协议失败的问题。沿用 SPEC、C3 §5/12 与既有批准边界，不扩大实验范围。

## 结论

未发现本次修复的阻塞缺陷。已知账号、HTTP 认证/限流、超时、取消、传输、JSON、协议、存储/预算与控制错误获得固定分类；未知错误固定返回 `LIVE_OPERATION_FAILED`。业务与 trace 异常采用同一安全映射，保留模型 profile 不匹配分类。仅白名单字符串可输出，异常正文不会作为返回码或调试消息泄漏。

失败后保持既有停止规则：业务失败不触发 trace 上传；trace 请求失败不继续回读或重试。分类只改变受控报告，不增加出站权限或重新执行权限。外层验证/claim 等进入业务段之前的失败仍由 CLI 原有受控退出处理；本修复不宣称每个故障阶段都持久化详细错误码。

## 实际验证

- `.venv/bin/python -m pytest tests/test_m0_live.py -q`：**49 passed in 1.29s**。
- 独立合成 MockTransport 探针：项目 GET 返回 403/429 时分别 AUTH/RATE，只有一个请求；trace POST 返回 401 时 trace AUTH，模型业务已完成且总计四个请求，没有后续 trace GET。
- 探针注入 `PRIVATE_SENTINEL` 响应正文，返回结构不包含该文本；未知 ConfigError 固定 OPERATION_FAILED，ConnectError 固定 TRANSPORT_FAILED，JSONDecodeError 固定 RESPONSE_INVALID。
- 原定向回归覆盖账号错配、取消、超时、存储、deadline、未知异常，以及模型 profile 拒绝和原授权/上传边界。

全部使用合成配置、MemoryLedger 与 `no_network()`；未读取凭据或真实批准文件、未联网、未运行 live CLI、未触碰 PostgreSQL。该结论不替代最新远程审查/CI，也不表示真实重跑、合并或产品验收。

## 已审文件 SHA-256

- `scripts/m0/live.py`：`d7c63d092432563d3381a5e0ff9b94296d4e067bd9192657a0536a0a36e0b46b`
- `tests/test_m0_live.py`：`5c70c6498d7702470da4f897f68a3cf8ebe6c7a95a9437204f3760c257cb504b`
