# Stratus：从 benchmark driver 到调查循环与结果归档

核查日期：2026-09-07。只读取完整源码快照，未安装依赖、执行 Agent、运行测试或操作集群。
固定提交：`f1e5d2ce4c633c3179ff72b9e0ee11a035247a99`。
本地快照：`/tmp/sregym-audit-20260907`（浅克隆包含完整当前源码树，未取完整历史）。
下述路径相对该快照；固定链接前缀为 https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/ 。

## 先还原它自己的组织方式

Stratus 在本仓库中是 benchmark client：driver 根据 benchmark stage 运行 diagnosis，必要时转 mitigation。
它不是一个已证明持久值守、多事故并发、进程崩溃后续跑的服务。
其实际结构是 driver 编排阶段和重试、BaseAgent 执行模型/工具循环、具体 Agent 配置工具、MCP 执行查询或变更、oracle 检查 mitigation。
这是一项从调用路径得出的结构结论，不据 README 的自治描述扩大推断。

## 入口与上下文如何真正到达模型

1. `clients/stratus/stratus_agent/driver/driver.py:720-733,779-843`：`main` 解析 problem ID、建立输出目录，等待 benchmark 从 setup 切换到 diagnosis/mitigation/done。
2. `driver.py:785-816`：diagnosis 阶段调用 `diagnosis_with_localization_task_main`；其结束后生成摘要，传给 mitigation。若 benchmark 直接处于 mitigation，则跳过 diagnosis。
3. `driver.py:385-410`：从固定 diagnosis YAML 读取 system/user 模板，从 app-info API 取得 app_name/descriptions/namespace，填入 user prompt，加 max_step。
4. `driver.py:233-247`：app-info 来自 HTTP API，并非本函数根据故障类别分发 prompt；请求失败返回字符串 error，调用端继续下标访问，没有完整降级协议。
5. `clients/stratus/configs/diagnosis_agent_prompts.yaml:1-81`：模板描述通用定位任务，要求检查服务状态与遥测；未按照测试症状选择三套调查流程。
6. `clients/stratus/stratus_agent/diagnosis_agent.py:21-61`：配置工具通过 `str_to_tool` 转成工具实例，构建同一个 BaseAgent graph。
7. `clients/stratus/stratus_agent/base_agent.py:44-55`：每轮把当前 messages 和全部已配置 tools 传给模型，再按是否有 tool_calls 决定进入工具节点或结束。

因此可支持的结论是：此路径按角色配置工具与 prompt，轮内由模型选择工具；不是按故障分类执行固定分支。
不能据此断言任何模型都能调查任意故障：数据、工具、prompt 和 benchmark 环境仍限定实际能力。

## 图循环、状态与结束语义

- `base_agent.py:142-157`：图为 START → call_model → tool_node → post_round_process → call_model；条件边也可 END 或 force_submit。
- `clients/stratus/stratus_agent/state.py:8-26`：状态只有 messages、num_steps、submitted、rollback_stack、executed_commands；messages 用 add_messages reducer，命令列表用 operator.add。
- `base_agent.py:90-93`：num_steps 在每次工具节点完成后加一，因此实际是工具轮次，不是 YAML 注释声称的单次 tool call；同一模型消息可以带多个工具调用。
- `base_agent.py:51-62`：无工具调用立即结束；工具处理后若 submitted 则结束，否则达到 max_step 才强制提交。
- `base_agent.py:64-88`：diagnosis 强制要求提交最佳答案，必要时再索取纯文本，通过 manual_submit_tool(stage=diagnosis) 提交。
- `clients/stratus/stratus_agent/mitigation_agent.py:23-28`：mitigation 覆盖该方法，只设置 submitted，真正 benchmark 提交由 driver 决定。
- `base_agent.py:47-48` 与 `llm_backend/get_llm_backend.py:177-192`：特定 IndexError 重试耗尽返回 Server side error；BaseAgent 不追加此 AI 消息，后续可能因最后一条不是带调用的 AI 消息而结束。不是专门持久失败状态。

`submitted` 表示提交行为，不等于调查正确或业务恢复。不能直接映射为生产产品的 success。
代码中没有与上述状态对应的持久 paused/cancelled/human-handoff 状态设计。

## 工具调用与实际权限关系

- `clients/stratus/configs/diagnosis_agent_config.yaml:1-77`：默认工具为 traces/services/operations/dependency graph/metrics/read-only kubectl/submit，max_step=20。
- `clients/stratus/stratus_utils/str_to_tool.py:45-83`：白名单名称映射为实例；这不是远端工具动态发现。kubectl 使用 MCP 客户端与生成的 session ID。
- `clients/stratus/tools/stratus_tool_node.py:38-112`：按模型返回顺序串行 await/invoke；传入当前 state，要求结果是 Command，合并 updates，收集 ToolMessages。
- 同文件 `:13-28` 虽定义 submit-first/wait-last 的 reschedule 函数，但当前 __call__ 没有调用它，不能据函数名声称提交一定优先执行。
- 同文件只捕获 ValidationError 并回传 ToolMessage；其他异常可向图外传播。多工具同轮看到同一 inputs，非 messages 的同名更新采用后值覆盖，不能泛称支持事务合并。
- `clients/stratus/tools/kubectl_tools.py:182-217`：diagnosis wrapper 检查 command 前缀和一种 logs -f 写法，然后仍调用 MCP 的 exec_kubectl_cmd_safely。
- `mcp_server/kubectl_server_helper/kubectl_cmd_runner.py:27-93,95-156`：服务端还做 AST/命令安全检查、dry run，并可能走实际变更及 rollback-stack 分支；它是共用执行器，不是独立只读身份。
- `mcp_server/kubectl_mcp_tools.py:14-18,55-69`：按 session cache 取得工具集执行。session 标识组织工具状态，不能据此证明 Kubernetes RBAC 隔离。
- `kubectl_tools.py:38-85`：部分连接/读取异常会重连重试；这不是工具节点对所有异常统一恢复。

因此不能直接将 diagnosis 的工具名/前缀过滤当作我们要求的目标只读授权证明。
本轮未审计所有 kubectl parsing、kubeconfig 与部署 RBAC 路径，不宣称发现可利用绕过或已证明安全。

## 上下文变化：已接通机制与未接通代码分开

- `base_agent.py:95-140` 有删除旧 Command Rejected 消息的过滤意图，但 `post_round_process` 返回普通过滤后列表，而 state 的 messages 使用 add_messages。
- 这不能单凭过滤函数证明 graph 内旧消息真正被删除：需要核对安装版本 reducer 或用最小执行测试确认；本轮未运行，保留为语义疑点。
- `clients/stratus/tools/jaeger_tools.py:29-65` 的 traces 工具实际调用 truncate_to_tokens，summary 分支被注释掉；不能因存在 _summarize_traces 就声称默认启用摘要。
- `clients/stratus/stratus_utils/truncate_by_token.py:4-26` 默认 6000 token；未截断时返回(text,count) 元组，截断时返回字符串，调用端 str(result)，这是输出形态不一致的静态事实。
- `llm_backend/get_llm_backend.py:118-192`：部分 LiteLLM 错误触发下一次请求 trim；BadRequest 直接抛出，不是统一的 token 阈值压缩策略。
- `llm_backend/trim_util.py:8-38`：深拷贝消息，保留末尾 30 条，对更早 HumanMessage 内容改成省略号，保留其他角色。不是对所有工具证据做语义摘要。
- `mitigation_agent.py:69-88`：阶段摘要确实调用模型，将 last_state messages 放入摘要请求；driver 用其作为下一阶段/下一次尝试输入。

## retry 与 oracle：不能混称恢复

`driver.py:428-474` 默认组装 ClusterStateOracle 和 AlertOracle；WorkloadOracle 接入代码被注释。
`driver.py:585-706` 的 validate retry 模式：运行 mitigation → oracle → 成功提交；失败有次数则确定性 rollback → 摘要/反馈组成新 prompt → 新 Agent 再运行。
`mitigation_agent.py:91-105` 每次 single/retry 均 build 新 agent，结束清内存。这是新尝试，不是从中断指令继续。
`driver.py:703-705` 最后尝试仍向 benchmark 提交，不论 weak oracle 结果；提交与正确性再次分离。
`driver.py:215-226` 检查所有配置的 oracle，将失败项反馈，未通过由外部逻辑决定。
`weak_oracles/cluster_state_oracle.py:7-98` 默认 namespace=default，检查 Pod/容器状态；driver 此处未传 app_namespace，不能泛称它检查目标业务全貌。
`weak_oracles/alert_oracle.py:45-114` 等待告警安静窗口，但查询失败/JSON错误返回空列表，teardown/done 也可提前走 PASS。
这些是弱检查实现的限制，不是独立业务恢复真值；本轮未运行证明出现误判。

## 保存在哪里，重新运行读取什么

`base_agent.py:156-175,247-275` 使用 MemorySaver、固定 thread_id=1，初始 state 重新构造；graph events 在 Python 列表积累。
`diagnosis_agent.py:64-69`、`mitigation_agent.py:91-105` 在 run 完后清除该 thread 的内存存储。
`driver.py:78-211,893-895` 在完整流程末尾写 combined trajectory JSONL 和统计 CSV；不是每次工具前后持久事务检查点。
`base_agent.py:189-230` 另有 save_trajectory 方法，但当前主路径使用 driver 的 combined writer。
`atif_converter/adapters/stratus.py:271-304` 读取 JSONL 后转换 ATIF steps/metrics/stages，用于分析，不重新驱动 graph。
在 clients/stratus 与 clients/harness 搜索 trajectory/resume/checkpoint，未发现把保存轨迹还原为图执行状态的调用路径。
因此已证据支持的是内存 checkpoint + 事后轨迹，不是崩溃后 durable resume；中途异常时能否获得完整 combined JSONL 未受保证。

## 测试交叉核查与剩余验证

- `tests/traces/test_stratus_adapter.py:1-25,31-97` 用真实旧轨迹和合成新轨迹核查 ATIF 转换、stage、提交与 schema；支持“轨迹可消费”，不支持“可恢复执行”。
- `tests/traces/test_stratus_edge_cases.py:42-55,158-199` 覆盖缺文件、空/异常内容等转换边界；不是运行 Agent 的故障注入测试。
- `tests/stratus/test_usage_reporting.py:1-51` mock driver 依赖检查统计保存；不能作为真实 token 计费或任务恢复证明。
- 搜索 tests 中 BaseAgent/clear_memory/force_submit/trim_messages_conservative 未找到对应直接测试；不据此声称整个仓库没有其他间接覆盖。

下一层证据应是隔离最小测试：reducer 删除语义、多调用预算、工具异常结束行为、进程中断保存边界、弱 oracle 查询失败语义。
这些是验证候选，不在本轮执行，不替本项目选择框架，也不将 benchmark 机制直接升级为生产设计。

固定源码导航：[driver](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/stratus_agent/driver/driver.py#L779-L895)、[BaseAgent](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/stratus_agent/base_agent.py#L44-L175)、[ToolNode](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/clients/stratus/tools/stratus_tool_node.py#L38-L112)、[轨迹读取](https://github.com/SREGym/SREGym/blob/f1e5d2ce4c633c3179ff72b9e0ee11a035247a99/atif_converter/adapters/stratus.py#L271-L304)。
