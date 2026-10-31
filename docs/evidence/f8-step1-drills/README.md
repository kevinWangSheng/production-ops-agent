# F8 第 1 步 PR-2（#414）：lab 抓取、两条自监控告警与两次演练（2026-10-31）

类别：集成运行（本机 colima `m1-kind` VM；kind lab 的 Prometheus v3.5.0 与 Alertmanager v0.28.1；Compose 项目 `opspilot-393`，`compose.yaml` + `compose.lab.yaml`；`fixture` 工具档；真实 DeepSeek 模型）。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。合同：[F8 决定表 r1](../../tasks/2026-10-27-m3-f8-operability-contract.md) F8-Q1、Q3、Q4、R5、R6、R8、R9。

## 本目录文件

| 文件 | 内容 |
|---|---|
| `rules-check.txt` | `kind_lab.py rules-check`：promtool `check rules`、`check config --syntax-only`，amtool `check-config`，以及 `opspilot_self="true"` 的路由测试 |
| `drill1-am-alerts-firing.json`、`drill2-am-alerts-firing.json` | 每次演练告警 firing 时的 Alertmanager `/api/v2/alerts` 原样导出 |
| `drill1-prom-firing.json`、`drill2-prom-firing.json` | 同一时刻的 Prometheus 即时查询：`up{job="opspilot"}`、`ALERTS{opspilot_self="true"}`、模型调用计数、`opspilot_queue_depth` 等 |
| `drill1-prom-range.json`、`drill2-prom-range.json` | 演练窗口的 `query_range`（15s 步长）：`ALERTS`（pending → firing → 消失）、`up`、`opspilot_model_consecutive_failures`、`opspilot_queue_depth{state="queued"}` |
| `alertmanager-notifications.txt` | 演练后 Alertmanager 自身的 `alertmanager_notifications_total`（所有集成均为 0） |

原始记录不入库（ADR-0006），放在仓库外 `~/.cache/opspilot-414-drills/`，已脱敏：不含 key、口令、令牌值。告警令牌与工作台口令只在脚本内存里用过；worker_env 与 DeepSeek key 未读取（K2）。下文出处写法：`d1:N` = `drill1/timeline.log` 第 N 行，`p1:N` = `drill1/alerts-poll.log`，`d2:N`、`p2:N` 同理，`td:N` = `teardown.log`，`up:N` = `kind-lab-up-3.log`，`up0:N` = `kind-lab-up.log`。时间均为 UTC。

## 结论与出处

| 结论 | 出处 |
|---|---|
| promtool 校验通过：3 条规则（原有 checkout 规则 + 两条新规则）；渲染后的 `prometheus.yml` 语法有效；Alertmanager 配置有效（2 个接收器）；带 `opspilot_self="true"` 的告警只路由到 `opspilot-self` | `rules-check.txt` |
| lab 配置经 `kind_lab.py up` 生效（`roll_prometheus_on_config_change` 打了新哈希，Prometheus 与 Alertmanager 都滚动了） | up:22, up:64, up:85 |
| Prometheus pod 经 `192.168.5.2` 能抓到宿主回环上的三个端口：栈起来后 `up{job="opspilot"}` 三个 role 都是 1（合同「未确认」中的可达性，现已实测） | `drill1-prom-range.json` 首个点；`drill1-prom-firing.json` |
| 抓取配置的键形：chart 0.37.8 的 `prometheus.extraScrapeConfigs` 是字符串，模板用 `tpl` 追加到 `scrape_configs` 之后（合同「未确认」中的键形）；demo chart 内置 Prometheus 的 `evaluation_interval` 与 `scrape_interval` 都是 1m | 本次读的 chart 源 `charts/prometheus/values.yaml:1258-1260`、`templates/cm.yaml:70-71`；live ConfigMap |
| **演练一 停 worker**：16:22:38 停 worker；16:22:44 发一条合成告警，intake 建事故 `df84a9af…` 与 Run `e6c48c8f…` | d1:6-10 |
| Run 停在 `queued`，无 owner；web `/metrics` 的 `opspilot_queue_depth{state="queued"}` 从 0 变成 1 | d1:13, d1:22；`drill1-prom-range.json` |
| `OpsPilotAgentAbsent{role="worker"}` 16:24:15 进入 pending，16:26:24 观察到 firing（Alertmanager `startsAt` 16:26:12.801Z），接收器 `opspilot-self` | p1:5, p1:13；`drill1-am-alerts-firing.json` |
| 16:26:43 过了 deadline（web 用 `compose.drill-run-wall.yaml` 设 `OPSPILOT_RUN_SECONDS=240`），Run 仍是 `queued`；16:27:08 打开事故页触发清扫，Run 变 `waiting_human`，事件 `run_handoff` 的 reasons = `["DEADLINE_EXCEEDED"]`，`parked: true`，`published: false`；队列深度回到 0 | d1:1-5, d1:33-51 |
| 恢复：去掉 run-wall override 重建 web，启动 worker；16:28:16 告警消失 | d1:52-66；p1:14-15 |
| **演练二 断模型出口**：worker 带 `compose.drill-egress.yaml` 重建，`ExtraHosts` = `["api.deepseek.com:127.0.0.1"]`，容器内解析结果为 `127.0.0.1` | d2:1-8 |
| 16:28:47 发告警，Run `542db16a…` 被领取；两次模型请求都是 `MODEL_UNAVAILABLE`（`model_requests_used=2`），Run 交接为 `waiting_human`，reasons = `["MODEL_UNAVAILABLE"]` | d2:10, d2:17-20, d2:40-45 |
| worker `/metrics`：`opspilot_model_calls_total{category="MODEL_UNAVAILABLE"} 2`，`opspilot_model_consecutive_failures 2` | d2:11-16 |
| `OpsPilotModelCallsFailing{role="worker"}` 16:29:28 pending，16:30:17 firing（`startsAt` 16:30:12.801Z），接收器 `opspilot-self` | p2:1-4；`drill2-am-alerts-firing.json` |
| 恢复：不带 override 重建 worker（`ExtraHosts` 为空）；人工 `resume`（operator，generation 0→1）后 Run 被重新领取，两次模型调用都成功（`category="ok"` 2，连续失败数 0）；告警 16:32:17 消失 | d2:46-57, d2:72-75；p2:5-6 |
| 恢复后的 Run 以 `INCOMPLETE_INVESTIGATION` 交接：唯一的工具调用被拒，原因 `TARGET_NOT_REGISTERED`。演练目标 `checkout-drill2` 不是 fixture 工具档登记的目标（`checkout-prod`），与 #393 第一次 Run 的现象相同 | d2:58-71 |
| 自监控告警没有发出任何通知：Alertmanager 收到 5 条 firing，所有集成的 `alertmanager_notifications_total` 都是 0（含 webhook）；工作台也没有出现关于自身告警的事故 | `alertmanager-notifications.txt` |
| 收尾：16:33:13 `compose stop`，数据卷 `opspilot-393_pgdata` 与密钥目录保留；`target_identities.json` 恢复为演练前字节（sha256 `1714ab7b…`） | td:1-13 |

## 时间线

| 时刻 | 事件 |
|---|---|
| 16:13:51 | 拿到 `~/.agent/locks/kind-lab`；`kind_lab.py health` ok；swap 剩约 0.4–1.0 GiB |
| 16:14:13 | 第一次 `kind_lab.py up` 失败：Prometheus ConfigMap 的 `alerting_rules.yml` 字段归 `kubectl-patch` 所有（2026-10-10 的手工 patch），server-side apply 冲突（up0:21） |
| 16:15:51 | 给 demo chart 的 `helm upgrade` 加 `--force-conflicts` 后 `up` 成功（第二次是 GitHub Pages EOF，与本改动无关） |
| 16:18:15–16:19:06 | `install.sh upgrade` 重建镜像并起栈；再用 `compose.lab.yaml` 重建三个运行时；Observer 接入 `kind` 网络 |
| 16:19:12 | 栈起来之前 Prometheus 已抓到三个目标 `up=0`，三条 `OpsPilotAgentAbsent`（web/worker/observer）firing 到 `opspilot-self`；栈起来后约 1 分钟消失（`~/.cache/opspilot-414-drills/baseline/`）。这是计划外的观察，不算演练 |
| 16:22:38–16:28:16 | 演练一 |
| 16:28:36–16:32:17 | 演练二 |
| 16:33:13 | 停栈，恢复目标文件；16:33:28 释放锁 |

检测延迟（由配置决定，非产品指标）：worker 停止到 firing 约 3 分 46 秒，组成为 15s 抓取 + 1m 规则评估 + `for: 2m`。断出口到 firing 约 1 分 30 秒（`for: 1m`）。

## 偏离与局限

- **合同外的 lab 工具改动**：`kind_lab.py up` 的 demo chart `helm upgrade` 加了 `--force-conflicts`。不加就无法改规则文件：2026-10-10 的一次手工 patch 让 `kubectl-patch` 拥有了该字段。副作用：故障钩子 `kubectl patch` 的 `flagd-config` 字段也会被 `up` 收回，因此在故障注入期间运行 `up`，会把 flagd 恢复成 chart 的值。
- 新增 `kind_lab.py rules-check`（promtool/amtool 跑在 lab pod 里，因为宿主没有这两个二进制）。另加了 lab 专用的 `deploy/compose/compose.drill-run-wall.yaml`（F8-Q4 的缩短墙钟），`install.sh` 不引用它。
- 为了让两次演练各自建事故、不被关联进 #393 留下的开放事故，在操作者文件 `target_identities.json` 里临时加了 `checkout-drill1`/`checkout-drill2` 两条，演练后已恢复原字节。库里这两个事故仍是 `waiting_human`、开放，没有人工关闭。
- 告警消失的方式：演练二里，`opspilot_model_consecutive_failures` 是在 worker 重建（进程内计数清零）时回到 0 的，不是因为一次成功调用；之后的 `resume` 才产生两次 `ok`。
- 合成告警由脚本 POST 到工作台 intake（同 #393），不是 Alertmanager 投递的。投递路径不在本步范围内；自监控告警刻意不投给工作台。
- 费用：被拦截的两次调用连接在本机就被拒绝，不会产生供应商费用。人工 `resume` 产生了 2 次真实 DeepSeek 调用（`budget_spent 2`）。Agent 看不到余额（K2），由用户按余额差对账。
- 没有 trace 链接：产品 Compose 不设 `OPSPILOT_TRACE`，产品 trace 属 #243。
- 镜像 revision 标签是 `696fb2b2`；构建时工作区里还有未提交的 `kind_lab.py` 改动，产品代码与 main `6845ffc7` 相同。
- lab 状态：Prometheus 与 Alertmanager 留在本分支提交的配置上。停栈后三个目标 `up=0`，`OpsPilotAgentAbsent` 会一直 firing 到 `opspilot-self`（不通知、不调用任何东西）；原有 checkout 规则与 webhook 路由不变。
- 未做：独立审查；全量 `make check`/pytest（lab 窗口内按规则不跑）；F8 `passes` 保持 false。
