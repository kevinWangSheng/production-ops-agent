# 工具面拓展 C1：调查任务清单工具 `TodoWrite`

- 状态：进行中（公开接口已写；实现等 C2b（PR #741）与 #722（PR #725）合并后 merge `origin/main` 再开工）
- 更新日期：2026-11-20
- 依据：issue #565；[B/C 批次记录](2026-11-16-tool-expansion-bc.md)；[决定表](2026-11-12-tool-expansion-contract.md) r11 C1-U1=a、C1-U2=a，R8-3、R8-4、R8-9；C3 §5（指令分层与 revision）、§8（工具网关）。预审全文在仓库外 `~/.agent/handoffs/opspilot-c1-prereview-2026-11-19.md`，可逆默认 C1-0 至 C1-14 采纳，偏离两处见「合同（r11）」末两行
- 上游：HolmesGPT `a0009195` `holmes/plugins/toolsets/investigator/{core_investigation.py,model.py,investigator_instructions.jinja2}`、`holmes/plugins/prompts/generic_ask.jinja2:33-43`、`tests/llm/utils/test_toolset.py:178-190`
- 工作区：`../poa-565-todo`，分支 `feature/565-todo`（起点 `a8cf46d9`）

## 目标

扩展档位打开开关时，模型多一个 `TodoWrite` 工具，用来列出并更新本次调查的步骤；工作台显示这份清单与完成状态。它是调查行为工具：不读数据源、不产生证据、报告不能引用，不与数据源工具算同一能力分数。另做一次小规模开关对照并成表。

## 合同（r11）

| 编号 | 要点 | 出处 |
|---|---|---|
| C1-U1 | 不经只读网关，调查 loop 直接处理（串行、并行、故障补跑三处）；结果存同一步已有的工具结果，受同一控制检查；无证据编号，报告不能引用；C3 §8 加一句 | 决定表 r11 |
| C1-U2 | 小对照：主跑照上游（283 开、其余关），另加 283 关 3 遍、4 道 B1 `hard` 题（184、185、193、280）开 3 遍，共多 15 次；单独成表、无门槛；产品默认开不开看表后另开 issue | 决定表 r11 |
| 偏离 1 | 开关取值改为 `1` 开，未设/空/`0` 关，其他值启动失败（预审 C1-2 写的是 `on`）。理由：同档位的 `OPSPILOT_K8S_LIVE_METRICS`（B3-1）与 `OPSPILOT_KNOWLEDGE_TOOL`（C2b）都是这个写法 | 本记录 |
| 偏离 2 | `OPSPILOT_TODO_WRITE` 加进 `deploy/docker-entrypoint.sh` 的 env 文件白名单（预审 C1-2 写不进）。理由：C2b 对同类开关这么做，否则 Compose 部署用 env 文件时 worker 读不到开关；它不是秘密，白名单只决定导出哪些名字 | 本记录 |

## 公开接口

### 1. 工具 schema（照上游，名字不改）

- 名字 `TodoWrite`；唯一参数 `todos`（必填数组），每项 `{id: string, content: string, status: "pending"|"in_progress"|"completed"|"failed"}`，三个字段都必填（上游 `core_investigation.py:42-58`）。上游说明里的 `priority` 不在 schema，不搬。
- 描述用预审 C1-8 原文（去掉上游「会出现在后续提示词」一句，加「不是证据、无 evidence_id、超限整份拒绝」）。
- 校验（预审 C1-5）：≤50 项；`id` 1–64 个可打印字符且清单内不重复；`content` 1–500 字符、可打印；规范 JSON ≤32 KiB；任何一项不合规整份拒绝，`status: "denied"`、`reason: "INVALID_TODOS"`、附一句哪里不合规（不回显内容），上一份清单不变。`content` 先过 `redact_credentials`，存入与回给模型的都是脱敏后的值。
- 模块 `opspilot/investigation/todo.py`：`TOOL_NAME = "TodoWrite"`、`ENV = "OPSPILOT_TODO_WRITE"`、`TODO_WRITE_SCHEMA`（OpenAI function 形状）、`todo_write_enabled(environ) -> bool`（非法值抛 `SystemExit("TODO_WRITE_INVALID")`）、`todo_write_view(operation_id: str, arguments: object) -> dict`（纯函数，只由参数决定，无时间戳）。

### 2. 返回给模型的结果（也是存储的形状）

成功：`{operation_id, tool: "TodoWrite", status: "ok", adopted: false, citable_as_fact: false, todos: [规范化后，保留模型给的顺序], counts: {pending, in_progress, completed, failed}, message: "Task list saved: N tasks. This replaces the previous list. It is your plan, not evidence; it has no evidence_id."}`。拒绝：同形状去掉 `todos`/`counts`，`status: "denied"`、`reason`、`message`。两种都没有 `evidence_id`、`source`、`target_id`、`window`。

### 3. loop 处理与存储

- 分流条件：本 Run 输入记录的 `tool_schemas` 里有 `TodoWrite`，且这次调用名字是 `TodoWrite`。开关关着时模型若写出这个名字，仍照旧交给执行器（未登记工具被拒，行为不变）。分流看输入而不看环境变量，故障补跑与续跑用同一判断。
- 三个调用点按名字先分流，不调执行器：串行（`loop.py` `_run_tools`）、并行（`_run_tools_parallel` 的 `fill`：同步生成结果，不提交线程池、不占并行名额，到它的序号照常提交）、故障补跑（`runner.py` `_proceed` 的 `execute`）。分流前照样 `_renew_before_dispatch()`。
- 提交照现有路径：`_deliver` → `_commit_durable` → `store.commit_tool`（控制检查）→ `tool_committed` 事件（`evidence_id` 为空、`tool` 为 `TodoWrite`）。存在 `opspilot_steps.tool_results` 该序号处。**不改库表、不迁移**；不进 `opspilot_evidence`、不进 `opspilot_tool_charges`、不进 `seen_tool_calls`。
- 状态规则：每份成功的清单整份替换上一份；本 Run 的清单 = 最后一次 `status: "ok"` 的结果；被拒的调用不改清单；同一轮两次以序号后者为准。
- 追踪：一个与执行器同款的 `tool_call` span，名字 `TodoWrite`，输入是脱敏后的参数。

### 4. 模型可见面与 revision

- 开关关（或档位不是 `otel-demo-ext`）：工具数组、`tool_schema_revision`、`prompt_revision`、`context_policy_revision`、渲染出的 system 字节全部不变；`otel-demo` 与 `fixture` 不读开关，设了任何值（含非法值）都不报错、不变。
- 开关开：`TodoWrite` 接在工具数组最末（知识工具之后）；`otel_demo_ext` 的 `_tool_schemas/tool_schema_revision/otel_demo_ext_versions/otel_demo_ext_face/otel_demo_ext_executor_factory` 加关键字 `todo: bool = False`（照 C2b 的 `knowledge=`），只有 `_tool_schemas` 因它变，`_registrations`、`ToolRegistry.revision`、`_data_sources` 不变（它不是登记的网关工具）；新常量 `TODO_TOOL_SCHEMA_REVISION`。
- 提示词：开时变体名加 `-todo` 后缀，段落按固定顺序拼：K8s 段 → 集群段（设了集群名时）→ `TODO_GUIDANCE`（预审附录 5 句）→ 报告契约。变体 `replay-candidate-k8s-todo`、`replay-candidate-k8s-cluster-todo`；`prompt_revision` 因变体与段落而变。上游追加在用户问题后的提醒（`prompt.py:167-173`）不搬，问题字节与 Run 输入不变，结果页披露这一差异。
- Run 输入版本不变：`TodoWrite` 只经已有的 `tool_schemas` 与 `variant_id` 进入输入，不加事实键。
- 开关两进程都读，从传给 `select_profile` 的映射读；web 与 worker 一边开一边不开时，现有 `versions()` 比对让认领 `INCOMPATIBLE_STATE`。

### 5. 与暂停、取消、续跑、重建、压缩的交互

| 情况 | 行为 |
|---|---|
| 暂停、取消 | 提交时 `store.commit_tool` 的控制检查照常拦下（`CONTROL_DENIED`），Run 照现有规则交人；清单停在最后一次提交的那份 |
| 故障补跑 | 已提交计划里未提交的 `TodoWrite` 序号用同一纯函数重新生成，逐字节相同，不调执行器 |
| 重建（`store.rebuild` → `loop.resume`） | 清单在已提交的工具结果里，重建出的对话含它；它没有证据编号，证据撤销过滤不碰它【推断，合同测试 ⑧ 验】 |
| 后继 Run（续接、重新调查） | 从空清单开始，上一份不进新 Run 的提示词；工作台按 Run 分别显示 |
| 上下文压缩 | 不改：`COMPACTION_INSTRUCTION` 与 `context_policy_revision` 不变，压缩后模型按需重发清单（已知限制） |
| `reply_only` 与最终报告轮 | 本来不带工具，不出现 `TodoWrite` |

### 6. 上限与计数

- 不另设调用次数上限。它自身不花模型请求；模型调用它的那一轮照常计 1 次请求，计入唯一的 `MAX_MODEL_REQUESTS_PER_RUN = 100`。单次调用只受 §1 的大小上限。
- 不计入「查询数」：工作台每步与日志头的 queries 数、`summary.json` 的工具调用计数、对照表的查询数都不算它；`step_committed.planned_tools` 保持原义（计所有调用）。#666 的 `include_tool_calls` 附件照上游包含它【推断：上游按全部调用拼附件，实现时核对 `tool_calls.py`】。

### 7. 报告不能引用

靠现有规则，不加新校验：结果无 `evidence_id` 且 `adopted: false`，`delivered_view` 返回 `None`，它不进本 Run 的可引用集合；报告写任何不在集合里的编号照旧被拒。清单内容也不进事实来源。

### 8. 工作台（最小改动，前端截图审由 lead 安排 Opus）

- `opspilot/web/incident_view.py` `_log`：每步的 `calls` 排除 `TodoWrite`，另给步骤项加 `plan: {done, total, todos}`（取本步最后一份成功清单）或 `plan_rejected: true`；日志头 `log.calls` 同样排除。Run 起始项加 `plan`：该 Run 最后一份成功清单（无则 `None`）。
- `_incident_regions.html`：步骤摘要的「N queries」不含它；步骤展开里一行「Plan updated · 2 of 5 done」，再展开看这一版；Run 起始项下一张「Investigation plan」卡片，状态图标照上游 `[ ]` `[~]` `[✓]` `[✗]`，Run 结束后未完成的项照实显示。没有清单的 Run 与改前渲染一致。
- 不加事件类型：`tool_committed` 已触发页面刷新。数据只从现有快照的步骤工具结果取。

### 9. C3 §8 一句（加在 §8 末尾「取消不能撤销…」之后）

> 调查行为工具（目前只有任务清单 `TodoWrite`，扩展档位开关打开时才有）由调查循环处理，不经工具网关，不读数据源，不对外发请求，不产生证据，不能被报告引用；它的结果与其他工具结果存在同一步的记录里，受同一控制检查。

### 10. 驱动开关接口（#666 后续 PR 实现，不在本 PR）

- CLI：`--todo {question,on,off}`，默认 `question`（按题目 `enable_todo` 设，即主跑照上游）；`on`/`off` 强制本轮所有题。
- `tool_env(cfg, backend, *, todo: bool)`：`todo` 为真时 web 与 worker 都加 `OPSPILOT_TODO_WRITE=1`，假时不写该键。驱动每个 trial 起停 web 与 worker，所以开关可以逐 trial 设，不需要分批重启。
- 核对：trial 的 Run 输入 `tool_schemas` 含 `TodoWrite` 当且仅当本 trial 开着，不一致记确定性失败 `TODO_SWITCH_MISMATCH`，不计分。
- trial 记录加 `todo`（布尔）、`todo_source`（`question`/`forced`）、`prompt_revision`、`tool_schema_revision`。对照用独立 `--round` 名（例 `c1-todo-off-283`、`c1-todo-on-hard`），不进主分。

### 11. 对照表（C1-U2）

每行一题 × 一种开关，3 遍：

| 列 | 口径 |
|---|---|
| 通过率 | 通过次数/3，同一 judge 与 R8-3 口径 |
| 模型请求数 | `run_completed.model_requests_used`，列 3 遍的值与均值 |
| 查询数 | 该 Run 所有步骤工具结果中 `tool != "TodoWrite"` 的条数，3 遍值与均值 |
| 是否真的调用 | `TodoWrite` 成功次数/被拒次数；3 遍中调用过的遍数 |
| 完成条数 | 最后一份成功清单的 `completed`/总数（另列 `failed`、未完成）；没调用记「—」 |
| 耗时（附加） | Run 认领到结束 |

行：283 开（主跑）、283 关（加跑）、184/185/193/280 开（加跑）、184/185/193/280 关（主跑现有结果）。只照实报告，不设门槛。

## 合同测试要覆盖的行为（独立作者按本节与 C3 写，不看实现）

1. 开关关：两个档位的工具数组、三个 revision、system 字节与改前钉值相同；只设集群名时等于 #722 钉值；`otel-demo` 设任何值不变。
2. 开关开：工具数组末尾多 `TodoWrite`，`tool_schema_revision`、`prompt_revision` 变，`context_policy_revision` 不变；与集群名、知识开关组合两两不同；变体名按后缀规则。
3. 非法值两进程都 `TODO_WRITE_INVALID`；web 开 worker 关时认领 `INCOMPATIBLE_STATE`。
4. 一次调用：执行器调用数 0、无证据行、无计费行；步骤工具结果有清单；`tool_committed` 的 `evidence_id` 为空。
5. 报告引用伪造编号被拒；清单不进可引用集合。
6. 51 项、重复 id、超长、非法状态、非数组、缺字段都整份 `INVALID_TODOS`，上一份不变；含 `password=...` 的内容存入与回给模型前已脱敏。
7. 同一轮与其他工具并行：按序号提交，其他结果不受影响；同轮两次以后者为准。
8. 故障补跑与重建：补跑逐字节相同、不调执行器；重建出的对话含清单。
9. 暂停后提交被拦，Run 照规则交人。
10. 开关关时模型写出 `TodoWrite`：照旧交给执行器被拒。
11. 工作台：有清单的 Run 显示计划卡片与「Plan updated」行，queries 数不含它；无清单旧 Run 渲染与改前一致。

R8-25 集成测试不适用（无后端容器）；R8-9 的「报告引用其证据」不适用，换成真实 Run 的 trace 有调用、工作台截图看到清单（#669 租机冒烟 283，web 与 worker 都设开关）。PR 正文写明。

## 执行进展与证据

- 2026-11-20：公开接口写完（本节以上）。未跑任何测试（本步只写接口；本机内存紧，不跑全量）。答案隔离：没读任何题的 `expected_output`。

## 下一步与交接

- C2b（PR #741）与 #722（PR #725）合并后，本分支 merge `origin/main`，按本接口实现；合同测试交独立作者。
- 驱动侧 §10 另开 #666 后续 PR；对照在 #669 租机时跑，结果表写回本记录。
