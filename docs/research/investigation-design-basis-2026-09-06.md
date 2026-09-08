# 调查机制：多项目源码依据与设计取舍

2026-09-06。证据类型：官方文档及固定提交源码静态检查；没有执行上游 Agent 或验证本项目效果。主对标不限制设计来源；以下机制可借鉴，不因项目知名度就称其最优。

## 查明的机制

### HolmesGPT：共享 prompt/loop 与按需排障知识

固定 SHA `5e983c17f30e93099c7d775167266d4cd1d586c4`。

- [prompt.py:179–217](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/prompt.py#L179-L217) 通过共同模板组合 toolset 说明、集群上下文、skills 开关和额外指令；[223–256](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/prompt.py#L223-L256) 组合请求、文件和技能目录等内容。
- [tool_calling_llm.py:1145–1190](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1145-L1190) 维护消息、可用工具、压缩、取消和迭代预算；[1319–1349](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1319-L1349) 在无工具调用时结束回复；[1522–1535](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1522-L1535) 刷新变化的工具列表并处理步数耗尽。
- [generic_ask.jinja2:23–27](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/plugins/prompts/generic_ask.jinja2#L23-L27) 明确要求匹配相关 skill 后读取并遵循步骤；[skills 官方文档](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/docs/reference/skills.md#L1-L17) 描述相同做法。通用调查与专业知识并存。

所查路径没有本项目 S1/S2/S3 分类器，不代表全仓库没有任何专项逻辑。其 prompt 中的工具/命令建议和技能优先级不能原样继承为本项目权限策略。

### OpenSRE：共享调查、上下文组装与停止交接

固定 SHA `cd7e1b9136ca5f9dd3614e586813d98ac9a2a543`。完整入口证据见 [专项笔记](opensre-investigation-source-2026-09-06.md)。

- [官方概览](https://www.opensre.com/docs) 描述读取事件、获取上下文、查询已接工具、检验想法和回答。旧六阶段架构文件当前不存在，不再用其解释现行实现。
- [assemble.py:88–243](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/action/assemble.py#L88-L243) 组合运行事实、integrations、skills、仓库、记忆、对话和任务上下文。
- [react_loop.py:287–483](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L287-L483) 循环调用模型/工具；普通回答可先经过 host 的目标完成判断。另有取消、步数和停滞停止，以及工具禁用后的交接输出。
- [skills/loader.py:229–270](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/skills/loader.py#L229-L270) 提供紧凑目录与按需正文。仓库还有明确的 GitHub CI 健康专项 skill，不能据此声称所有路径都没有专用工作流。

### Stratus：可观察的研究循环与预算终止

核查的是 SREGym 内嵌客户端，固定 SHA `f1e5d2ce4c633c3179ff72b9e0ee11a035247a99`，不代表另一个独立 Stratus 仓库。

[diagnosis_agent.py:21–61](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/stratus_agent/diagnosis_agent.py#L21-L61) 从诊断配置构造工具和预算；[base_agent.py:44–88](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/stratus_agent/base_agent.py#L44-L88) 根据工具调用继续，达到步数上限进入强制提交；[142–154](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/stratus_agent/base_agent.py#L142-L154) 将这些节点接成图。

值得参考的是可追踪的循环与明确预算。benchmark 的强制提交不适合直接当生产调查成功：本项目应保留部分发现并说明预算耗尽、交接。诊断/修复角色的拆分也不要求本项目照搬多 Agent 或增加写操作。

### K8sGPT：确实存在资源分析器与对应 prompt

固定 SHA `731a6c90749e8e62b9325e41712c39c0d72510c4`。

[analysis.go:354–475](https://github.com/k8sgpt-ai/k8sgpt/blob/731a6c90749e8e62b9325e41712c39c0d72510c4/pkg/analysis/analysis.go#L354-L475) 根据资源分析器及过滤配置运行确定性检查；[511–548](https://github.com/k8sgpt-ai/k8sgpt/blob/731a6c90749e8e62b9325e41712c39c0d72510c4/pkg/analysis/analysis.go#L511-L548) 取结果的 Kind，存在相应 PromptMap 时选择专用模板，否则使用默认模板。

这是“上游完全不分类”的反例，但分类依据是资源/插件，不是本项目三个测试症状。确定性检测适合明确可判断的问题；它可以作为调查工具或比较基线，不能由此证明其开放探索能力和通用 loop 相同。

## 设计取舍与验证义务

2026-09-07 用户确认共享调查、按需上下文/专业知识与系统强制运行边界，见 [ADR-0002](../adr/0002-context-driven-investigation.md)。下文具体实现候选和效果仍待验证；此确认不采纳全部候选组件。

**D1 共享调查机制。** 来源：HolmesGPT/OpenSRE/Stratus 上述入口；分析：同一症状多因、多症状共因，测试枚举无法稳定决定调查路径。采用上下文和工具观察驱动后续查询。代价是查询成本与波动；用混合症状、未见原因和变更误导案例验证。暂不引入症状分类器。

**D2 上下文和按需知识分开组织。** 来源：HolmesGPT prompt/skills、OpenSRE assemble/loader。运行身份/时间/权限、可用工具、已有证据、历史对话和审阅知识有不同来源与更新规则。专业知识可以按需取回，但不为每条测试预编答案。代价是检索失配和知识过时；在无skill/相关skill/无关或陈旧skill条件下对照，衡量准确性和成本。

**D3 工具按数据能力和权限开放。** 来源：OpenSRE integrations 工具构造与 HolmesGPT toolset 配置；权限是本项目已确认的只读边界。不能因一个症状未分类而隐藏相关数据工具。代价是工具过多时选错；工具检索、分组或按需发现需要再以相关性/查询成本证明，不预定某种实现。

**D4 完成、预算停止和交接分开。** 来源：OpenSRE 完成判断/停止机制、Stratus 强制提交反例、现有 SPEC 明确的故障处理目标。模型不再调用工具不证明事故已经查清；预算耗尽也不能记成完成。具体确定性状态契约是本项目分析建议。额外 reviewer 是否有收益尚未证明，不预定多模型审查架构；测试提前结束、工具故障、反证与预算耗尽。

**D5 正确性与恢复独立判断。** 来源是用户确认的只读恢复观察要求与现有 SPEC/F6，不宣称上述上游已提供满足本项目要求的 verifier。设计理由：流量消失和缺测会造成假健康。通过无流量/缺数据/持续退化反例验证；此处是自有职责而非照搬 benchmark 提交。

**D6 测试分类与产品实现分离。** 来源是用户本轮明确纠正；实现分析由 D1 和跨项目源码支持。初始症状移到 `docs/testing/initial-investigation-coverage.md`；它们不驱动准入、prompt路由或答案。接入环境、权限和数据可得性仍决定实际能力，测试结果只说明已验证的覆盖。

补充原则来源：[Anthropic Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) 区分动态工具循环与预定义路由，并明确路由适合可准确分类、适合不同处理方式的问题。这是工程方法文章，不是本项目性能证据，不能推出“分类必然不好”或“所有 Agent 都应该只有一个 prompt”。

## 后续设计的准入要求

每个实质设计选择记录：要解决的具体问题；一手来源与版本/路径，或明确的自有分析；至少一种替代方式及代价；适用条件；验证方法。运行收益未知就标待验证。已确认用户需求不必假装是从开源项目发现的；源码存在某机制也不等于该机制适合直接采用。

当前未确定：多 Agent、额外评审模型、知识检索实现、路由策略、存储与工作流框架、具体服务/部署拓扑。没有依据时不增加这些组件。现阶段的文字流程只是职责说明，不应被当成固定串行状态机。
