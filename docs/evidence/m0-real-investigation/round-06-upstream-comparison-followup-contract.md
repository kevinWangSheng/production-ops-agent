# B6 M004 上游复验合同

状态：**session 内已授权；每场景最多 3 HTTP，合计最多 6 HTTP/6 CNY；不上传 trace。**

固定 HolmesGPT checkout `5e983c17f30e93099c7d775167266d4cd1d586c4`、`deepseek/deepseek-v4-flash`、thinking/high。使用已提交的 M004 normal/fault 问题和 business-summary 作为输入视图，不启动新的故障注入；不启动 m0-otel，除非运行前提强制需要。

每个场景只运行一次；本轮为受控基线复验，禁用交互式 shell 执行，最多单步模型请求以防在无 OTel 环境下调用越界工具。凭据只由受信任启动器读取，不进入 prompt、报告或 trace。失败、放弃、toolset 不匹配进入分母，不重跑补分。

判据：记录模型 HTTP/usage、最终可观察报告或失败、输入来源与差异因素；不能把单步无工具结果称为同条件质量比较，也不计算候选优于上游的结论。费用按新 ledger 记账，实际账单仍 unknown。
