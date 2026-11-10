# F8 第 1 步：卡住的 Run 按事故和版本关联（2026-11-10）

类别：集成运行，也是一次故障注入。本机 colima `m1-kind` VM，Compose 项目 `opspilot-393`，只起 web 和 worker，`fixture` 工具档，真实 DeepSeek，`OPSPILOT_TRACE=outbox`。一次有界 Run，2 次模型调用。不是生产证明。冻结摘要见 [summary.json](summary.json)。F8 `passes` 未翻。

补的缺口：此前没有任何证据里 `running_lapsed` 出现过非零值。[f8-step1-drills](../f8-step1-drills/README.md) 两次演练的 Prometheus 导出（`drill1-prom-firing.json`、`drill2-prom-firing.json`）里都是 0，[joined run](../f8-step1-joined-2026-11-10/README.md) 也全程是 0。soak `stuck_work` 里唯一非零的 `over_wall_clock_runs`（1 个）统计的是超过墙钟的 Run，不是租约过期。

原始记录不入库（ADR-0006），放在仓库外 `~/.cache/opspilot-f8s1-stuck/`：`timeline.log`、每 3 秒一次的 web/worker `/metrics` 采样（`metrics-samples*.txt`）、`builds.jsonl`、日志。脚本没有读 worker_env；告警令牌只在脚本内存里用过。

## 信号是什么

合同里卡住的 Run 只通过 `opspilot_queue_depth{state="running_lapsed"}` 暴露（[合同](../../tasks/2026-10-27-m3-f8-operability-contract.md)第 72 行），由 web `/metrics` 给出。计数条件在 `opspilot/web/__main__.py` 的 `_QUEUE_DEPTH_SQL`，和 worker 领取用的 `claimable_incidents` 一样：事故开放、模式为自动、当前 Run 是 `running`、没过 deadline，并且 `lease_until` 为空或已过。租约 420 秒（`opspilot/worker.py` 的 `DEFAULT_LEASE_SECONDS`），worker 每次调用工具和预留预算前续租。所以要让它变成非零，得在 Run 运行中途杀掉 worker，并且至少 7 分钟不让任何 worker 接手。

## 过程（UTC）

| 时刻 | 事件 | 出处 |
|---|---|---|
| 15:02:20 | web 和 worker 的 `opspilot_build_info` revision 都是 `9b208cd1`，镜像 `sha256:ad770d78…`（该 revision 到 HEAD `ee6fc62c` 之间只改了文档和 ROADMAP） | `builds.jsonl` pre |
| 15:02:40.99 | 合成告警新开事故 `75ea6248-3da4-502f-80db-823106e6d2b9`，Run `7f1e5f5d-7799-5f50-8bf2-6640f60c7ef3` | `timeline.log` |
| 15:02:41.68 | 第 1 次领取（epoch 1） | 事件 seq 2 |
| 15:02:43.86 | 第 1 轮模型调用后，`metrics_range_query` 结果提交 | 事件 seq 3–4 |
| 15:02:44.44 | `install.sh compose kill worker`，容器退出码 137 | `timeline.log`、`docker inspect` |
| 15:02:59 | 租约还有效：`lease_until` 15:09:43.86，`running_lapsed` 0 | `timeline.log` |
| 15:09:46.589 | 最后一次 0 | `metrics-samples.txt` |
| **15:09:49.634** | **第一次 `running_lapsed` = 1**，同一次采样里 web 的 build_info revision = `9b208cd1` | `metrics-samples.txt` |
| 16:36:11–12 | 仍是 1；PG 行：`running`、epoch 1、owner 仍是被杀的 worker、`lease_until` 15:09:43.86 已过、deadline 17:02:40.87 未到 | `timeline.log` |
| 16:36:23 | 启动 worker | `timeline.log` |
| 16:36:25.33 | 第 2 次领取（epoch 2），`running_lapsed` 回到 0 | 事件 seq 5、`metrics-samples-resume.txt` |
| 16:36:41.41 | 接着第 2 轮继续（第 1 轮没有重做），`INCOMPLETE_INVESTIGATION` 交接，停在 `waiting_human` | 事件 seq 6–7 |

前一个 worker 停了 93 分钟，比计划长：采样脚本 15:22:57 结束后，执行会话有一段没动，16:36 才用手动读数接上。这段时间里 PG 行没变，读数仍是 1。web 的 400 次采样里 build_info revision 都是 `9b208cd1`；共 261 次 `running_lapsed` = 1（第一个文件 259 次，恢复文件 2 次）。

## 怎么对上事故和版本

| 面 | 按事故 | 按版本 | 本次 |
|---|---|---|---|
| 指标 `running_lapsed` | 否（合同不让事故 ID 当标签） | 是，同一次抓取里的 `opspilot_build_info` | 15:09:49.634 到 16:36:19 一直是 1，期间只有这一个 Run 处于 `running` |
| PG `opspilot_runs` | 是（`incident_id`、`run_id`） | Run 行只有 prompt/tool/context 版本，没有镜像 revision | 租约过期但仍是 `running` 的就是这一行 |
| PG trace outbox | 是，6 行都带 `incident_id` | 4 行带 `image_revision` = `9b208cd1`（`run_state` 行不带，已知形状） | worker 停着时有 3 行 `pending`，恢复后 6 行都是 `delivered` |
| worker 结构化日志 | 是，`kv.incident` | 是，`revision` | 16:36:41.407Z `status=handed_off reason=INCOMPLETE_INVESTIGATION` |

## 证明不了什么

- 指标只能靠版本和时间窗对上事故；同时卡住好几个 Run 时，计数只告诉你有几个，要按事故区分还得回 PG 查（领取条件同上）。
- 没有做 LangSmith 回读。trace 已送达只看 PG outbox 的 `state=delivered`，没有在 LangSmith 里按事故筛选核对，也没有 trace 链接（未确认）。
- 15:22:57–16:36:01 之间没有指标采样。
- 没起 observer；本次和 Observer 无关。
- 只有一个 worker。多 worker 时会被别的 worker 在租约过期后很快接走，`running_lapsed` 可能只非零几秒。

## 收尾

web 和 worker 已停（`install.sh compose stop web worker`）。postgres 本来就在运行，状态、数据卷和密钥目录保持不变。没有新建凭据文件。
