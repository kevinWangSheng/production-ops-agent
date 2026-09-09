# M0-01 畸形响应解析：独立复核

2026-09-09；未参与实现的独立 Agent，范围仅为 `c0580b7` 后 `scripts/m0/live.py` 与 `tests/test_m0_live.py` 的解析和错误分类修复。默认 Flash 切换不在本次复核范围。

未发现本次窄修复的阻塞缺陷。模型响应的顶层对象、choices、choice、message、role、content 与 tool_calls 容器在使用前验证；工具条目继续由既有 `tool_result` 验证。结构异常和最终内容 JSON 不合合同返回固定 `LIVE_PROTOCOL_FAILED`，不泄漏响应文本。结构合格后的 model profile 校验仍先于工具执行与续接。非字典 usage 不使正常协议失败，也不产生已核账费用声明。

实际执行 `.venv/bin/python -m pytest tests/test_m0_live.py -q`：**59 passed in 1.47s**。

独立 MockTransport 完整 `execute` 探针追加验证：最终 content 为非法 JSON、最终 content 为对象、首轮工具条目为 None，均得到协议失败，无 trace POST，模型请求分别为两次、两次、一次；两轮 usage 为列表时正常完成且实际费用仍 unknown。注入 `PRIVATE_SENTINEL` 未出现在返回结构。原回归继续覆盖模型 profile 不匹配与原授权/上传边界。

验证均使用合成配置、MemoryLedger 和 `no_network()`；未读取凭据/真实批准文件、未联网、未运行 live CLI 或触碰 PostgreSQL。本地结论不表示最新远程审查或 CI 已完成。

审查文件 SHA-256：

- `scripts/m0/live.py`：`6fe67c14bae493baf0124efd60bc6a840eac1639546c0414fd446aa8e1449e8c`
- `tests/test_m0_live.py`：`7331dee4279578a6f8e6153ea41f5afc769d4073fe9116e7cd3f4fe862df2df9`
