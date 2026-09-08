# HolmesGPT：原生入口、工具循环与不同状态路径

日期：2026-09-07。固定 SHA `5e983c17f30e93099c7d775167266d4cd1d586c4`，不声称本轮已刷新最新 main。整仓下载未完成，改用完整递归 tree 加固定 raw 文件追踪，下载文件与 Git blob 校验见 `holmes-source-manifest-2026-09-07.json`。未安装/执行上游、未运行其测试。

范围：CLI ask/告警、普通 HTTP chat、可选 conversation worker、Operator check，以及这些入口实际连接的 prompt、工具、压缩和状态读写。不是全仓安全审计或生产认证。

## 先区分入口

### CLI ask 与交互

`holmes/main.py:350–417` 在 `tool_result_storage` 生命周期内构造 AI，工具过滤为 CORE/CLI，默认尝试启用无需额外配置的可用工具。普通 ask 经 `build_initial_ask_messages` 调 `ai.call`；交互模式交给 `run_interactive_loop`。[入口](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/main.py#L350-L417)

交互 history 在进程内更新；`/clear` 清空上下文，`json_output_file` 可保存完整结果。所查入口没有把这个输出文件装载成崩溃后执行检查点的路径。[interactive.py:2440–2501](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/interactive.py#L2440-L2501)、[2801–2829](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/interactive.py#L2801-L2829)

### CLI 告警调查

Alertmanager source fetch 后逐 issue 调 `_investigate_issue`，结束可写结果 JSON。[main.py:503–544](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/main.py#L503-L544)

该 helper 使用共享 system template，但传 `skills=None`、source-type 调查补充和 `issue.raw`。因此它不是与普通 ask 相同的技能目录组装路径，不能只看通用 prompt 就声称所有入口都附带 skills。[main.py:163–190](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/main.py#L163-L190)

### 普通 HTTP chat

`server.py:556–660` 从请求取得 ask/history/user_id 等，按用户构造 skills，组装 request context；CORE/CLUSTER、显式配置工具、复用 executor。历史由请求携带；普通请求经 `build_chat_messages`，仅审批/前端工具结果的续接可跳过追加空 user message。[入口](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/server.py#L556-L660)

SSE 分支直接调用 `call_stream` 并传审批/前端工具结果；非流式分支调用 `call`，返回更新后的 history。不能把 conversation_id 的存在当成自动从数据库装载历史。[server.py:710–811](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/server.py#L710-L811)

这不代表服务器没有任何数据库写入：usage/tracing 和下述可选 worker 是其它路径。这里只限定普通 chat 的历史来源。

## 共同循环如何取得和改变上下文

`build_chat_messages` 创建/更新 system prompt，再加入 user 内容；调用 `build_prompts` 组合 toolset 说明、cluster、skills 目录、额外指令及输入文件/图像等，受 prompt component 开关控制。[conversations.py:41–92](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations.py#L41-L92)、[prompt.py:176–294](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/prompt.py#L176-L294)

`ToolCallingLLM.call_stream` 每轮检查取消、预算和上下文大小，调用模型，追加 assistant 消息；有工具调用就执行并添加结果，无工具调用则返回 ANSWER_END。到最后一轮移除工具；模型终止并不是独立正确性判断。[tool_calling_llm.py:1145–1190](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1145-L1190)、[1304–1379](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1304-L1379)、[1522–1535](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1522-L1535)

这支持共享工具反馈循环，不支持“全项目只有一个 prompt”或“所有入口只有一种停止语义”。

## Skills：用户、告警、缓存三种条件不能混淆

`Config.get_skill_catalog` 每请求按终端用户、alert_name 和 hierarchy 构造目录。文件、远端及个人 skill 合并；无显式终端 user_id 不加载个人技能。alert_name 存在时过滤不适用该告警的 skills；chat/CLI 无该值则不过滤。[config.py:400–424](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/config.py#L400-L424)、[skill_loader.py:497–568](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/plugins/skills/skill_loader.py#L497-L568)

worker 才有 `_resolve_alert_name` 并传到目录构造的这条调用链。[worker.py:1057–1125](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L1057-L1125)

目录可见性不等于授权：同名层级选择只影响 prompt catalog，缓存 fetch tool 的 ID 解析是另一回事（skill_loader.py:439–447）。因此应纠正“没有任何按告警限定知识”的泛化；共享循环可以同时带有专业知识筛选。

## 工具从配置到实际结果

`Config.create_tool_executor` 先按 tags/config/prerequisite 准备工具；复用时按 tags/enable_all 缓存。不同入口选项不同。[config.py:431–533](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/config.py#L431-L533)

`ToolExecutor` 仅将 ENABLED toolsets 进入映射，处理重名，按用户展开 OAuth 工具；首次使用可触发 lazy initialization，已经 FAILED 的工具集阻止后续执行。前端工具在克隆的 registry 注入，不能据共享 executor 推断每个请求均完全隔离。[tool_executor.py:61–193](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tools_utils/tool_executor.py#L61-L193)

调用先解析模型参数、查工具、限制重复调用，经 `_directly_invoke_tool_call` 执行并生成 `ToolCallResult`；随后限制大结果，再加入模型消息和 trace。[tool_calling_llm.py:788–849](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L788-L849)、[911–1037](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L911-L1037)

注册、选择、审批、执行与工具背后的身份不是同一层；不能用“工具名是只读”推断 Kubernetes RBAC。脚本工具共享 subprocess 路径目前无通用 timeout 参数，之前 issue 核查见 upstream-led 笔记；本轮未做权限绕过或子进程故障实验。

## 两种上下文缩减及文件生命周期

单结果过大：`spill_oversized_tool_result` 写全文/图像文件，消息改成路径和预览；无可用存储则丢弃该结果并回传收窄查询的错误。调用方还要求具备 bash 文件访问能力才提供目录，不能假定每个入口都能 spill。[limiter.py:33–140](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tools_utils/tool_context_window_limiter.py#L33-L140)、[调用方:1005–1013](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tool_calling_llm.py#L1005-L1013)

整个 history 过大：循环前的 limiter 调用模型摘要。成功后保留 system、user-role summary、最后 user；模型错误或返回工具调用有一次扁平化重试，仍失败则保留原 history 供上层处理。不是无损压缩。[compaction.py:196–340](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/truncation/compaction.py#L196-L340)

`tool_result_storage` 在 context exit 删除 UUID 目录；HTTP 流完成/关闭由 finally 清理，CLI 在 ask/交互上下文退出时清理。临时文件不是持久证据仓库；history 内的文件引用是否跨后续请求仍有效必须单独验证。[filesystem_result_storage.py:26–44](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/tools_utils/filesystem_result_storage.py#L26-L44)、[server.py:532–553](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/server.py#L532-L553)

## Supabase conversation worker：另外一套状态路径

仅 `ENABLE_CONVERSATION_WORKER` 开启时创建 worker；按空闲容量调用 DAL claim，然后派发。[server.py:838–845](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/server.py#L838-L845)、[worker.py:592–659](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L592-L659)

claim、写事件、改 status 和读事件都依赖外部 Supabase RPC。写入携带 assignee/request_sequence；ownership mismatch 停止重试。数据库 SQL/RLS/外部 migration 不在本次已核证据内，不能仅凭 wrapper 证明原子 claim 与部署权限。[supabase_dal.py:1357–1396](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/supabase_dal.py#L1357-L1396)、[1504–1700](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/supabase_dal.py#L1504-L1700)

事件有内存缓冲，terminal/compaction 等触发 flush；最终状态更新前检查 terminal 是否持久化。不能据此保证进程硬退出前每条中间工具结果都落盘。[event_publisher.py:79–137](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/event_publisher.py#L79-L137)、[worker.py:1237–1263](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L1237-L1263)

Hydration 从最新 user event 取得请求，从此前最近 `ai_answer_end`/`approval_required` 的 messages 恢复历史；已有回答则不重复运行。`resume_only` 对应审批/前端结果续接，未见恢复任意半途工具调用栈的路径。[worker.py:989–1030](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L989-L1030)、[1157–1162](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L1157-L1162)

普通停机调用 stop：写 `Holmes Restarted` error，再将活跃会话设 timeout；不是自动继续。SIGKILL/OOM 不执行此 hook，源码注释依赖外部 pg_cron sweep。线程池 wait=False 不证明底层调用被立即终止。[server.py:848–868](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/server.py#L848-L868)、[worker.py:764–832](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/worker.py#L764-L832)

Remote tool worker 另有 claim/结果提交路径，旧 ownership 的结果会丢弃；stop 不把在途工具自动重新排队。不要将它、conversation worker 和请求内 loop 合成一个 exactly-once 引擎。[tool_call_worker.py:247–309](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/core/conversations_worker/tool_call_worker.py#L247-L309)

## Operator/check：共享模型引擎，不同 prompt 与持久状态

CRD handler 创建 Running 状态，调用 HTTP `/api/checks/execute`，再把结果写入 HealthCheck status。[healthcheck.py:28–103](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes_operator/handlers/healthcheck.py#L28-L103)

API 构造 Check 并调用 `execute_check`；后者使用独立 `check_system_prompt`，要求 passed/rationale。Gemini 路由先无 response_format 调查，再 tools-free 结构化转换；其他所示路线直接结构化调用。它没有调用普通 chat 的 `build_chat_messages`，不能套用该路径全部 skills/context 结论。[checks_api.py:69–143](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/checks/checks_api.py#L69-L143)、[checks.py:74–166](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/checks/checks.py#L74-L166)

`check.timeout` 进入 Check 对象和 HTTP payload，但所查 `execute_check` 没有把它作为取消截止时间；HTTP 客户端使用构造时的默认 timeout。请求超时不等于服务端工具被终止。[client.py:25–100](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes_operator/client/holmes_api_client.py#L25-L100)、[checks.py:179–262](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes/checks/checks.py#L179-L262)

发布 trigger 的延迟 pending 信息写 CR status，按 deployment 替换尚未触发项，timer 检查到期项后生成 HealthCheck。它保存计划和结果，不保存 LLM 内部每轮执行检查点。[trigger_executor.py:212–261](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes_operator/trigger_executor.py#L212-L261)、[triggeredhealthcheck.py:198–249](https://github.com/HolmesGPT/holmesgpt/blob/5e983c17f30e93099c7d775167266d4cd1d586c4/holmes_operator/handlers/triggeredhealthcheck.py#L198-L249)

## 已读测试及其证明范围（均未执行）

- `tests/core/test_tool_executor.py:24–108`：mock prerequisites，验证 lazy init 成功/失败与失败后不再执行。
- `tests/core/tools_utils/test_tool_context_window_limiter.py:274–375`：临时文件+fake LLM 验证图像/文本 spill；不是跨请求文件生命周期验证。
- `tests/core/truncation/test_compaction.py:395–567`：fake LLM 检查 tools 附带、fallback、system/user summary 布局及保留原历史；不证明真实模型摘要保真。
- `tests/llm/fixtures/compaction/007_negative_findings/test_case.yaml`：要求保留已排除假设的实际评测输入/预期；只读 fixture 不能声称通过。
- `tests/checks/test_checks_gemini.py:48–120`：mock 模型验证两阶段/单阶段路由；`tests/checks/test_checks_api.py:16–51` mock execute_check 验证 API结果，不能证明故障诊断正确。
- `tests/core/conversations_worker/test_worker_hydration.py:31–123`：首轮/跟进/审批点、已经回答时不重跑。
- `tests/core/conversations_worker/test_worker_lifecycle.py:954–979,1046–1106`：mock DAL 验证 error→timeout、停机预算；不包含实际 SIGKILL。
- `tests/core/conversations_worker/test_dal_contract.py:66–121,171–216,264–395`：RPC参数、重试和 mismatch；不是 SQL 实现验证。
- `tests/core/conversations_worker/integration/test_claiming_concurrency.py:160–202,247–298`：依赖真实 Supabase fixture 的并发 claim/过期 pending 测试；不代表已运行或恢复 crashed running 工具。
- `tests/core/conversations_worker/integration/test_retry_resilience.py:120–188`：短暂503后完成的集成测试定义；不等于进程恢复证明。

## 当前不能下的结论

不能宣称统一持久化恢复、任意工具硬取消、只读身份已保证、摘要无损、健康检查是独立业务真值、所有入口共享相同知识配置。数据库 RPC SQL、provider 全路径、全部 MCP 工具权限、CRD 崩溃窗口/幂等、暂存文件跨请求可用性仍需专项运行或外部实现证据。

这些边界来自入口/调用方/读写/测试交叉检查；本轮不据它们直接选择我们自己的架构，也不修改产品验收来适配上游。
