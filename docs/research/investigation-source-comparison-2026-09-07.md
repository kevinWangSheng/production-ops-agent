# 三项目调查实现：源码追踪结果与待讨论差异

日期：2026-09-07。任务是先还原已有实现，再比较；本轮不产出新的本项目架构决定。

## 阅读入口与证据范围

- [HolmesGPT 审计](holmes-source-audit-2026-09-07.md)：固定 `5e983c17f30e93099c7d775167266d4cd1d586c4`。CLI ask/告警、HTTP chat、可选 Supabase worker、Operator checks；prompt/skills、工具注册执行、两种上下文缩减、临时文件、会话事件和恢复读路径。关联 unit/mock/外部数据库 integration 测试已阅读。
- [OpenSRE 审计](opensre-source-audit-2026-09-07.md)：固定 `cd7e1b9136ca5f9dd3614e586813d98ac9a2a543`。普通交互、headless、resume、后台计划任务；context envelope、工具执行、会话 JSONL、任务定义 JSON 与运行 claim SQLite，关联读写和测试。
- [Stratus 审计](stratus-source-audit-2026-09-07.md)：固定 SREGym `f1e5d2ce4c633c3179ff72b9e0ee11a035247a99`。benchmark driver、diagnosis/mitigation 图、工具节点、MCP 执行、弱 oracle、结果保存与 ATIF 消费及相关测试。

此前 K8sGPT 的资源 analyzer/prompt 分支核查保留在 [设计依据](investigation-design-basis-2026-09-06.md)；本轮没有把它包装成同等深度的全路径审计。

这是固定版本的静态研究，不是最新主分支承诺。全量下载遇到网络问题时用固定 tree/raw 补齐相关源码，未执行上游代码、测试或外部操作。Holmes 下载文件另有 [Git blob 校验清单](holmes-source-manifest-2026-09-07.json)。完整相关调用链不等于读完全仓所有代码。

## 从原生结构得出的结论

### 1. 共用循环不能抹平入口差异

Holmes ask/chat 共用工具循环，CLI 告警 helper 却以 skills=None 构造 prompt；Operator check 使用独立检查 prompt，有 provider 条件下的两阶段格式转换。OpenSRE 普通请求是 ReAct，显式命令走确定工具路径；GitHub CI 计划任务还可以直接返回预取报告。Stratus 是 benchmark 阶段驱动，而不是长期服务入口。

因此“这些系统都是一个通用 prompt”的说法不成立。它们可以共享推理/工具引擎，但输入、工具可见性、知识、输出与持久性仍由实际入口决定。

### 2. Context 是执行过程中形成的状态，不只是初始材料

Holmes 的 prompt 组装、工具结果入消息、skills 按请求加载与两类缩减构成连续路径。OpenSRE 的 envelope 将稳定和临时内容放在不同消息位置，读取积累上下文、历史、目标、计划及恢复提示。Stratus 在图 messages 中累积观察，阶段间另生成摘要；部分看似摘要的工具函数实际上未接通。

不能看到函数名为 summarize/filter 就宣称系统采用了相应策略：必须核对调用方、默认配置和状态更新算子的真实语义。

### 3. 保存历史、保存任务、恢复工具调用是不同能力

- Holmes 普通 HTTP chat 使用调用方传回的历史；可选 Supabase worker 从先前 terminal/approval 事件恢复历史。正常关停会标 timeout，不是任意中断后的自动续跑。
- OpenSRE 显式 resume 恢复消息/上下文/目标/计划；未完成工具 intent 生成重查提示。默认一次性 headless 不开启会话 store。
- Stratus 使用 MemorySaver，每轮尝试重建 Agent，事后轨迹供转换/分析读取，未发现驱动恢复执行的读取路径。

这些事实不否定其各自用途，但都不足以直接满足本项目的持久 incident 责任；适配是否可行需具体设计与运行证据。

### 4. “有持久化/锁/取消”不等于承诺已成立

OpenSRE WAL 写失败被吞，不能叫审计落盘失败时阻止执行；SQLite claim fencing 不等于外部消息恰好投递一次。Holmes Python DAL 包装依赖外部 RPC/数据库实现，内存事件 buffer 仍有硬退出丢失窗口。取消标志或线程池 wait=False 不能保证任意阻塞工具立刻退出。

完整审计应明确执行层、数据存储层和外部服务各自的合同；不能凭一个字段或一段成功路径得出可靠性结论。

### 5. 循环停止、提交答案和服务恢复不能混为一谈

Holmes 普通 loop 无工具调用可结束，check 的 passed 来自模型输出。OpenSRE 有目标审阅、停滞和预算终止，但不证明根因正确。Stratus 的 submitted 只表示向 benchmark 提交，弱 oracle 有查询失败/检查范围的限制。

这支持我们已经接受的“系统状态与调查结论分开、恢复由独立观察判断”；不支持直接照搬任何上游 success 字段作为本项目成功标准。

## 值得讨论的取舍，尚未采纳具体实现

1. 入口统一到什么程度：共享调查引擎的同时，是否采用不同上下文适配器；必须明确每个入口带来哪些真实差异。
2. 证据保留到什么时候：Holmes 临时 spill 文件会删除，不能直接当永久证据；应基于本项目可核查要求比较持久引用方案与存储成本。
3. 恢复到什么边界：上轮完整历史、上次安全检查点、还是重新查询真实状态；分别需要什么日志/幂等/状态契约。
4. 专业知识如何参与：Holmes 有按告警过滤目录，OpenSRE 有专项 skill；哪些机制真正改善我们的问题，必须对照验证，不能把测试分类带入实现。
5. 哪些错误应允许继续、哪些必须停止：上游 best-effort 持久化选择有体验理由，但本项目的状态与证据要求需要逐项评估。

这些问题由源码差异自然产生，不是预先要求系统必须包含某个框架、数据库或多 Agent 架构。

## 未覆盖到足以下结论的部分

- Holmes 外部 Supabase SQL/RLS、pg_cron 实际部署、所有 provider 与 MCP 权限、CRD 生成/重试的完整崩溃窗口。
- OpenSRE gateway 的跨进程会话归属、全部 provider 中断、租约续期与任务编辑竞争的完整运行行为。
- Stratus 安装版本 reducer 的过滤效果、全部 kubectl 安全解析与实际 RBAC、弱 oracle 在运行中的误判概率。
- 三者的真实 token/延迟/成功率、进程 kill 后行为、长期负载效果。本轮读过的测试定义不是测试通过记录。

下一轮可以据此讨论需要采用的行为合同；涉及上述未知项的决定必须补证据。当前 SPEC/ADR/验收保持不变，不因上游做不到就削减产品要求。
