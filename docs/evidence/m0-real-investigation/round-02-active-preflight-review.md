# M0-02 主动调查执行前独立核验

日期2026-09-10。**当前冻结active harness本地检查通过；正常工程观察有流量，可在父记录flagd同integration授权scope修正后执行已授权正常Run。** 不是正常报告质量、真实Holmes→v3或产品入口通过。

31项定向tests PASS/1.49s；真实固定Holmes preflight m002-active-preflight-independent01返回import_configuration_pass（0模型/工具查询）。静态重读scope/actual_sources、raw/view保存、partial传输与受限响应捕获；scope注入固定window，metrics明确integration级，混合trace按实际来源集合检查，越权返回只交safe拒绝view；原业务raw保留。报告引用/定量质量另评审。

工程正常窗1789007908..1789008208：7份原始文件全部存在且SHA256匹配summary；ERROR增量全0，payment transactions增8.7482139，checkout Charge status0增9.9986668；checkout/payment查询各9 traces，非空窗。样本和浮点increase不是总体SLO或精确订单数。只检查既存工件，无环境操作/新查询/模型。

发现旧17服务scope未含flagd，而真实mixedtrace/metrics包含flagd。registry确有/opspilot-m0-flagd、hostname990187bcd9c2。父可依据同专属integration既有授权将flagd加入混合遥测scope并留hash；不代表开放flag配置、工程注入器或旧backend直查flagd（后者仍可能denied）。其他scope新增须有同样来源依据。

代码hash：
- `holmes_baseline.py` `6f01c71303bd0ddce79eed91e99ae5a3186cd8386a86d4577b235ae8de36bb8b`
- `round02.py` `33ef91543e1266bf7428c9585d86d4a5f067f8d8f4563f304fa4fb46b6386e12`
- `transport_worker.py` `559b32fd55eeb5fa03513e7739e2c331dcde3b67d4264497274324f4649b7582`
- `run_bounded.py` `c46493aa1c44a4e5a0f627a62525d1ce82215dbc73dc76405fa4bd40e0550d24`
