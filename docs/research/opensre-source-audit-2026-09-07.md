# OpenSRE 原生执行、上下文与持久化审计

日期：2026-09-07。源码固定为 `cd7e1b9136ca5f9dd3614e586813d98ac9a2a543`（2026-09-06 已核查的 main）。
本轮刷新 API/整仓 tarball 响应缓慢，未把“最新 main”当已确认事实。采用该固定 SHA 的递归 tree 与 raw 文件深入追踪；下载到 `/tmp/opensre-research`，未执行上游代码。
范围：普通交互、headless、会话恢复、后台调度；不据此选择本项目框架。

## 结论及证据等级

- **静态实现事实：**共享 ReAct 调查循环，但同时存在确定命令路径和专用 skills；没有所提三症状选 prompt 的依据。
- **静态实现事实：**上下文包括真实会话/工具/知识/运行状态，结果循环回传模型；不是单次“输入 context 出报告”。
- **静态实现事实：**有 JSONL 会话、JSON 任务定义和 SQLite 调度 claim 三种存储；职责不同。
- **重要限制：**默认 one-shot headless 不开会话 store；WAL 落盘失败不会阻止工具执行；resume 不恢复活工具调用。
- **未获得证据：**真实故障运行、进程 kill 恢复实测、并发部署效果、所有工具取消时限。本轮未运行测试。

## 官方文档与源码边界

[官方概览](https://www.opensre.com/docs) 描述 alert/context/tools/digging/answer；这是行为概述，不是固定阶段状态机。
[README](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/README.md#L135-L160) 描述 shell、headless、Python API。不同入口的会话配置不同，不能把 /resume 特性直接推广到所有 headless runner。
旧 `docs/investigation-pipeline-architecture.md` 不在已固定 tree 中；不复用旧六阶段表述。
GitHub CI skill 文档仍描述利用预取报告，而真实 runner 直接返回报告、不调用 LLM；以调用代码为准。

## 调用链证据

### 普通入口

run_turn 先压缩、扩展已有 follow-up，再构建 TurnSnapshot/TurnPlan，调用 execute_actions，记录结果。普通输入未按症状类别选 prompt。 [core/agent_harness/turns/orchestrator.py:80-138](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/orchestrator.py#L80-L138)

### Agent 构建

显式 !shell 和 slash 使用确定性工具调用；其余走共享 envelope、LLM 与 goal reviewer。这个入口区分不是事故症状分类。 [core/agent_harness/turns/action_driver.py:450-544](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/action_driver.py#L450-L544)

### 调用工具集

读取当轮 resolved integrations，组装工具，构建 Agent，经 telemetry wrapper 执行。 [core/agent_harness/turns/action_driver.py:866-920](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/action_driver.py#L866-L920)

### 上下文组装

shared base、runtime facts、skills index、integrations、repository、memory、conversation、prior action facts、recovery note、task plan 分块进入 envelope。 [core/agent_harness/prompts/action/assemble.py:88-243](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/prompts/action/assemble.py#L88-L243)

### 上下文传递

cached 部分进入 system，ephemeral 部分随 user message 进入；不是所有上下文都在 system prompt。 [core/agent_harness/turns/action_driver.py:486-518](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/action_driver.py#L486-L518)

### 可用性

从 registry 选择 action/chat surface 工具，再依据配置 integrations 过滤。 [core/agent_harness/tools/action_tools.py:52-75](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/tools/action_tools.py#L52-L75)

### 工具执行

一个 sequential 工具使批次串行，否则线程池执行并按原请求顺序返回；本层未见统一截止时间，不能保证取消立即杀死底层调用。 [core/tool/execution.py:226-288](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/tool/execution.py#L226-L288)

### 单工具执行

查名称与可用性、before hook、分派 RuntimeTool/RegisteredTool、after hook，返回结构化结果。 [core/tool/execution.py:338-463](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/tool/execution.py#L338-L463)

### 结果再入模型

执行结果经 formatter 写入 messages，驱动后续模型调用。 [core/agent/react_loop.py:441-483](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L441-L483)

### 停止

没有工具调用时仍会检查 queued follow-up 和 host conclusion acceptance；不接受时追加 nudge。 [core/agent/react_loop.py:394-439](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L394-L439)

### 预算/停滞

tool terminate、stagnation、iteration cap 对应停止；安全停止禁用工具做最后交接，失败有固定 fallback。 [core/agent/react_loop.py:519-624](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent/react_loop.py#L519-L624)

### Headless 公共 API

run_headless_turn=start(...).chat(message)，一轮 convenience API，不是持续工作流执行器。 [core/agent_harness/harness.py:198-224](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/harness.py#L198-L224)

### 默认持久性条件

SCHEDULED_RUN_CONFIG 默认 persistent_tasks=False、open_store=False；因此单次后台报告默认不留下持久会话。 [core/agent_harness/harness.py:101-110](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/harness.py#L101-L110)

### 显式 resume API

传 session_id 时委托 SessionManager.resolve，否则 create；持久性由 SessionConfig 控制。 [core/agent_harness/harness.py:370-398](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/harness.py#L370-L398)

### Headless 外层

handle 单轮与 run_goal 跨轮共享入口；run_goal 的 cancel_requested 在外层轮次间检查，不等于中断任意工具。 [core/agent_harness/turns/headless_agent.py:105-177](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/headless_agent.py#L105-L177)

### 会话读取

resolve 构造并 bootstrap，repo.load_session，再 restore 与 reopen。 [core/agent_harness/session/lifecycle.py:158-176](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/lifecycle.py#L158-L176)

### 恢复字段

恢复 user/assistant 消息、accumulated_context、goal、task plan、history；没有恢复正在执行的线程/网络调用栈。 [core/agent_harness/session/lifecycle.py:254-291](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/lifecycle.py#L254-L291)

### 交互 resume 调用方

读取数据后切换 session identity，rebind_for_resume 与 restore_context；空会话不会被盲目接管。 [surfaces/interactive_shell/command_registry/session_cmds/resume.py:63-115](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/surfaces/interactive_shell/command_registry/session_cmds/resume.py#L63-L115)

### 磁盘读模型

load_session 读取 JSONL 并按分支恢复；WAL sidecar 独立扫描。 [core/agent_harness/session/persistence/jsonl_repo.py:50-100](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/jsonl_repo.py#L50-L100)

### 持久写模型

flush 把上下文/goal/plan/messages 等变化追加并写 leaf；reopen 清除缓存，使后续从磁盘解析。 [core/agent_harness/session/persistence/jsonl_store.py:393-491](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/jsonl_store.py#L393-L491)

### WAL 调用方

tool start 写 intent、end 写 commit；使用默认 store，失败被吞掉，不能称 fail-closed durability。 [core/agent_harness/turns/wal_recorder.py:45-86](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/turns/wal_recorder.py#L45-L86)

### fsync 实现与限制

durable=True 时 flush+fsync；但外层 suppress(Exception)，缺文件返回空串。因此只是成功路径的 write-ahead，不保证每次工具执行前都已可靠落盘。 [core/agent_harness/session/persistence/jsonl_store.py:504-558](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/jsonl_store.py#L504-L558)

### 锁条件

跨进程锁默认关闭，需配置环境变量开启；不能默认声称多进程写会话安全。 [core/agent_harness/session/persistence/jsonl_store.py:33-42](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/jsonl_store.py#L33-L42)

### 锁失败

锁超时跳过写入并告警；非事务性工具执行门禁。 [core/agent_harness/session/persistence/jsonl_store.py:103-145](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/jsonl_store.py#L103-L145)

### 崩溃恢复含义

dangling intent 生成恢复提示：先重新查真实状态，再继续剩余任务。不是自动重放副作用或 exactly-once。 [core/agent_harness/session/persistence/wal_recovery.py:22-90](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/persistence/wal_recovery.py#L22-L90)

### 退出持久化

close/flush 为 best-effort；异常记日志而不向上抛。正常退出也不能等同于强持久事务提交。 [core/agent_harness/session/lifecycle.py:293-354](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/core/agent_harness/session/lifecycle.py#L293-L354)

### 后台调度入口

APScheduler callback 从 store 取最新任务并检查 enabled，execute_task；过期 claim 可重新投递。 [infrastructure/scheduling/scheduler/runner.py:104-158](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/runner.py#L104-L158)

### 调度执行

claim→build_message→delivery fanout→complete_run。调度历史是 SQLite，不是调查会话 JSONL。 [infrastructure/scheduling/scheduler/executor.py:40-172](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/executor.py#L40-L172)

### claim 存储

SQLite immediate transaction 按 task/fire_time claim，租约过期可新 attempt/owner 接管。 [infrastructure/scheduling/scheduler/storage/run_store.py:51-104](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/storage/run_store.py#L51-L104)

### 完成 fencing

完成 UPDATE 包含 attempt 与 owner_token，拒绝被接管的旧 worker completion。外部消息送达与数据库完成不构成原子事务，不能推断恰好一次投递。 [infrastructure/scheduling/scheduler/storage/run_store.py:126-170](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/storage/run_store.py#L126-L170)

### 调度 skill

按 pinned name/revision 校验输入并取上下文；多数用 unattended headless，一些有专用路径。 [integrations/scheduled_skill_runner.py:43-85](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/integrations/scheduled_skill_runner.py#L43-L85)

### 重要专项反例

github-ci-health 直接返回预取报告，不调用 Agent。这修正此前“该 skill 经 Agent 忠实输出”的概括。 [integrations/scheduled_skill_runner.py:59-64](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/integrations/scheduled_skill_runner.py#L59-L64)

## 相关测试：已阅读，不等于已通过

- mock 验证 startup/sink/dispatch 和 prepare 在 build 前，不证明磁盘恢复：[tests/core/agent_harness/test_run_headless_turn.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/core/agent_harness/test_run_headless_turn.py#L35-L110)。
- 临时磁盘 JSONL 检查 context/messages 和 leaf 计数：[tests/core/agent_harness/session/test_jsonl_flush.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/core/agent_harness/session/test_jsonl_flush.py#L65-L111)。
- 明确测试默认不锁、启用锁后竞争时跳过写入、flush 可完成：[tests/core/agent_harness/session/test_jsonl_store_lock.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/core/agent_harness/session/test_jsonl_store_lock.py#L43-L103)。
- 会话 ID 接管后，slash 与 chat 追加到目标 JSONL：[tests/interactive_shell/sessions/test_resume_scenarios.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/interactive_shell/sessions/test_resume_scenarios.py#L182-L237)。
- 取消 action 或 action 后 host cancel 会形成取消结果；不能外推底层工具终止：[tests/core/agent_harness/test_host_cancel_turn.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/core/agent_harness/test_host_cancel_turn.py#L52-L119)。
- 检查 unattended 工具筛选，以及 github CI 不经 Agent 截断、morning report 使用 pinned recipe：[tests/integrations/test_scheduled_skill_runner.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/integrations/test_scheduled_skill_runner.py#L19-L136)。
- 模拟 scheduled skill runner 与投递管线，记录投递；非真实服务/模型运行：[tests/scheduler/test_recurring_skill_acceptance.py](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/tests/scheduler/test_recurring_skill_acceptance.py#L30-L110)。

## 反证与仍需追踪的边界

1. “有 WAL”不能推导出审计丢失时停止执行：写入 suppress 和回调吞错是直接反证。
2. “支持 resume”不能推导出任意后台任务断点续跑：one-shot open_store=False 是直接反证。
3. “通用循环”不能推导出没有专项工作流：pinned skills 与 CI 直接报告分支是直接反证。
4. “支持取消”不能推导出中断所有外部调用：tool executor 的线程池等待没有统一 timeout，需要逐工具/provider 核查。
5. claim fencing 防旧 worker 更新 run，不证明外部投递恰好一次；发送与 complete_run 之间仍需实测崩溃窗口。
6. scheduler 成功后对先前读取的 task 调用 update_task；task_store.py:270-282 把完整 model_dump 替换现有记录。静态上存在覆盖运行期间编辑的窗口，文件锁仅序列化写入、不解决 stale object。尚未运行并发复现，不能宣称已修复。
7. 当前已串起主入口、实现、读写与若干相关测试；不能声称全项目所有原生入口审计完成。Gateway host 的会话归属/跨进程协调、provider 中断、租约续期、任务编辑竞争仍未完整覆盖。
8. 没有把本次审计转成我们的架构决定；上游事实与本项目需求适配另行评审。

## 任务定义存储补充

[infrastructure/scheduling/scheduler/storage/task_store.py:130-157](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/storage/task_store.py#L130-L157) 使用临时文件、fsync、replace 保存 JSON 任务定义；与 SQLite run history 分离。
[update_task:270-282](https://github.com/Tracer-Cloud/opensre/blob/cd7e1b9136ca5f9dd3614e586813d98ac9a2a543/infrastructure/scheduling/scheduler/storage/task_store.py#L270-L282) 持文件锁但替换整个旧对象。
