# #393 安装后的栈跑通一次真实调查（2026-10-31）

类别：集成运行（本机 colima `m1-kind` VM，Compose 安装，真实 DeepSeek 模型，`fixture` 工具档）。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。

原始记录不入库（ADR-0006），放在仓库外 `~/.cache/opspilot-393-run/`，均已脱敏（不含 key、口令、令牌值）：

- `run-a.log`：第一阶段 09:09:55–09:10:29Z 的命令与输出。
- `run-b.log`：第二阶段带 agent 的 `install.sh upgrade`，12:53:50–12:53:57Z。
- `run-b2.log`：13:00–13:20Z 窗口，运行后汇编。内容：fault-log.jsonl 原文与注入/恢复命令（控制台输出为会话转录，已标注）、webhook 响应、轮询输出、三个容器的 `docker logs --timestamps`、在 postgres 容器内以 owner 角色执行的只读查询（SQL 与输出）、工作台页面文本，以及停机后容器的 `docker inspect`。
- `pages/incident-606d9341-run-796fd3a1.txt`、`pages/incident-9869b041-run-5732baeb.txt`：两次 Run 的工作台页面文本，只含 run-state 与报告部分，已去掉表单。

下文 `b2:N` 指 `run-b2.log` 第 N 行，`a:N`、`b:N` 同理。

## 结论与出处

| 结论 | 出处 |
|---|---|
| 第一阶段：安装完成；Observer 用自己的账号查询 `http://opspilot-m1-control-plane:30090` 返回 200；未放 key 时 `OPSPILOT_START_AGENT=1 upgrade` 被预检拒绝，不做改动 | a:2, a:146, a:175, a:179 |
| 镜像 `sha256:7b311f88…`，版本 0.1.0，revision `0aad093c`；三个运行时用同一镜像 | a:190, b2:422-424 |
| 第二阶段升级顺序：stopping runtimes → postgres → migrate（`schema unchanged: 0023_retention_raw_cleanup`）→ init-roles（4 个登录都在 head）→ starting runtimes: web worker observer | b:68, b:73, b:81, b:86, b:91-94, b:99, b:100 |
| 三个运行时都 healthy；`worker started`、`observer started` | b:112-114, b2:431-432 |
| 隔离：worker 只挂 `worker_pgpass`、`worker_env`；observer 只挂 `observer_pgpass`、`observer_env`，并接入 `kind` 网络；web 挂它的三个 secret 加 config `target_identities.json` | b2:422-424 |
| worker 的 `OPSPILOT_WORKER_CAPACITY=2`、`OPSPILOT_POOL_MAX_SIZE=4`、`OPSPILOT_TOOL_PROFILE=fixture` | b2:426-429 |
| lab 故障：13:00:17Z 注入 `paymentFailure`（已校验），13:15:19Z 恢复（已校验）。12:55 那次注入在发告警前已恢复 | b2:13-14, b2:20-21 |
| 注入期间 checkout 与 payment 出现 ERROR span（5 分钟内约 22.6 / 11.3，经 Observer 账号查询，控制台转录） | b2:23 |
| 第一次告警 13:03:40Z：intake `created`，incident `9869b041…`，Run `5732baeb…` | b2:27, b2:63 |
| 第一次 Run 的两次工具调用都被拒 `TARGET_NOT_REGISTERED`，交接 `INCOMPLETE_INVESTIGATION`；Run 停在 `waiting_human` | b2:177-178, b2:59, b2:174, b2:186, b2:35 |
| 把目标身份键改成 `checkout-prod` 后重启 web（13:14:21–13:14:22Z） | b2:28, b2:119-125 |
| 第二次告警 13:14:31Z：intake `created`，incident `606d9341…`，Run `796fd3a1…` | b2:30, b2:127 |
| 第二次 Run `completed`；worker `status=published`；报告 assessment `completed`、conclusion `partial`，content sha256 `22767fd0…` | b2:47, b2:60, b2:175, b2:187 |
| 报告在工作台显示：页面 `id="run-state"` = completed，有 `report-status`，无 `report-missing` / `handoff-report`，显示 "Assessment completed" | b2:195-202；`pages/incident-606d9341-run-796fd3a1.txt` 第 1-8 行 |
| Run 结束后，系统自动授权恢复观察（`incident_recovery`，13:15:22Z）。Observer 取了 2 个样本（13:16:54Z、13:18:25Z），两次都是 `degraded`，样本窗口覆盖故障期 | b2:189, b2:191-192, b2:136-137 |
| 13:19:06Z 停栈：worker、observer、web 收到 signal 15，正常退出 | b2:61-62, b2:132, b2:138 |

报告摘录（`pages/incident-606d9341-run-796fd3a1.txt`）：模型执行了唯一授权的 `rate(http_errors[5m])`，结果 ok。它指出 fixture 样本的 `data_as_of` 是 2026-09-14，不覆盖告警窗口，因此不做归因。重复查询被 `DUPLICATE_TOOL_CALL` 拒绝（b2:182）；traces、k8s、logs、changes 标为 not_integrated。

注意：`fixture` 档的调查读的是固定 fixture 视图，看不到 lab 里注入的故障。真实故障只出现在 Observer 读的 Prometheus 里（上面的 `degraded` 样本），这与 #393 合同的 fixture profile 一致。工作台页面是在 13:15:10Z 抓的，那时恢复观察还没被授权，所以页面写着 "No recovery observation authorized"（b2:200）。授权发生在 12 秒后，以数据库为准。

## 未做 / 局限

- LangSmith trace：无。产品 compose 没有 `OPSPILOT_TRACE`，产品 trace 属 #243（outbox）范围，本次没有 trace 链接。
- 费用：Agent 未观察 DeepSeek 前后余额（K2，Agent 不接触 key），由用户对账。两次 Run 各记 3 spent / 1 unknown of 100（b2:174-175）。
- opt-in live 测试（`OPSPILOT_393_LIVE=1`）未运行：两份测试都需要把 DeepSeek key 放进 Agent 进程或复制它，与 K2 冲突。
- 恢复观察没有结束：两个样本都是 `degraded`，13:19Z 停栈时会话仍是 `authorized`（b2:189）。本次不证明恢复判定。
- 第一次 Run 的 incident `9869b041…` 留在 `waiting_human`，没有人工关闭（b2:171, b2:174）。
- 故障注入与恢复的控制台输出、注入期间的 span 数字是会话转录；注入与恢复本身的机器记录是 fault-log.jsonl（b2:13-21）。
