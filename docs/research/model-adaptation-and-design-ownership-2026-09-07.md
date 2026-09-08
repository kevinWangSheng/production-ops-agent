# DeepSeek 优先与技术方案分工

2026-09-07。用户确认首期主要适配DeepSeek，未来可能接GLM；希望助手参考已实现设计、形成方案，由用户审核关键取舍。具体模型、endpoint、模式未指定。未运行模型或修改功能验收。

## 模型适配建议

共享调查目标、证据标准、工具语义和输出合同。把provider/模型版本/mode的协议能力与参数放在适配配置；必要的提示差异做小型可版本化补充，不预先复制全套prompt。只有官方要求或匹配预算的评测证据才引入差异。

[DeepSeek工具调用](https://api-docs.deepseek.com/guides/tool_calls/)支持function calls，strict仍有特定beta入口及JSON Schema约束。[思考模式](https://api-docs.deepseek.com/guides/thinking_mode/)说明思考模式工具续接的消息处理要求。兼容某种API格式不代表所有字段/模式一致，应验证所选SDK/adapter不丢必要字段，且tool call ID、stream拼接、错误分类和usage含义正确。

[Z.AI function calling](https://docs.z.ai/guides/capabilities/function-calling)同样有tools/tool_calls结果合同，但具体功能/模式支持须按将来选择的GLM模型与服务端点确认。这是未来可替换的依据，不是当前已经适配GLM。

优先复用已核实现：Pi的provider与context边界、Holmes/OpenSRE的prompt/context组装及现成SDK/model adapter。相关路径在 `investigation-source-comparison-2026-09-07.md` 和 `runtime-choice-pi-2026-09-07.md`。本项目先检查实际适配器，再决定是否补写协议处理，不因模型更换重写Incident流程。

每轮记录model/version、provider endpoint标识（不含凭据）、thinking/config、adapter版本、effective prompt与tool schema版本。平台里的prompt版本管理可以辅助，但不能替代协议参数和状态版本。judge模型可以独立选择，不能因为被测模型是DeepSeek就将自评分视为独立正确性依据。

## 哪些是常规工程，哪些要结合Agent语义

常规机制：事务、唯一键、队列/租约、备份、retention、鉴权、指标和部署。优先采用现有组件/模式，不为项目创造通用数据库或调度器。

必须明确的领域语义：

- 重复投递同一个事件可以去重；相似告警是否属于同一事故则是关联规则，误合并会丢问题。
- 相同工具参数不意味着结果永远可复用；同一metrics查询在不同时间执行可能是新的合法观察。执行重试与定时复查要区分。
- 保存对话不等于保存运行状态；工具已返回但尚未保存时崩溃，必须说明恢复会重查、保留什么、如何计预算。
- 压缩模型上下文不能销毁用于人工核查的原始证据；两者有不同retention。
- 人工取消/纠正后到达的旧结果不能覆盖新决定；普通last-write-wins不够。

这些来自当前SPEC及三项目源码审计中的真实语义差异，不是新增故障专用Agent。

## 方案交付方式

助手先给一份完整技术方案，每项写推荐、来源/分析、替代方案、代价和验证。常规细节由助手决定，用户审核边界、成本、数据去向和长期耦合等重要选择。没有合适参考时，明确标成自有设计，说明为何现成机制不足。

首期DeepSeek优先并不自动接受LangGraph/LangSmith或改变只读边界。现有底座/平台推荐需要以DeepSeek的真实adapter与工具循环兼容性补验证后再锁定。实现门禁保持原状。
