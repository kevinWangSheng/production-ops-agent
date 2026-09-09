# 正常开发基线 normal-01：执行前冻结

2026-09-09。固定窗口 `1788975149.661..1788975449.661`，目标 m0-otel-20260909 / OTel Demo 2.0.2，问题为调查checkout/payment链路是否异常，不向模型提供正常结论或故障答案。

环境执行者确认真实Prometheus metrics、checkout traces(该窗9条)和部分服务日志可查询。已知日志集成缺口：cart/frontend-proxy有数据，checkout/payment/frontend无OTLP log记录；缺口单独披露，不能以缺日志当无错误。正常性由独立工程观测判定，不由调查者自认。

可诊断性：通过已采集指标和traces能够检查这个有界窗口的请求/错误及调用路径；日志不完备限制原因排除。仅为开发校准，非完整正常遥测验收；默认工具与宿主网络隔离差异见合同。

本案上限4实际模型HTTP / 20工具查询 / 每请求预留1CNY，子allocation不变。输入文件hash及运行代码hash随结果登记，模型实际配置/提示schema由执行器先落盘。未来质量判断以实际query/observation及独立环境观测对照，不因名为normal就强迫正常输出。
