# OpenSRE 调查路径与场景分类：一手证据

核查日期：2026-09-06。固定 `main` SHA：`cd7e1b9136ca5f9dd3614e586813d98ac9a2a543`。仅官方文档和固定提交源码静态检查；未安装、未执行上游代码、未证明运行效果。

## 结论

当前交互调查入口是共享的工具调用 ReAct loop。在所查入口、prompt 组装与工具构造路径中，没有按“启动失败 / 请求错误 / 延迟”选择三套调查 prompt 的路由。此结论限于所查路径，不等于全仓库不存在专用任务。

项目确实有按用户任务匹配的 skills 和专项 workflow。因此不能说“开源 Agent 都完全通用、没有场景知识”。测试症状分类不应自动升级为生产路由；通用循环可以按证据和任务需要读取专业知识。

## 官方文档事实

[官方概览](https://www.opensre.com/docs) 的 How a turn runs 描述：读告警、收集服务及变更上下文、查已连接的遥测工具、继续调查检验想法、回答。它是行为说明，不是源码中固定五/六阶段状态机的证据。README 直接链接该站点，见 [README.md:30-34](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/README.md#L30-L34)。

不再沿用旧 `docs/investigation-pipeline-architecture.md` 六阶段说明：当前树没有该文件，不能作为当前实现的架构来源。

## 静态调用链

1. [core/agent_harness/turns/orchestrator.py:80-113](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/orchestrator.py#L80-L113)：`run_turn(text, session)` 构建 `TurnSnapshot` / `TurnPlan` 并调用 `execute_actions`；没有在此按症状分流。
2. [core/agent_harness/turns/action_driver.py:866-915](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/action_driver.py#L866-L915)：从已解析 integrations 取得可用工具，构建 Agent，再调用 `run_react_agent_with_telemetry`。
3. [core/agent_harness/turns/action_driver.py:450-544](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/action_driver.py#L450-L544)：区分显式 `!shell`、显式 slash 命令和 LLM-selected 分支。普通自然语言请求使用共享 prompt envelope，附加 goal reviewer；这里的命令分流不能误称为症状分类器。
4. [core/agent/agent.py:81-96](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/agent.py#L81-L96)：`Agent.run` 转交 `run_react_loop`。
5. [core/agent/react_loop.py:287-324](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L287-L324)、[core/agent/react_loop.py:441-483](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L441-L483)：调用模型；有 tool calls 就执行并把观察结果加入消息，继续循环。

## Context 和工具从哪里来

[core/agent_harness/prompts/action/assemble.py:88-243](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/action/assemble.py#L88-L243) 组装共享 system base、运行事实、skills 索引、已连接 integrations、仓库上下文、长期记忆、近期对话、既有操作事实、重启恢复提示和任务计划。不是只有初始告警文本。

[core/agent_harness/tools/action_tools.py:52-75](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/tools/action_tools.py#L52-L75) 使用 canonical registry 与 integrations 可用性筛选 action/chat tools；[core/agent_harness/tools/tool_provider.py:96-154](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/tools/tool_provider.py#L96-L154) 将会话与工具上下文连接起来。可用工具筛选不等于预先指定故障答案。

## 按需 skills 与专用任务：必须保留的区别

[core/agent_harness/prompts/skills/loader.py:229-270](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/skills/loader.py#L229-L270)：prompt 放简短索引；匹配任务时通过 `skill_view(name)` 加载正文。[tools/interactive_shell/actions/skill_view.py:15-43](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tools/interactive_shell/actions/skill_view.py#L15-L43) 实际读取并返回 skill 正文。

明确反例是 [core/agent_harness/prompts/skills/github_ci_health/SKILL.md:1-40](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/skills/github_ci_health/SKILL.md#L1-L40)：它定义 GitHub CI 健康只读专项流程，计划任务先获取证据，再要求 Agent 忠实输出，禁止进一步扩大探索。这说明项目包含有意限定的专项任务；不能推导成“所有工作都是开放调查”，也不能以它证明需要我们那三种症状 prompt。

## 停止策略

[core/agent/react_loop.py:394-439](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L394-L439)：无工具调用的回复先经过 host 接受判断；未满足目标时可以追加 nudge 继续。

[core/agent/react_loop.py:519-550](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L519-L550)：工具终止、观察停滞有独立停止条件；[core/agent/react_loop.py:178-225](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L178-L225) 有迭代上限和取消；[core/agent/react_loop.py:556-624](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L556-L624) 在安全停止后禁用工具生成可见交接，并有 fallback。

这些是终止和任务完成机制的实现证据，不是“根因正确”或独立业务恢复验证的证明。

## 对本项目的分析建议（不是上游事实）

- S1/S2/S3 放在初始测试覆盖 / 验证证据中，不作为产品准入枚举、prompt 选择键或硬编码排障流程。
- 产品实现定义通用输入上下文、只读可用工具、证据与输出、状态及预算。接入能力和权限仍有实际边界；不能把通用推理等同于所有系统都受支持。
- 按需知识是否引入，必须有“缺少什么知识导致什么失败”的基线证据；无需为了三类测试先建三套 skills。
- OpenSRE 默认包含终端和修改能力，本项目不能直接继承其权限或 system prompt。需要借鉴机制并单独强制只读。
- 当前可以参考 shared loop、上下文组装、按需知识、停止交接；尚不能宣称这些已经在我们的环境满足长期值守要求。

## 未知与后续验证

未做真实事故运行；未验证所有入口的行为一致性；未完整审查调度和全部 provider 的权限。当前有专用 skills 的事实不意味着存在任意用户可配置 DAG 工作流引擎，该项没有足够证据，保持未确认。
