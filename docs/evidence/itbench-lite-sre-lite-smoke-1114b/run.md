# ITBench-Lite 冒烟：2 题 × 1 次（#618）

类别：集成运行（真实 DeepSeek 模型、回放组件、真实 judge），不是全量评测，冒烟题不计分（#616）。依据 [租机手册](../../tasks/2026-11-13-rental-runbook.md) 阶段 2。主机与阶段 1 见 [租机记录](../rental-2026-11/run.md)。时间为太平洋时间（PST），2026-11-14。

- **提交 S'**：`748ecefa9504fb818d143368ce2059a77de1f4f9`（提交 S `c8f753f9` 加 #624 修复，PR #626）。主机主 checkout 快进到 S'，`uv.lock` 未变，无已跟踪修改。
- 档位 `otel-demo-ext`；版本 `prompt-replay-candidate-k8s-dbc726eb5ae5` / `otel-demo-ext-d467c4b436b0` / `ctx-ctx-policy-v1-a30e65ea52f4`。
- 数据集 `ibm-research/ITBench-Lite` SRE v0.2，HF revision `d0916b08`；数据集根只作为路径传给驱动，没有打开答案文件。
- lab 同时运行（灌入耗时是在 lab 开着时测的）。`lab_actions`：无。
- 所有命令用 `.venv/bin/python`：主机系统 `python3` 没有项目依赖（`ModuleNotFoundError: No module named 'fastapi'`），手册写的 `python3 -m …` 在主机上跑不起来（文档问题，交 #620）。

## 时间线

| 时刻（PT） | 步 | 结果 |
|---|---|---|
| 09:03–09:05 | 2.1 白名单复制（轮次 `lite-smoke-1114`） | 35/35，每题约 2–3 秒 |
| 09:05–09:11 | 2.1 词表 | 35/35 `S<id>.txt`，无报错 |
| 09:11 | 2.1 分档（提交 S） | **退出码 1**：`shift.py:210` 对真实 `Events.Timestamp`（`[np.datetime64('…')]`）`ast.literal_eval` 失败，35 题全中。类别 ①，开 #624。全量扫 5,361,980 行 trace：其余 8 个 list/dict 列都能解析 |
| 10:25–10:31 | 2.1 分档重跑（提交 S'） | 退出码 0，35 题：`指标+trace` 14、`故障窗无 trace` 21；用时 5 分 46 秒。复制与词表沿用 |
| 10:31 | 2.2 挑题 | 手册 jq 命令输出 S1、S5（trace 与日志都覆盖故障窗，去掉 S18/S38，按编号前两个） |
| 10:31–10:33 | 2.3 S1，轮次 `lite-smoke-1114` | 驱动退出 0、`completed`，但 Run 首个模型请求 0.3 秒即 `MODEL_REJECTED` 转人工，0 token、无报告。原因：主机 `lite.env` 的 `DEEPSEEK_API_KEY` 被 DeepSeek 以 401 拒绝（`/models` 探测）。类别 ①（环境），用户换 key 后 `/models` 200 |
| 10:57–11:04 | 2.3 S1 重跑，轮次 `lite-smoke-1114b` | 通过，见下 |
| 11:06–11:13 | 2.4 S5，轮次 `lite-smoke-1114b` | 通过，见下 |
| 11:14 | 收尾检查 | 29110/29190/29186/29118/29143/29180 无监听；`itbl-*` 容器 0 个 |

## 每题判定

| 项 | S1（Scenario-1） | S5（Scenario-5） |
|---|---|---|
| 驱动退出码 | 0 | 0 |
| `summary.json` status | completed | completed |
| `complete.json` | 有 | 有 |
| 作废 / 机会 | 0 / 1（`history` 空） | 0 / 1 |
| `load_seconds` | 86.7 | 84.4 |
| `gap_seconds` | 5.0 | 5.0 |
| `data_end_to_delivery_seconds` | 151.8 | 150.3 |
| 假 server 加载 | 23.8 秒 | 14.3 秒 |
| 内存（`memory_before` used / available，MiB） | 2970 / 29119；运行中峰值约 4.5 GB used，swap 0 | 2986 / 29103；swap 0 |
| `target_fallback` | false | false |
| `run.outcome` | available，`completed`，`report_available`，41 条证据 | available，`completed`，`report_available`，33 条证据 |
| 模型轮次 / token（prompt，其中缓存命中 / completion） | 24 / 1,610,997（1,513,600）/ 48,155 | 20 / 1,271,768（1,190,656）/ 27,525 |
| `conversion.reason` | null | null |
| judge | `judged: true`，official 非空 | `judged: true`，official 非空 |
| git | `748ecefa`，dirty false | 同 |
| LangSmith | [S1 trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/fcee460d-b2eb-43ad-9c6b-9de9c975d1f1/r/00000000-0000-0000-fc2c-d8b8c06591f2)（read_back found） | [S5 trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/fcee460d-b2eb-43ad-9c6b-9de9c975d1f1/r/00000000-0000-0000-b95e-bab4c889f5f1)（read_back found） |

灌数据远低于 4 分钟线，作废线（灌完到投递 2 分钟）两题都没碰到。分数只记录、不作门槛、不作改动理由，这里不列。

## 工具调用（`run.tool_requests`，ok / no_data / error）

| 工具 | S1 | S5 |
|---|---|---|
| metrics_range_query | 9 / 0 / 0 | 11 / 0 / 0 |
| fetch_pod_logs | 8 / 4 / 0 | 1 / 2 / 0 |
| kubernetes_jq_query | 4 / 0 / 0 | 7 / 0 / 0 |
| kubernetes_tabular_query | 3 / 0 / 0 | 1 / 0 / 0 |
| traces_search_by_tags | 2 / 3 / 0 | 0 / 0 / 1（RESULT_TOO_LARGE） |
| traces_search | 1 / 1 / 1（RESULT_TOO_LARGE） | — |
| traces_query_trace_by_id | 1 / 0 / 0 | 1 / 0 / 0 |
| traces_list_operations | 1 / 0 / 0 | — |
| get_metric_names | 1 / 1 / 1（INVALID_PARAMS） | 2 / 0 / 0 |
| get_label_values | 1 / 0 / 0 | 1 / 0 / 0 |
| execute_prometheus_instant_query | — | 2 / 0 / 0 |
| get_series | — | 2 / 0 / 0 |
| list_prometheus_rules | 0 / 1 / 0 | 0 / 2 / 0 |
| read_evidence | — | 1 / 0 / 0 |

两题都没调：`get_all_labels`、`get_prometheus_target`、`get_metric_metadata`、`kubernetes_count`、`traces_list_services`。

## 反馈简报（按 #616 分类）

| # | 发现 | 证据 | 类别 | 处理 |
|---|---|---|---|---|
| 1 | 真实 `Events.Timestamp` 是 numpy repr，分档与驱动解析失败 | `shift.py:210` traceback，35 题 | ① | #624，已合并（PR #626），重跑通过 |
| 2 | 主机 DeepSeek key 无效，Run 首个请求即 `MODEL_REJECTED` | `/models` 401 | ①（环境） | 用户换 key，重跑通过 |
| 3 | `traces_search` / `traces_search_by_tags` 宽窗口 `RESULT_TOO_LARGE`（S1：checkout 40 分钟 limit 15，114322/25000 token）；工具返回了 `read_evidence` 用法提示，S1 没有接着读，S5 读了一次 | 原始 ledger（未入库） | ④ | 工具按设计工作；是否在调查指导里强调「先收窄或用 `read_evidence`」由 agent 决定 |
| 4 | `get_metric_names` 用 `{__name__!~"…"}` 这种只有否定匹配的 selector，Prometheus 400 `match[] must contain at least one non-empty matcher` | ledger `source_error` | ④ | 模型参数错误，工具如实回报；可在工具说明里提一句 |
| 5 | `list_prometheus_rules` 两题都是 no_data | 回放 Prometheus 按设计不带规则（`services.py:243`「no scrape jobs, no rules」） | 预期（③ 的已知限制） | 不是缺陷；Lite 回放里规则类工具没有数据，记入全量限制说明 |

没有 ② 或 ③ 类缺陷。

### 阶段 3 决定（类别 ④，lead 2026-11-14 决定；用户常设规则：全量前只停一次）

| # | 决定 | 理由 |
|---|---|---|
| 3 | **改**：#628 给 `replay-candidate-k8s` 加一行调查指导（宽 trace 查询先收窄或用 `read_evidence` 分页），只动提示词 | 两题都撞上 `RESULT_TOO_LARGE`，S1 没有接着读；只改提示词不动 `tool_schema_revision` 与 `context_policy_revision`，A7 前一次 Run 的版本核对不受影响（手册阶段 3 版本停点） |
| 4 | **不改** | 改工具说明会变 `tool_schema_revision`，A7 的 `compare` 会拒绝；工具已如实回报 Prometheus 400 |
| 5 | **不改**，记入全量限制说明 | 回放 Prometheus 按设计不带规则，不是缺陷 |

## 未执行 / 未入库

- 两题的 `summary.json` 仍留在主机运行 checkout 的未跟踪目录里（`docs/evidence/itbench-lite-sre-lite-smoke-1114b/S{1,5}/trial-1/`），按手册第 1 节收尾再提交；首轮失败的 `lite-smoke-1114/S1` 同样保留。原始 ledger 在主机 `tmp/lab-ledgers/`，不入库。
- 费用（token × 单价）未算：单价未在当天读取。
