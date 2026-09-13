# M0-03 相邻窗口 baseline 对照

以同一 OTel integration、同一固定查询合同、相邻的 normal/fault 观察窗口做确定性对照。原始 observation 与 raw/view/hash 不改写；本文件只新增比较工件。

- normal：`m003e-normal-observation.json`
- fault：`m003e-fault-observation.json`
- 比较：`m003e-baseline-comparison.json`

比较只使用已保存的 calls 与 checkout-rpc 原始返回，保留原始 metric labels/value。故障窗口出现 checkout/payment ERROR 与 Charge/PlaceOrder 非零状态，正常窗口对应错误率/状态为零或明确 unknown；这支持“窗口间遥测差异”，不能换算唯一请求数、完整失败率或产品 SLO。

限制：trace/log 仍是 bounded sample；checkout/payment 日志源缺失；缺失 series 保持 unknown；没有产品 SLO，不能认证健康、恢复或总体 blast radius。
