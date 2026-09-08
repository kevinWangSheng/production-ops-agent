# DeepSeek 适配复用证据与方案（2026-09-07）

状态：技术方案输入，未接受 ADR；本轮读取官方文档、固定源码与测试，未安装执行 adapter、未调用模型、未验证 PostgreSQL checkpoint。用户仅确定 DeepSeek 优先，尚未选择具体模型 ID、端点或模式；GLM 为未来方向。

## 结论

优先复用 `langchain-deepseek` 的模型、流式、工具与用量处理，但 **不能将核查版本原样视为 thinking + tools 已兼容**。静态调用链显示：接收端保留了 reasoning_content，发出端却没有将其写回 messages。优先做一个窄的 DeepSeek provider adapter 补齐消息回传，并用真实模型与 checkpoint round-trip 验证；如修补依赖内部私有方法导致维护不稳，则在 LangGraph 模型节点直接调用官方文档使用的 OpenAI-compatible client。无需为此重写调查循环。

## 固定来源

- LangChain 仓库提交 `fa942aec719abd92026dcb56cab8e50a38776611`；该提交 `langchain-deepseek` pyproject 版本 1.1.0，依赖范围不是精确 lock：core >=1.4.7,<2；openai integration >=1.1,<2。正式采用要记录实际安装 lock，不能假设不同发行版本行为相同。
- LangGraph 提交 `81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1`；复用已有只读 checkout。
- [DeepSeek thinking 官方文档](https://api-docs.deepseek.com/guides/thinking_mode/)：工具模式下历史 reasoning_content 的协议回传要求；thinking 放在 extra_body，模式参数需按模型文档确认。
- [DeepSeek tool calls 官方文档](https://api-docs.deepseek.com/guides/tool_calls/)：strict 当前为 beta 端点与受限 schema 的显式能力，不能默认启用。

## 接收、流式、保存、再次发送的完整链

以下 LangChain 链接均固定到上述提交。

1. [ChatDeepSeek.validate_environment](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/deepseek/langchain_deepseek/chat_models.py#L299-L331) 建立 OpenAI/AsyncOpenAI client，使用配置的 DeepSeek base_url、timeout、max_retries。可直接复用，不需要自己写 HTTP 客户端。
2. [_create_chat_result 与 _convert_chunk_to_generation_chunk](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/deepseek/langchain_deepseek/chat_models.py#L385-L459) 将模型结果的 reasoning_content 存入 AIMessage/AIMessageChunk.additional_kwargs；也处理 cache_read 用量。接收测试见 [test_chat_models.py](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/deepseek/tests/unit_tests/test_chat_models.py#L122-L245)。这些测试证明作者覆盖接收转换，不能推出跨工具轮回传已正确。
3. 父类 [tool chunk 转换](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/openai/langchain_openai/chat_models/base.py#L499-L521) 保留工具 index/id/name/arguments；[AIMessageChunk 合并](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/core/langchain_core/messages/ai.py#L666-L687) 合并 additional_kwargs 和工具分片。字符串拼接与按 index 合并由 [merge helpers](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/core/langchain_core/utils/_merge.py#L6-L150) 处理。仅在完整工具消息组装、JSON 校验后执行工具，不在部分分片上执行。
4. [LangGraph JsonPlusSerializer](https://github.com/langchain-ai/langgraph/blob/81bf17b23123e4ef8b9d5f49fa09a0122fc2edd1/libs/checkpoint/langgraph/checkpoint/serde/jsonplus.py#L305-L321) 的 Pydantic 分支序列化 model_dump。AIMessage.additional_kwargs 是模型字段，因此从静态实现推断它可随消息保存；这 **不是** PostgreSQL saver、新进程反序列化或版本升级实测。
5. 关键缺口：DeepSeek [_get_request_payload](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/deepseek/langchain_deepseek/chat_models.py#L350-L383) 调用父类，然后仅修正 content 与 Azure tool_choice；父类 [payload 构造](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/openai/langchain_openai/chat_models/base.py#L1939-L1969) 调用 [_convert_message_to_dict](https://github.com/langchain-ai/langchain/blob/fa942aec719abd92026dcb56cab8e50a38776611/libs/partners/openai/langchain_openai/chat_models/base.py#L404-L475)，后者没有将 additional_kwargs.reasoning_content 加入 assistant 请求。故“能接收、能存储”不等于“能再次正确发送”。官方文档要求 tools 请求中的相关历史回传，缺失可能导致 400。实际所选发行版本及端点需运行确认。

## 推荐设计与复用边界

- 共享 system prompt、工具说明、证据要求；不按事故标签路由 prompt。provider profile 只管理端点、模型 ID、模式、受支持参数和协议处理。针对模型的提示补充必须由实验理由支持，独立记录版本。
- 首选 adapter 使用 ChatDeepSeek 并补齐 outbound assistant provider 字段；补丁只影响 provider 层，写明依赖版本和升级检测。不要全局 monkeypatch，也不预先建立大型插件体系。
- 备用 adapter：AsyncOpenAI 指向 DeepSeek 官方兼容端点，显式保存/发送 assistant content、reasoning_content、tool_calls 与对应 tool_call_id。沿用 LangGraph 状态边界；自行承担 chunk 聚合和归一化时必须测试。采用哪条以隔离验证的工作量与正确性决定。
- reasoning_content 是 provider 续接字段，不是事实证据、用户复盘、知识库或质量评分对象。需私有保存以满足协议；公共事件和默认观测输出排除这类字段。checkpoint 访问与 retention 要覆盖它，不能在通用上下文压缩中随意删掉正在延续的工具对话协议。
- thinking 使用显式配置；strict beta 首期默认不启用。工具参数仍在本地用 schema 校验，失败返回结构化调用错误。不能把参数错误伪装成 Prometheus 等数据源故障。
- 调查 Run 固定 provider/model/config/prompt 版本；后续换 GLM 优先从新 Run 开始，保留事故事实与证据。不直接将 DeepSeek 原始 provider 对话跨厂商续接。

## 实施前最小验证（本轮未执行）

1. 两次工具调用、最终答案、下一用户轮，检查真正发出的请求保留全部必需 provider 字段和匹配 tool_call_id。
2. 流式 reasoning/content、多工具 interleaved fragments、空 content、畸形 JSON：聚合正确且部分消息不执行工具。
3. 完整模型输出 checkpoint 后退出进程，新进程加载，再请求模型；逐字段比较请求。中途未完成 stream 不当作完整 AIMessage 保存和执行。
4. 数据库写失败、模型 400/429、超时和取消：错误分类、重试预算、迟到结果符合 Run 语义；400 协议错误不无限重试。
5. 显式 thinking 开关，工具 strict 普通端点隔离；实际模型用量归一化，不把思考 token 误漏或重复计费。
6. 对观测出口检查 private provider 字段是否被默认 callback 泄漏；运行记录保留配置版本以可复现。

这些是适配与运行证明，不是事故场景 prompt 设计。本轮没有修改 SPEC、验收字段或实现代码。
