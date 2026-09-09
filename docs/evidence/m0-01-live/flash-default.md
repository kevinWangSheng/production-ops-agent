# 2026-09-09：按用户决定默认使用 DeepSeek Flash

用户明确指定默认Flash，直接调用Flash，不依赖Pro别名转路由。官方当日[模型与价格页](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)列出的API名为deepseek-v4-flash，当前页面标注Flash-0731；V4.1发布/替换尚不能仅凭该页面确证。因此使用当前官方可调用别名，不猜造deepseek-v4.1-flash请求ID。模型效果优先级是用户本次选择，不当作项目已完成Pro/Flash对照评测。

当前默认请求与预期响应均改为deepseek-v4-flash，保留thinking enabled/high。配置加载器、模板、live及本地协议/流式替身入口同步，测试中的响应与默认一致，旧Pro作为错配负例或历史响应枚举保留。SPEC和C3记录用户的新选择，不改产品实施门槛或feature_list验收。主私有.env仅设置OPSPILOT_MODEL，key不输出/复制，0600权限保持；代码与规范已随 PR #12 合并到 main `55539bc`（2026-09-09T13:25:36Z）。

原Pro真实实验、已使用v1批准文件、原账本及费用记录保留，不能改名为Flash成功；本次选择不授权重新claim或付费重跑。新实验仍需匹配当前profile/runtime/代码hash的具体批准合同，错配和旧批准均拒绝。

验证：make check 240 passed / 15显式PG默认skip；offline返回offline_pass、external_calls=0、live_verified=false。本次只做本地切换/测试，无真实模型或trace上传。独立审查见[flash-review.md](flash-review.md)。官方峰值cache-miss输入3元/M、输出9元/M，仅作为后续估算依据，原已使用的单次2元授权不延续到新实验。

补充只读核验：DeepSeek /models 返回HTTP200，包含deepseek-v4-flash，未单列deepseek-v4.1-flash；仅一次模型目录读取，无推理请求。目录可用不代替Flash真实协议验证。

当前新 Flash 实验授权及范围见 [本轮合同](flash-contract.md)；本页原切换测试、未运行与未授权描述均属于切换时的历史，不覆盖本轮新批准。
