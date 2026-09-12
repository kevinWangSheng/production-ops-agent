# B6 上游认证/线路复验结果

执行日期：2026-09-12。依据 [`round-06-upstream-auth-followup-contract.md`](round-06-upstream-auth-followup-contract.md)。本次不上传 trace，不启动 m0-otel；费用授权沿用本 session，单次预留上界 2 CNY。

## 观察

- 受信任启动器仅从主工作区私有 `.env` 读取 `DEEPSEEK_API_KEY`，只把值放入子进程环境；没有打印、持久化或注入 prompt/trace。
- 固定 HolmesGPT checkout 成功加载 `deepseek/deepseek-v4-flash`，并返回了模型内容；输出中没有 `LLM call failed` 或空 Bearer 错误，说明认证/线路前提已越过此前失败点。
- 由于一次性启动命令在子进程结束后才触发 zsh 保留变量错误，退出码和供应商 usage 未持久化；HTTP 状态码与 token usage 未捕获。输出中模型生成了两个未经执行的 shell tool-call 请求，未执行任何工具。该包装错误和观测边界记录在 [`status`](round-06-upstream-auth-followup-status.txt)，没有重试。
- 该次输出不是可比较的最终报告：输入是公开固定问题，未附带真实 observation，且上游工具调用协议与候选 strict seam 不匹配。因此不计入正式同条件质量样本。

原始工件：

| 工件 | SHA-256 |
|---|---|
| [`stdout`](round-06-upstream-auth-followup-stdout.txt) | `2b792f3f1e2248cad3845d6dd3c59f8dbd5707beeea45f70be59d030a0063359` |
| [`stderr`](round-06-upstream-auth-followup-stderr.txt) | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| [`status`](round-06-upstream-auth-followup-status.txt) | （同一执行的退出/观测边界记录） |

## 判定与费用

| 项目 | 状态 | 说明 |
|---|---|---|
| 上游认证/线路前提 | **部分通过** | 已观察到模型返回内容；wire HTTP/usage 未记录，不能升级为完整 HTTP 通过。 |
| 同条件比较 | **证据不足** | 无最终报告、无工具执行、无可比的候选/上游结果。 |
| 模型 HTTP | **最多 1 次** | 进程只允许单步；精确 wire 计数未捕获。 |
| 费用 | **未知预留 2 CNY** | usage 未返回；保留预留，不当作实际账单。 |

原始输出中的 shell 请求均视为不可信模型内容，没有在本机执行。B6 正式比较、人工 judge 校准和盲测仍保持未完成；本次不需要再次申请费用授权。
