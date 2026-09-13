# B6 上游认证/线路复验合同

状态：**session 内已授权，执行前已记录；最多 1 次模型 HTTP，费用预留上界 2 CNY。**

## 目的

确认固定 HolmesGPT checkout 能否在受信任进程中读取既有私有 DeepSeek 配置，并通过
`deepseek/deepseek-v4-flash` 完成一次无工具、无 trace 的最小请求。该复验只解决运行时
认证/线路前提，不替代 B6 的同条件工具比较、judge 校准或保留集盲测。

## 边界与步骤

1. 仅由受信任启动器从现有私有配置读取 `DEEPSEEK_API_KEY`；不在 shell 输出、prompt、报告、trace 或提交中显示值。
2. 固定 HolmesGPT checkout `5e983c17f30e93099c7d775167266d4cd1d586c4`，模型
   `deepseek/deepseek-v4-flash`，`--max-steps 1`，`--no-interactive`，不启用 trace。
3. 使用公开固定问题 [`round-06-comparison-question.txt`](round-06-comparison-question.txt)，不执行任何工具或外部目标；本次实际 CLI 仍初始化并暴露了上游默认 toolset 定义，故不把它记为“严格无工具挂载”实验。
4. 保存进程退出码、HTTP/usage（若供应商返回）和脱敏 stdout/stderr；任何凭据或私有字段出站立即停止。

## 停止与判据

- HTTP 上界：1；费用记录上界：2 CNY；不重试、不补分。
- 成功只能标记“上游认证/线路前提通过”；失败保留原始输出并保持 B6 同条件比较为证据不足。
- 不启动 m0-otel，不上传 trace，不修改 feature passes 或 SPEC gate。

状态：**已执行；工具定义暴露但未执行，HTTP/usage 未捕获。**
