# HolmesGPT 复用与差异登记

基线固定 commit `5e983c17f30e93099c7d775167266d4cd1d586c4`。本轮通过上游 Python API 运行原调查循环，不是全默认 CLI/工具组合；无候选同条件比较，不宣称源码接入就是产品完成。

| 类别 | 本轮决定与证据 | 实施含义 |
|---|---|---|
| 复用 | 原 `ToolCallingLLM.call_stream` 的上下文—工具—观察—再推理循环，原 `ToolExecutor`、原共享 `generic_ask` prompt builder | 原循环可作为真实开发基线，调查效果以运行结果另评 |
| 配置 | `DefaultLLM` + LiteLLM 1.89.0 / OpenAI 2.44.0，显式openai/deepseek-v4-flash；关闭全部默认工具、skills、Sentry初始化、Braintrust/Langfuse/OTel exporter | 原默认工具权限/数据出口不适合直接照搬；连接点可复用但必须系统执行只读范围 |
| 协议适配 | 顶层THINKING被LiteLLM UnsupportedParamsError拒绝，实际0 HTTP；改模型args `extra_body.thinking`，REASONING_EFFORT=high，离线真实出站构建及续传布尔检查成功 | 已复现配置兼容问题；非服务端故障，不用drop_params静默丢弃thinking，也不修改整套prompt |
| 出口适配 | 原call()日志会展示reasoning；call_stream事件及ANSWER_END.messages/prompt也含私有字段；执行器只摘最终业务content，观察自行留档、禁日志和自动trace | 原始事件不能直接上传LangSmith。需白名单业务DTO，原模型协议只同Run续传；本轮无上游trace上传 |
| 工具适配 | 四个固定Python Tool调用隔离Demo只读API；目标/源不可由模型变更，无shell/kubectl/注入器工具 | 这是实验接入适配，未证明所有上游原生连接器；必须披露与默认工具组合的差异 |
| 预算/终止 | 原max_steps末轮禁工具但不拥有跨进程预算。本轮增加allocation级flock、原子预留账本、当前16HTTP累计/4HTTP每Run（初始12，按原整轮总额重新分配）、1CNY预留/请求、180秒HTTP/780秒Run、固定HTTP出口及响应字节限制 | 保留开发原循环，不将实验封装扩大为产品网关；持久预算与人工控制仍需批准合同的独立实施与验证 |
| 业务完成语义 | 原ANSWER_END可能finish_reason=length/content_filter；本轮只有stop且非空才记investigation_returned，质量仍待独立证据核对 | 框架事件不是业务验收权威，截断/失败保留分母 |
| 待扩展 | 业务记录恢复权威、人工控制版本、独立恢复观察、发布观察、审核知识、认证交互、产品权限及72小时soak未因基线接入获得实现 | 仍遵循SPEC/M0实施门槛，不悄悄替换架构或修改passes |

独立审查已针对计数/体积/gzip/截断语义做静态与合成复验；其边界是开发执行器，不代表模型真实成功或产品沙箱。宿主调查没有OS/网络隔离证明，不是答案盲测；模型只能调用显式固定只读工具。


本轮最终运行结论：5个开发Run保留，normal-03返回有证据且有限质量的业务报告，两个故障Run都在末次模型请求前被总包络限制拒绝而无报告。实际暴露的缺口属于实验接入/上下文体积合同与原循环的兼容；不把HTTP200或工具成功升级为调查成功。后续复用决定必须解决单工具投影与总历史预算不对齐、明确有效引用/样本和健康表述边界，并补齐产品控制/恢复/权限机制；不无限追加包装层或重试。
