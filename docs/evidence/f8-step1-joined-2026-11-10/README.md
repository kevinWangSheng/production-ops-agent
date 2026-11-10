# F8 第 1 步：同一事故、同一版本的十个面并排记录（2026-11-10）

类别：集成运行。本机 colima `m1-kind` VM，Compose 项目 `opspilot-393`，`fixture` 工具档，真实 DeepSeek，真实 LangSmith SaaS（项目 `opspilot-lab-428`），`OPSPILOT_TRACE=outbox`。一次有界 Run，2 次模型调用。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。F8 `passes` 未翻。

补的缺口：此前第 1 步各面分别在不同 Run 上有证据（见 F8 逐步核对记录），没有一次 Run 同时把这些面按同一事故和同一版本串起来。本次只跑一个 Run，把每个面在同一 `incident_id` 与镜像 revision 下记录。

原始记录不入库（ADR-0006），在仓库外 `~/.cache/opspilot-f8s1-run/`（`timeline.log`、`upgrade.out`、每 2 秒一次的三个运行时 `/metrics` 采样、日志）。回读脚本按路径读取 `worker_env`，key 只在该进程内使用，没有打印或复制。

## 构建与 Run

- HEAD `9b208cd1`（origin/main，版本 0.1.1），`install.sh upgrade` 构建 `opspilot:local`，镜像 `sha256:ad770d78…`。web、worker、observer 的 `opspilot_build_info` revision 都是该 HEAD；schema 仍为 `0024_trace_outbox`。
- 合成告警 13:42:51Z（UTC，下同）POST 到 intake，目标 `checkout-prod`（fixture 唯一登记的目标）。用了新的 fingerprint，所以 10-31 与 11-01 留下的两个 checkout 开放事故不是关联候选，告警新开了事故。
- 事故 `6d857d18-3003-5b62-b82d-f02cea7abbe5`，Run `2e4ef59a-6350-5ca0-9a60-e7048418107b`：13:42:52 领取，第 1 轮调用 `metrics_range_query` 成功，第 2 轮 13:43:32 结束，`INCOMPLETE_INVESTIGATION` 交接，停在 `waiting_human`。预算 2/100，工具操作 1 次。
- [LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/1a1a92bb-976f-4dd7-8a68-283c492b3b61/r/00000000-0000-0000-83dd-1f994a25ad49?poll=true)

## 各面对照

「按事故」指该面的记录本身带 `incident_id`；「按版本」指带镜像 revision。指标按合同不带事故 ID 标签，和事故的对应靠同一 revision 加时间窗：这个 Run 运行期间（13:42:52–13:43:32）worker 上只有它一个在途尝试（`opspilot_in_flight_attempts` 恰好在这段时间为 1）。

| 面 | 按事故 | 按版本 | 本次的值 | 出处 |
|---|---|---|---|---|
| 健康 | 否 | 是（build_info） | 三个运行时 `/healthz`、`/readyz` 在 Run 前后都是 200 | summary `surfaces.health` |
| trace | 是 | 是 | LangSmith 按 `incident_id` 筛到 7 个 run，加 `image_revision` 筛到 4 个；6 行 outbox 全部找到，重复 0、缺失 0 | `readback.json` |
| 指标 | 否（时间窗） | 是 | `model_calls_total{category="ok"}` 13:42:55 变 1、13:43:34 变 2；outbox pending 峰值 2，13:43:01 回 0；丢弃全为 0 | summary `surfaces.metrics` |
| 结构化日志 | 是 | 是 | worker 13:43:32.659Z 一行 JSON，`kv.incident` = 本事故，`reason=INCOMPLETE_INVESTIGATION`，`revision` = HEAD；窗口内三个运行时每行都带 revision | 仓库外 `logs-*.log` |
| 队列深度 | 否（时间窗） | 是 | `queued` 与 `running_lapsed` 每次采样都是 0。Run 在队列里只停了 36 ms（intake 52.010 → 领取 52.046），短于 2 秒采样间隔，所以采样里看不到它 | summary `surfaces.queue_depth` |
| provider 状态 | 指标否；trace 是 | 是 | `last_model_call_category{category="ok"}`，连续失败 0；trace 里 `ls_provider` deepseek ×3、`ls_model_name` deepseek-flash ×2 | 同上、`readback.json` |
| 工具错误 | 是 | 是 | 本 Run 唯一一次工具调用成功：`tool_status_class` ok ×1，`tool_reason` 为空 | `readback.json`、`payloads.json` |
| 卡住的 Run | 指标否；PG 是 | 是 | `running_lapsed` 全程 0；PG 中本 Run 从 running 直接到 waiting_human，租约已清空，没有租约过期还停在 running 的行 | summary `surfaces.stuck_runs` |
| token | 是 | 是 | LangSmith prompt 4803、completion 6843、合计 11646；PG `opspilot_steps.usage` 逐项相同（另有 reasoning 5326，缓存命中 3456 / 未命中 1347） | `readback.json`、`steps-usage.json` |
| 成本 | 是 | 是 | LangSmith 估算 $0.00482625；token×单价 CNY 0.0288（闲时） | summary `surfaces.cost` |

## 成本口径

- 单价：DeepSeek 官方价格页（deepseek-flash），2026-11-10 读取，与 soak 11-07 读取的一致。闲时每百万 token：缓存命中 0.02、未命中 1、输出 4 元；高峰（北京时间周一至周五 9:00–12:00、14:00–18:00）翻倍。
- 本 Run 在北京时间周二 21:42–21:43，属闲时：3456×0.02 + 1347×1 + 6843×4（每百万）= CNY 0.0288。
- 不用余额差与控制台：余额是账户级，多个 key 共用；控制台只按天和账户汇总。LangSmith 的价格条目内容本次没有读取。

## 证明不了什么

- 没有出现工具错误：fixture 只有一个合法表达式，模型用对了。工具错误码能按事故在 LangSmith 筛到，证据在 [243-trace-outbox](../243-trace-outbox/README.md)（`TARGET_NOT_REGISTERED`，另一个 Run）。要在同一个 Run 上同时得到成功调用和错误码，需要改 fixture 或注入故障，本次没做。
- 卡住的 Run 只看到 0：本 Run 没有卡住，`running_lapsed` 能变成非零的证据在 [f8-step1-drills](../f8-step1-drills/README.md)。
- 队列深度没采到非零值，原因见上表。
- 指标、健康和队列深度只能按版本加时间窗对上事故，这是合同的设计（事故 ID 不作指标标签）；并发 Run 多时时间窗会重叠，需要回到 trace 或 PG 按事故查。
- `run_state` 行与根 span 不带镜像字段（PR-1 已知形状），所以 LangSmith 按事故加 revision 只筛到 4 个 run（3 个模型步、1 个工具结果），按事故能筛到全部 7 个。
- web 的 intake 访问日志不带事故 ID；带事故的日志行只有 worker 的尝试结果。
- 本机 swap 开窗前约 1 GB 余量；本次没有延迟或恢复观察，不影响任何判定。

## 收尾

web、worker、observer 已停。postgres 在本任务开始前就在运行，保留运行状态、数据卷与密钥目录。`target_identities.json` 未改（sha256 `1714ab7b…`）。`opspilot:local` 现在是 `9b208cd1` 的构建。本任务没有新建凭据文件。
