# #245 PR-1 有界真实运行：改目标后新 Run 对新目标查询（2026-10-27）

- 目的：PR #378（`rebind_target`）按 ADR-0006 附一次有界真实 Run：事故挂在目标 A → 经工作台表单改目标到登记表里的 B → 追问 → 新 Run 对 B 的只读查询成功、发布结论。
- 运行代码：`9358d25edbc5227be69e425ac4146a21e247f12c`（PR #378 当时的 HEAD）。
- 冻结摘要：[summary.json](summary.json)，14 项检查全部通过（`all_pass: true`）。判据由 [collect.py](collect.py) 从产品行与运行时保存的页面只读算出。原始 ledger 不入库，放在本机 `tmp/f245-live/ledger.json`（sha256 `8530cd89…2453`，203,023 B，含页面 HTML 和每次控制前后的行）。
- 操作工具：[ctl.py](ctl.py) 以工作台操作员（Basic Auth `f245-lab`，凭据读文件、不打印）向 `POST /intake/ui`、`POST /incidents/{id}/control` 提交与页面表单相同的字段（带 Origin），每步后保存页面。全程没有直接写 SQL。

## 环境

- PostgreSQL 17：分配给本线的临时实例 55701 上新建的库 `f245_live`，迁移到 `0022_incident_association`；web 和 worker 用非 owner 登录角色（`opspilot_web_f245 ∈ opspilot_web`、`opspilot_worker_f245 ∈ opspilot_worker`）。未启 Observer：本场景只需要观察会话的授权与撤销，二者都在 web 事务里完成。
- kind 集群 `opspilot-m1`：31 个 pod 全部 Running。开跑前 swap 已用 7.9 / 9.2 GB；frontend span 速率 1.77/s，checkout 0.19/s（10 分钟窗），无需重启 otel-collector。没有注入故障：本场景检验的是改目标语义和新目标的查询，不是排障质量。
- 工具 profile `otel-demo`。web 绑定 `127.0.0.1:18245`，不收 Alertmanager 投递。worker 设 `OPSPILOT_TRACE=lab`，LangSmith 项目为 `opspilot-lab-f12-245-rebind-20261027`（longlived）。
- 登记表（`tmp/f245-live/targets.json`）：
  - A = `checkout-misbound`：身份字段与实验环境的 checkout 相同，只有 `resource_uid` 不同，用来模拟「挂错登记项」；
  - B = `m0-otel-20260909`：实验环境唯一真实可查的目标。
- 场景限制：`otel-demo` 工具 profile 的网关只登记了 `m0-otel-20260909` 一个目标，它的 `QueryScope.target_ids` 是固定的（`opspilot/tools/otel_demo.py`）。所以只能取 B = 实验目标、A = 无真实后端的登记项，A 上的首个 Run 在 worker 启动前就被改目标取消，从未执行。这验证的是「改到 B 后能对 B 查询」，不是「A、B 两个真实后端之间切换」。

## 步骤（时间为 UTC）

| 时间 | 动作 → 结果 |
|---|---|
| 12:39:01 | `POST /intake/ui`，`target_id=checkout-misbound` → 事故 `6f4a57f7`、Run `19445791`（queued，输入 `bound_target_id=checkout-misbound`）。此时 worker 未启动 |
| 12:39:07 | `register_remediation`（revision `lab-20261027`）→ 代际 1，lifecycle `observing_recovery`，A 上的观察会话 authorized；页面出现 `rebind_target` 表单 |
| 12:39:14 | `rebind_target`，`to_target=m0-otel-20260909`，附说明文字 → 200、代际 2、响应里没有 `run_id`。Run `19445791` cancelled，会话 `revoked / authority_revoked`（结束记录 `lifecycle_before=observing_recovery`），lifecycle `open`，事故目标 = B。审计行：操作员 `f245-lab`，`from_target=checkout-misbound`，`to_target=m0-otel-20260909`。页面显示「Current target: m0-otel-20260909」和「Rebound from checkout-misbound to m0-otel-20260909 at …」 |
| 12:39:23 | `follow_up`（「在纠正后的目标上查 checkout」）→ 200、代际 3、新 Run `ec062969`。随后启动 worker |
| 12:39:24–12:42:14 | worker 领取 `ec062969`。输入：`bound_target_id` 与 `scope_facts.target_ids` 都是 `m0-otel-20260909`；`question` 就是接收时的原问题（没有续接的交接说明），是 fresh 输入。共 10 次模型请求、9 个工具步骤、21 条证据：17 条 `ok`、4 条 `no_data`，全部是 `target_id=m0-otel-20260909`。12:42:14 发布结论 |
| 12:42:24–12:43:01 | 会话撤销后事故成为复盘生成候选（已有行为，#255）：一次生成尝试，1 次模型请求，`superseded / WATERMARK_MOVED`。12:43:40 停 worker 和 web |

## 结果

`summary.json` 的 14 项检查全部通过，覆盖：

- A 上的接收与授权观察；
- 改目标的 200 / 代际 +1 / 不开 Run；
- 改目标后目标为 B、lifecycle open、旧 Run cancelled、会话已撤销；
- 结束记录；
- 审计行；
- 页面；
- 新 Run 绑定 B、输入是 fresh；
- 对 B 的查询有数据；
- 新 Run 完成并发布；
- 旧 Run 从未执行；
- 新 Run 有 LangSmith 根 trace。

如实记录：

- 结论摘要：PlaceOrder 在授权的 24 小时窗口内没有出现错误；唯一的异常是最近一小时 p95 延迟升高。这与没有注入故障相符。
- Jaeger 对 checkout 的 trace 查询没有返回数据，第一次查询 `SOURCE_UNAVAILABLE`，报告里写明了 trace 证据缺失。指标查询正常。这不影响本场景的判据（只要求对 B 的只读查询成功），记在这里供后续实验参考。
- 这是 kind 实验环境上的一次有界运行，不是生产证明。

## trace

| 根 run | 关联 | 链接 |
|---|---|---|
| `opspilot.run` | 改目标后的新 Run `ec062969` | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7037e7fc-a4f9-466c-9f3b-612b77c10e7d/r/00000000-0000-0000-a998-c462d3287004?poll=true) |
| `opspilot.postmortem_generation` | 复盘生成尝试（superseded） | [trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/7037e7fc-a4f9-466c-9f3b-612b77c10e7d/r/00000000-0000-0000-1101-eeb633d167d2?poll=true) |

## 费用

DeepSeek 余额 90.14 → 90.04 CNY，共 0.10 CNY（充值余额），覆盖 1 个调查 Run（10 次模型请求）和 1 次复盘生成尝试（1 次请求）。没有重跑。

## 收尾

web 和 worker 用 SIGTERM 按 PID 停止（PID 为本任务启动、已核对命令行）；库 `f245_live` 保留在 55701 实例上，未删。集群未改动。
