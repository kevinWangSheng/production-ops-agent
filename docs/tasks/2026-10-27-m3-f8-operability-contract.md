# F8 第 1 实施步骤合同（r1）：健康端点 + 外部探测 + 两条告警 + 两次演练

- 预审：全新上下文 Agent（Fable 5.1），只读，基线 origin/main `62e3cde2`，2026-10-31；全文在仓库外 `~/.agent/handoffs/opspilot-prereview-386-f8-step1-2026-10-31.md`（下称「预审」，行号指该文件）。
- 裁决（r1，用户 2026-10-31）：F8-Q1–Q4 全部按推荐 (a)；可逆默认 F8-R1–R11 照预审。记录见 `~/.agent/handoffs/opspilot-rulings-2026-10-31.md` §1。
- 写入时核对：main `cefa56c1` 与 `62e3cde2` 之间只改了 3 个文档文件，预审引用的代码与配置行号不受影响；抽查 `client.py:39`、`compose.yaml:102-106`、`test_392_compose_static_impl.py:26`、`loop.py:1320-1324`、`test_f7_step1_entry_points.py:345-351`、`incidents.py:207-211`、`alertmanager-values.yaml:33-56` 均与预审一致。预审对 `scripts/kind_lab/values.yaml` 的行号有偏差（非漂移，预审本身写错）：`prometheus:` 在 :105（预审写 :93），`serverFiles.alerting_rules.yml` 在 :169-200、现有规则 `CheckoutPlaceOrderErrorRatioHigh` 在 :182（预审写 :152-182）；内容（`for: 2m`、labels 含 `cluster`/`opspilot_integration_id`）一致。
- 全量探针（预审第 4 节）：草稿把 `/healthz` 挂在 web 路由表上，`tests/acceptance` 1 失败 / 2958 通过 / 302 跳过，唯一失败 `test_f7_step1_entry_points.py::test_s1b_web_routes_equal_the_classified_allow_list`（`Extra items in the left set: ('GET', '/healthz')`）。这正是 Q2 不选 (b) 的依据；按 (a) 独立监听则 `app.routes` 不变。

## 需用户裁决（r1 已裁决）

| 编号 | 问题 | 裁决要点 | 影响面 |
|---|---|---|---|
| F8-Q1 | 本步范围与 `passes` | (a) 三进程 `/healthz` `/readyz` `/metrics` + lab Prometheus 抓取 + 两条规则 + 停 worker / 断模型出口两次演练证据；#243 outbox、结构化日志、Observer span（#159）不在本步；F8 `passes` 保持 false。字段对照表见下 | 任务拆分；验收判定 |
| F8-Q2 | 健康监听放哪、暴露到哪 | (a) 三进程各起独立内网监听（容器内 `0.0.0.0:9464`，`OPSPILOT_HEALTH_BIND`），无认证、明文 HTTP、只回固定键 + 版本；不挂 web 8080 路由表；只在 lab/开发 override 发布到宿主 `127.0.0.1`（web 19464 / worker 19465 / observer 19466），默认 `compose.yaml` 不发布；跨主机接线随 F9 A4 另做预审。F7 入口枚举由验收作者收紧 | 网络边界、无认证面、F7 验收 |
| F8-Q3 | 关于 OpsPilot 自身的告警投给谁 | (a) 规则打 `opspilot_self="true"`，Alertmanager 子路由到新接收器 `opspilot-self`（不调用 OpsPilot、不持 token，lab 里为空接收器）；「人工交接」证据 = Alertmanager `/api/v2/alerts` 与 Prometheus `ALERTS` 的 firing 记录 + 工作台 Run 交接状态；不新增外发通知（PRODUCT-CONSTRAINTS:16） | 外部检测 → 人工交接的证据形状 |
| F8-Q4 | 停 worker 时 Run 怎么算交接给人 | (a) 不改产品：证据 = 告警 firing + Run 停在 `queued` 且 `queue_depth` 上升 + 用 `OPSPILOT_RUN_SECONDS` 缩短墙钟后打开事故页触发清扫得到 `DEADLINE_EXCEEDED`（或 worker 回归后首轮清扫）。自动清扫（选项 b）不做，不另开 issue | 恢复路径；F2 已验收行为 |

## 可逆默认（照预审，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| F8-R1 | 共用小模块 `opspilot/health.py`：stdlib `ThreadingHTTPServer` 守护线程，只接 GET 三路由，其余 404，响应 ≤ 4 KiB；空值关闭 | 不给 worker/observer 引入 uvicorn/fastapi |
| F8-R2 | 指标名与队列口径见下方代码块；复用 `incidents.py:207-211` 与 `observation/store.py:1866` 的 SQL 口径；不按 Run/事故打标签 | 不另造定义；无界基数 |
| F8-R3 | 在 `worker_main.slot_runner` 用计数包装器包住 `DeepSeekClient.complete`，复盘线程的客户端同样包装；不改 `loop.py` | 不触碰调查 loop |
| F8-R4 | `/readyz`：web 查 `alembic_version` = head 且池可用；worker/observer 用「上次成功轮询 ≤ 3×poll 间隔」+ 未收到 stop，不查库；不满足 503，body 给原因码 | worker 池 4 = 2 槽 + 轮询 + 复盘，无余量（`worker_main.py:498-507`） |
| F8-R5 | lab `values.yaml` 加 scrape job `opspilot`（见下），靠 `roll_prometheus_on_config_change`（`kind_lab.py:637`）生效 | 与 Alertmanager 到宿主的路径一致（M1-04 E14） |
| F8-R6 | 两条规则见下，promtool 校验（沿 M1-04 R11） | 阈值 2 对齐 loop 重试上限（`loop.py:1320-1324`）；`for` 对齐现有规则 |
| F8-R7 | web healthcheck 改为请求 `127.0.0.1:9464/readyz`，worker/observer 加同样 healthcheck；端口发布只在 `deploy/compose/compose.lab.yaml` | 默认文件保持「只有 web 发布」（`test_392_compose_static_impl.py:26` 相应调整） |
| F8-R8 | 断出口：override 给 worker 加 `extra_hosts`（见下）后 `up -d worker`；期望两次 `MODEL_UNAVAILABLE`、Run `waiting_human`、`opspilot_model_consecutive_failures=2`、告警 firing；恢复 = 去掉 override 重启，人工 `resume`/`new_run` | 端点写死（`client.py:39`）；断网会连 PG 一起断 |
| F8-R9 | 证据 `docs/evidence/f8-step1-drills/README.md` + 冻结摘要：每次演练一份 Alertmanager API 导出、Prometheus 查询 JSON、Run 状态与交接事件、时间线；`OPSPILOT_TRACE=lab` 时附 trace 链接 | ADR-0006 |
| F8-R10 | 结构化日志在本步之后单独小 PR：JSON 行，固定字段 `ts level logger msg` + 现有 kv，不新增内容字段 | 不改行为；「不记 provider 文本」独立复核 |
| F8-R11 | 默认文件不发布新端口，`install.sh upgrade` 不需新密钥，`install.env` 不加键 | 升级路径零变更 |

逐字节细节（原文逐字摘自预审「推荐/默认」列，行号为预审文件行号）：

```text
# 预审 :95（F8-Q2，选项 (a) 节选）
(a) 三进程各起一个独立内网监听（默认容器内 `0.0.0.0:9464`，环境变量 `OPSPILOT_HEALTH_BIND`），无认证、明文 HTTP、只回三项 + 版本；**不**挂在 web 的 8080 路由表上；Compose 只在 lab/开发 override 里把它们发布到宿主 `127.0.0.1`（web 19464 / worker 19465 / observer 19466），默认 compose.yaml 不发布
```

```text
# 预审 :103（F8-R1）
stdlib `http.server.ThreadingHTTPServer` 守护线程，`OPSPILOT_HEALTH_BIND` 默认 `0.0.0.0:9464`（容器）/ `127.0.0.1:9464`（裸进程）；空值关闭；三进程同一个小模块 `opspilot/health.py`，只接受 GET `/healthz` `/readyz` `/metrics`，其他 404；响应 ≤ 4 KiB
```

```text
# 预审 :104（F8-R2）
`opspilot_build_info{version,revision} 1`；`opspilot_schema_at_head`（0/1）；`opspilot_queue_depth{state="queued"|"running_lapsed"}`（web 查 incidents.py:207 口径；observer 查 store.py:1866 口径，名 `opspilot_observation_due`）；`opspilot_last_poll_age_seconds`（worker/observer）；`opspilot_in_flight_attempts`（worker）
```

```text
# 预审 :105（F8-R3）
在 `worker_main.slot_runner` 用一个计数包装器包住 `DeepSeekClient.complete`（成功→`ok`，`ModelError.code`→该码，超时类归 `MODEL_UNAVAILABLE` 已由客户端归并），写进程内快照：`opspilot_model_calls_total{category}`、`opspilot_model_consecutive_failures`、`opspilot_last_model_call_category{category}`
```

```text
# 预审 :107（F8-R5）
`values.yaml` `prometheus.extraScrapeConfigs` 加 job `opspilot`，static targets `192.168.5.2:19464/19465/19466`，标签 `role=web|worker|observer`，`scrape_interval: 15s`；靠 `roll_prometheus_on_config_change` 生效
```

```text
# 预审 :108（F8-R6）
`OpsPilotAgentAbsent`: `up{job="opspilot"} == 0 or absent(up{job="opspilot",role="worker"})` `for: 2m`，labels `severity=critical, opspilot_self="true"`；`OpsPilotModelCallsFailing`: `opspilot_model_consecutive_failures >= 2` `for: 1m`，同 labels。规则 promtool 校验（沿 M1-04 R11）
```

```text
# 预审 :110（F8-R8）
override 给 worker 加 `extra_hosts: ["api.deepseek.com:127.0.0.1"]` 后 `up -d worker`；期望：两次 `MODEL_UNAVAILABLE`，Run `waiting_human`，`opspilot_model_consecutive_failures=2`，告警 firing；恢复 = 去掉 override 重启，人工 `resume`/`new_run`
```

## 字段对照表（F8 第 1 步，预审 :71-86 定稿）

本步只暴露进程级三项（服务健康、队列深度、provider 状态）+ 版本。工具错误、token、成本、trace 已按 `run_id` 落 PG，缺的是导出，归 #243（F8 第 2 实施步骤，自做预审）；卡住的 Run 本步只出 `queue_depth{state="running_lapsed"}`；结构化日志归 R10 的后续小 PR。事故 ID 不作标签（无界基数，也是 Q5 禁止的业务内容）。

## 实施切分（均目标 `main`，不做 stacked PR）

1. PR-1 产品：`opspilot/health.py` + 三入口接线 + R3 包装器 + Compose healthcheck 与 `compose.lab.yaml` override + `test_392_compose_static_impl.py:26` 调整 + F7 入口枚举收紧（验收作者：新监听只有 GET 三路由、无认证面只回固定键）。不改 loop/runner/persistence。真实证据：本机 Compose 起三进程，`curl` 三端点。
2. PR-2 lab + 演练（PR-1 合并后）：`values.yaml` scrape + 规则、`alertmanager-values.yaml` 子路由、需要时 `kind_lab.py` promtool 校验；两次演练证据；任务记录更新。
3. 之后各自预审：结构化日志（R10）、#243 outbox、升级回滚演练（F8 第 3 步）。

## 未确认（预审 :142-147）

上游 HolmesGPT server 是否有 `/healthz`/`/metrics`；OTel Demo chart 内置 Prometheus 的默认 `scrape_interval`/`evaluation_interval`；（写入时补）chart 0.37.8 的 `extraScrapeConfigs` 键形（字符串或列表，实施时核对）；Prometheus pod 到 `192.168.5.2` 的可达性（推断可达，未实测）；da-server ↔ 目标机跨主机抓取（归 F9 接线预审）。
