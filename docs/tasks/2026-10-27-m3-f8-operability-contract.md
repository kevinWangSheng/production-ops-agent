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

本步只暴露进程级三项（服务健康、队列深度、provider 状态）+ 版本。工具错误、token、成本、trace 已按 `run_id` 落 PG，缺的是导出，归 #243（F8 第 2 实施步骤；合同 r3 已裁，见下文，实施 #427/#428）；卡住的 Run 本步只出 `queue_depth{state="running_lapsed"}`；结构化日志归 R10 的后续小 PR。事故 ID 不作标签（无界基数，也是 Q5 禁止的业务内容）。

## 实施切分（均目标 `main`，不做 stacked PR）

1. PR-1 产品：`opspilot/health.py` + 三入口接线 + R3 包装器 + Compose healthcheck 与 `compose.lab.yaml` override + `test_392_compose_static_impl.py:26` 调整 + F7 入口枚举收紧（验收作者：新监听只有 GET 三路由、无认证面只回固定键）。不改 loop/runner/persistence。真实证据：本机 Compose 起三进程，`curl` 三端点。
2. PR-2 lab + 演练（PR-1 合并后）：`values.yaml` scrape + 规则、`alertmanager-values.yaml` 子路由、需要时 `kind_lab.py` promtool 校验；两次演练证据；任务记录更新。
3. 之后各自预审：结构化日志（R10）、#243 outbox、升级回滚演练（F8 第 3 步）。

## 未确认（预审 :142-147）

上游 HolmesGPT server 是否有 `/healthz`/`/metrics`；OTel Demo chart 内置 Prometheus 的默认 `scrape_interval`/`evaluation_interval`；（写入时补）chart 0.37.8 的 `extraScrapeConfigs` 键形（字符串或列表，实施时核对）；Prometheus pod 到 `192.168.5.2` 的可达性（推断可达，未实测）；da-server ↔ 目标机跨主机抓取（归 F9 接线预审）。

## 结构化日志合同（r2）：F8-R10 的后续小 PR

- 预审：全新上下文 Agent（Opus 5.5），只读，基线 origin/main `b8a5fa5a`，2026-10-31；全文在仓库外 `~/.agent/handoffs/opspilot-prereview-f8-logs-2026-10-31.md`（下称「预审」，行号指该文件）。上游 HolmesGPT 的日志格式未核对（本机无源码，**未确认**），按 stdlib logging + JSON 行通行做法设计。
- 裁决（r2，用户 2026-10-31）：用户原话「没有问题,写入进去,但是不要实现」；grilling Q1「7 项按推荐落地」yes。F8L-Q1–Q3 按推荐 (a)（Agent 按用户常设指示自定）；F8L-R1–R9 照预审。记录见 `~/.agent/handoffs/opspilot-rulings-2026-10-31-f14s3-f8logs.md`。
- **本轮不实施**：实施等 M3 方向 Q4 第 ① 步全部完成后再排。
- 关键现状（预审 :11-34）：worker 与 Observer 用 `basicConfig` 文本格式；web 没有配置 root logger，`opspilot.*` 的 INFO 行今天被丢弃，WARNING 以上只打裸 `msg`；84 处日志调用全部把 kv 写在格式串里，无 `extra=`；已有 kv `version=`（`knowledge/worker.py:196-197`）、`revision=`（`observer/loop.py:251,261`）与候选固定字段同名；调查 loop、runner、tools、client 无日志调用；web 5 处 `.exception()` 记完整 traceback（`web/service.py:2523,2628`，`web/__main__.py:155,159,163`）。
- 全量探针（预审 :78-84）：草稿（`opspilot/logs.py` + 三入口接线，不含 R4 kv 与 Q1 版本字段）`tests/acceptance` **探针 0 失败**（3014 passed、303 skipped，跳过均为 `M1_DURABLE_POSTGRES=1` 门控）；定向 205 passed；测试里无钉日志格式的断言，读进程日志的只有 `tests/test_393_installed_run_live_impl.py:175-176` 子串断言。
- 共享脱敏实测（预审 :110-125，Q2 不选 (b) 的依据）：`redact_credentials` 把 Observer 关联键 `session=<id>`、`prompt_tokens`、`auth: ok` 改成 `[REDACTED_CREDENTIAL]`，而裸写的 `Incorrect API key sk-…` 与模型散文原样漏过。

### 需用户裁决（r2 已裁决）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| F8L-Q1 | 按「事故和版本」关联靠什么字段（R10「不新增内容字段」与 #386 字段表「每条日志带 incident/run/version」不一致） | (a) 固定字段加两个**进程常量** `version`（`opspilot.__version__`）、`revision`（与 `/metrics` `opspilot_build_info` 同一取值与校验，`health.py:146-147`）；事故/会话/Run 关联沿用现有 kv（`incident=` `session=` `observation_id=` `run=`），不按调用注入上下文、不改键名。代价：关联键名不统一（`observation=` 与 `observation_id=` 并存），查询要按 logger 选键；与已有 kv `version=`/`revision=` 同名，须按 R4 嵌套避免覆盖。(b) 严格照 R10，一个字段都不加；版本只能靠镜像标签或启动行推断。代价：F8 steps[0]「按 version 关联」在日志面上做不到，事后要再改格式。(c) 用 contextvars 在 attempt/sample 作用域给每行注入 `incident_id/run_id`。代价：要在 runner/sampler 线程里设上下文，碰调查与恢复路径（触发 ADR-0006 trace 要求），超出「不改行为」小 PR | (a) | 日志字段合同（下游查询依赖）；F8 steps[0] 投影 | (a)，Agent 按推荐自定（用户常设指示） |
| F8L-Q2 | 「不记 provider 文本 / 凭据」靠哪一层保证 | (a) 源头纪律 + 确定性测试，formatter **不**调用共享脱敏：独立复核 84 处调用（结论随 PR 记录），加哨兵测试（模型客户端抛出/返回带哨兵凭据与哨兵 provider 文本，worker/Observer/web 入口跑一轮，断言所有捕获行都不含哨兵）；web 5 处 `.exception()` 保留 traceback（不改行为），列入复核清单。代价：保证依赖调用点自律，新增调用点要靠测试与审查守住。(b) formatter 对 `msg`/`exc` 调 `redact_credentials` 兜底。代价（探针实测）：把 Observer 的关联键 `session=<id>` 改成 `[REDACTED_CREDENTIAL]`（#366 同源），`prompt_tokens`/`auth: ok` 同样误伤；`Incorrect API key sk-…` 与模型散文**照样漏过**；每行多一次 #365 的潜在二次方开销。(c) 日志专用窄规则（只认 `Authorization`、`api_key=`、URL userinfo）。代价：第二套脱敏规则要单独维护和审查，仍挡不住散文 | (a)：兜底既误伤关联键又挡不住真正的 provider 文本，换不来保证；源头纪律今天已成立（worker 只记类型/码） | 安全边界；与 #366/#365 的耦合 | (a)，Agent 按推荐自定（用户常设指示） |
| F8L-Q3 | 验收怎么判、`passes` | (a) 确定性测试（三入口每行一个 JSON 对象、固定键齐全、多行消息与 traceback 仍是一行、`uvicorn`/`psycopg.pool` 行同格式、Q2 哨兵断言、`OPSPILOT_LOG_FORMAT=text` 回退）+ 一次 Compose 真实运行抽检（`install.sh compose logs` 三服务，含一次真实模型调用的 Run 与一次停 PG 的失败行，人工核对无凭据/无 provider 文本，摘录入任务记录）；不附 LangSmith trace（不触碰 loop/报告校验/恢复路径，ADR-0006）；F8 `passes` 保持 false。代价：真实抽检是人工判读，不是确定性断言。(b) 只做确定性测试。代价：Compose 下 Docker 实际收到什么没有证据。(c) 另加 trace。代价：无对应改动面 | (a) | 验收证据形状；`passes` | (a)，Agent 按推荐自定（用户常设指示） |

### 可逆默认（照预审）

| 编号 | 问题 | 默认 | 理由 / 出处 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| F8L-R1 | 实现位置与依赖 | 新模块 `opspilot/logs.py`（stdlib `logging` + `json`），三入口各调一次 `configure()`：替换 worker_main.py:565、observer/__main__.py:217 的 `basicConfig`，web 在 `_serve()` 起点调用 | 不加 structlog/python-json-logger 依赖（全局规则：不擅加依赖）；CLI 子命令（migrate/doctor/install）不在范围 | 依赖面 | 照预审 |
| F8L-R2 | 开关与默认 | 见下方代码块（预审 :51） | 这就是本 PR 的目的；`text` 留给开发者；与其他 env 的拒绝启动做法一致（`health.py:506`）；`install.env` 不加键 | 部署 | 照预审 |
| F8L-R3 | 固定字段与时间 | 见下方代码块（预审 :52） | 墙钟只用于日志显示，不参与任何判定；`msg` 保留全文，旧的子串查询（如 `tests/test_393_installed_run_live_impl.py:175-176`）不受影响 | 格式 | 照预审 |
| F8L-R4 | kv 映射 | 见下方代码块（预审 :53） | 值来自参数而不是解析渲染后的文本（`code=%s` 的值可能含空格与引号，§4.2）；嵌套避开 `version=`/`revision=` 与固定字段同名（§0）；不改任何调用点 | 格式 | 照预审 |
| F8L-R5 | 异常栈 | `exc_info` → `exc` 字段（`formatException` 全文），`stack_info` → `stack`；整条仍是一行（JSON 转义换行） | 不改行为：今日 traceback 也会输出；多行 traceback 在 Docker 里拆成多条是现有痛点 | 格式 | 照预审 |
| F8L-R6 | 第三方库 | root 只挂一个 stderr handler、级别 INFO；web 调 `uvicorn.run(…, log_config=None)`，`uvicorn.error`/`uvicorn.access` 冒泡到 root，同格式；访问日志保留，从 stdout 改到 stderr；`psycopg.pool` 等同格式 | 一个进程一种格式；可见变化：web 今天被丢弃的 INFO 行会出现（§0），列在 PR 正文 | web 日志量 | 照预审 |
| F8L-R7 | 单行与转义 | 见下方代码块（预审 :56） | 探针 §4.2：含 `\n` 与 `"` 的 msg 仍是一行合法 JSON | 格式 | 照预审 |
| F8L-R8 | 不做的事 | 不改任何调用点的措辞与键名（含 `observation=`/`observation_id=` 不统一）；不加长度上限；不改 Compose logging driver/轮转 | R10「不改行为」；键名统一与轮转（72h soak 前需要）各开 issue | 范围 | 照预审 |
| F8L-R9 | 测试隔离 | `configure()` 幂等（替换 root handler 而非追加）；pytest `caplog` 用自己的 handler，不受影响 | 探针：相关 205 条定向测试不受影响（§4.1） | 测试 | 照预审 |

逐字节细节（原文摘自预审，行号为预审文件行号）：

```text
# 预审 :51（F8L-R2，默认）
`OPSPILOT_LOG_FORMAT`：`json`（默认）或 `text`（旧格式串 `%(asctime)s %(name)s %(levelname)s %(message)s`）；其他值拒绝启动、非零退出
```

```text
# 预审 :52（F8L-R3，默认）
`ts`（`record.created` 转 UTC，RFC 3339 毫秒，`Z` 结尾）`level`（`levelname`）`logger`（`name`）`msg`（`getMessage()` 全文，与今日文本一致）+ Q1 定的进程常量
```

```text
# 预审 :53（F8L-R4，默认）
只从格式串里 `name=%…` 占位符按位置取对应 `record.args`，放进嵌套对象 `kv`（不铺到顶层）；值：`int/float/bool/None` 原样，其余 `str()`；非 `name=` 占位符不提取；格式串不匹配或参数数不一致时不出 `kv`，`msg` 照常
```

```text
# 预审 :56（F8L-R7，默认）
`json.dumps(…, ensure_ascii=False, default=str)`；换行、引号、控制字符由 JSON 转义，日志注入无法伪造新行
```

```text
# 预审 :91-95（草稿 formatter 输出节选；草稿 `ts` 为 `+00:00` 结尾，R3 定为 `Z`，实现时调整，见预审 :100）
{"ts": "2026-10-31T20:39:20.585+00:00", "level": "INFO", "logger": "opspilot.worker", "msg": "attempt incident=11111111-2222-3333-4444-555555555555 status=hand_off reason=('MODEL_UNAVAILABLE',)"}
{"ts": "...", "level": "WARNING", "logger": "opspilot.schema", "msg": "takeover accepted a column-order-only difference:\n- a int\n+ b int"}
```

### 切分与范围（预审 :72-74）

一个 PR：`opspilot/logs.py` + 三入口接线 + 实现者测试；独立验收测试由另一作者按本表与 F8L-Q3 编写（含哨兵断言）；之后 Compose 真实抽检写入任务记录。另开 issue：关联键名统一（`observation=`/`observation_id=`）、Compose 日志轮转（soak 前）。

### 未确认（预审 :136-140）

Docker 宿主 daemon 的 json-file 轮转配置；`web/service.py:1579,1659` 的 `str(exc)` 是否恒为带固定码的异常、web `.exception()` 的异常消息是否可能引用模型输出或证据原文（推断很低，交 F8L-Q2 复核）；上游 HolmesGPT 的日志格式。

## #243 产品模式 trace outbox 合同（r3，F8 第 2 实施步骤）

- 预审：全新上下文 Agent（Opus 5.5），只读，基线 origin/main `ea2d6e16`，2026-10-31；全文在仓库外 `~/.agent/handoffs/opspilot-prereview-243-outbox-2026-10-31.md`（下称「预审」，行号指该文件）。上游 HolmesGPT 没有 outbox（ADR-0006 背景，`9e21560`），本合同无上游可照搬，只照搬「默认空实现、一个开关打开导出」。
- 裁决（r3，用户 2026-10-31 grilling，d1 会话 Q14–Q18）：OB-Q1–Q5 全部按推荐 (a)；OB-R1–R13 照预审（Agent 可逆默认）。记录见 `~/.agent/handoffs/opspilot-rulings-2026-10-31-m3-batch4.md`。
- 关键现状（预审 :11-37）：`OPSPILOT_TRACE` 只有 `off|lab`，lab 每 Run 重做 fail-closed 证明，其中要求数据源是回环或 `*.svc`；产品 Compose 不设 `OPSPILOT_TRACE`，实际什么都不导出；outbox 只有领域 DTO `ExportOutboxEntry`（`opspilot/domain/records.py:18-27,54-74`），没有表、写入点和投递者；`opspilot_steps.response` 含 `assistant.reasoning_content`，outbox 载荷不能从它整段拷贝；worker 池 `capacity + 2`，Compose 4/2，无余量。

### 需用户裁决（r3 已裁决）

| 编号 | 问题 | 裁决要点 | 影响面 |
|---|---|---|---|
| OB-Q1 / Q14 | 产品模式可发到 LangSmith 的字段 | (a) 只发标识、版本、数字和固定码：incident/run/step/复盘 attempt ID；镜像 version/revision、`prompt_revision`/`tool_schema_revision`/`context_policy_revision`、模型名；span 类型与开始/结束时间；token 数（输入/输出/合计/推理）；`finish_reason`；Run 状态与交接原因码、工具状态类与原因码（固定码形状见下方代码块，其余记 `UNCLASSIFIED`）；登记表解析出的工具名；`source_contact` 三态；预算秒数。不发提示词与回复、工具视图与原始结果、查询语句、目标名/服务名/告警标签、报告与结论文本、`reasoning_content`、任何凭据 | 数据出口（PRODUCT-CONSTRAINTS Data flow、C3 §12） |
| OB-Q2 / Q15 | 怎么打开、怎么保证只在合成 lab | (a) 新开关 `OPSPILOT_TRACE=outbox`，默认关；打开须 LangSmith endpoint 在白名单、`LANGSMITH_PROJECT` 以 `opspilot-lab-` 开头、key 存在，缺一不投递（fail-closed）并在 `/metrics` 报原因码；不再要求数据源回环（lab 身份由运维以项目名前缀声明）。ADR-0006 决定 2 追加一行 | 出口开关；ADR-0006 |
| OB-Q3 / Q16 | 本地 outbox 行与 LangSmith 副本的保留 | (a) 本地行投递成功或丢弃 7 天后删除，worker 只对 outbox 表有 DELETE；LangSmith 产品项目只用基础 14 天保留、不开 180 天；C3 §11 保留与删除的「外部副本不主动删」偏离删除，改写为「产品项目 14 天自然过期」 | 保留合同；C3 §11；worker 权限 |
| OB-Q4 / Q17 | F8 第 1 步「成本按事故和版本关联」口径 | (a) 发 token 数 + `ls_provider=deepseek` + `ls_model_name`；LangSmith 工作区配 DeepSeek 价格条目由平台估算美元成本，按 incident/version 元数据筛选；证据写明「估算，权威为余额差」，并对一轮估算总额与余额差对比一次 | F8 第 1 步判定口径 |
| OB-Q5 / Q18 | 迁移与 downgrade | (a) 新迁移 0024，downgrade 不拒绝：先按 pending/delivered/dropped 统计行数并 `RAISE NOTICE`（只有计数），再删触发器与函数、撤授权、删表；有未导出行也照删，业务表一行不动。拆 PR-1（写入侧，带 0024，不出境）与 PR-2（投递出境） | 回滚路径；F8 第 3 步两个版本（F8S3-Q1） |

固定码形状（预审 :45 OB-Q1 (a) 原文节选，字节照抄）：

```text
工具状态类与原因码（只收 `^[A-Z][A-Z0-9_]{1,63}$`，其余记 `UNCLASSIFIED`，沿 tracing.py:112）
```

### 可逆默认（照预审，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| OB-R1 | 表 `opspilot_trace_outbox`：`entry_id bigint identity`、`idempotency_key text UNIQUE`、`incident_id`、`run_id`（复盘行为空）、`kind`（`run_state|model_step|tool_result|generation`）、`payload jsonb`（只有 OB-Q1 白名单键，CHECK `octet_length(payload::text)<=4096`）、`state pending|delivered|dropped`、`attempts`、`next_attempt_at`、`drop_reason`、`created_at clock_timestamp()`；索引 `(state,next_attempt_at)` | 复用领域 DTO 状态名；载荷上限防大行 |
| OB-R2 | 模型步、工具结果、复盘生成 attempt 在 `commit_step`/`commit_tool`/复盘提交的同一事务由应用层插行，载荷由一个函数从类型化字段按白名单构造（不读 `response` 整段）；Run 状态变化用 `opspilot_runs` 上 `AFTER UPDATE OF state` 触发器插行（非 `SECURITY DEFINER`，web、worker 都要 outbox 列级 INSERT） | 覆盖散在三处、两个角色的状态写；白名单集中一处便于复核 |
| OB-R3 | `idempotency_key` = `kind:subject_id:序号`（例 `model_step:<step_id>`、`run_state:<run_id>:<control_generation>:<state>`），`ON CONFLICT DO NOTHING`；OTLP trace_id = sha256(run_id) 前 16 字节，span_id = sha256(idempotency_key) 前 8 字节 | C3 §11「至少一次、幂等身份」；LangSmith 是否去重重复 span 未确认，PR-2 实测 |
| OB-R4 | worker 进程内一个投递线程，只在 `OPSPILOT_TRACE=outbox` 且 OB-Q2 条件满足时投递；`SELECT … FOR UPDATE SKIP LOCKED LIMIT 100` 领取；池下限 `capacity + 3`，Compose `OPSPILOT_POOL_MAX_SIZE` 4→5 | key 只在 worker；不新增运行时 |
| OB-R5 | 退避 5s→10min 指数，最多 8 次后 `dropped(MAX_ATTEMPTS)`；HTTP 4xx（除 408/429）`dropped(REJECTED)`；构造或序列化失败 `dropped(UNSERIALIZABLE)`；pending 上限 20 000，超出最旧的 `dropped(BACKLOG)`；每批 10s 超时 | C3 §11「积压有界丢弃并记录计数」 |
| OB-R6 | 投递失败对 Run 完全无影响：Run 状态、交接、预算与不开导出时相同 | C3 §11；PRODUCT-CONSTRAINTS:36 |
| OB-R7 | worker `/metrics` 加 `opspilot_trace_outbox_pending`、`opspilot_trace_outbox_dropped_total{reason}`、`opspilot_trace_outbox_last_delivery_age_seconds`、`opspilot_trace_export_enabled{reason}`；读数来自线程写的进程内快照，健康监听不查库 | 沿 F8-R4 |
| OB-R8 | 未开导出时 outbox 也总是写；7 天清理在开关关闭时也运行，只删不发 | 一条代码路径；本地最多 7 天、零出境 |
| OB-R9 | `OPSPILOT_TRACE` 三值互斥 `off|lab|outbox`；`lab` 保持现状（带内容、直发、不经 outbox），`lab` 下 outbox 照写不投递 | ADR-0006 决定 5 不动 |
| OB-R10 | 不做 Observer 埋点（#159，`post-m3`）、不做 `payload_kind=eval`、不改 `loop.py` 与提示词/工具面 | 一个 PR 一个 C3 条款 |
| OB-R11 | 迁移 0024：建表、触发器、列级授权；downgrade 按 OB-Q5；不回填已有 Run；同步 `RUNTIME_GRANTS` 与头版本钉值；部署顺序按 C3 §11（三运行时停机后迁移） | 与 0019/0023 先例一致 |
| OB-R12 | `install.env` 加 `OPSPILOT_TRACE`（默认 `off`）；`LANGSMITH_PROJECT` 与可选 `LANGSMITH_ENDPOINT` 放 `worker_env`；entrypoint 白名单加这两项；`install.sh` 默认不打开 | 默认零出境 |
| OB-R13 | 证据 `docs/evidence/243-trace-outbox/`：一次 Compose 有界真实 Run（`OPSPILOT_TRACE=outbox`）冻结 `summary.json` + trace 链接；按 `incident_id` 与 version 筛到同一 Run 的 token、工具错误码、状态；故障注入①投递目标不可达（`extra_hosts` 指 127.0.0.1）②投递中途重启 worker，照实记重复 span 条数；载荷全文抽查无提示词、凭据、`reasoning_content` | ADR-0006；AGENTS.md 真实 Run 证据 |

授权（预审 :74）：web 只得 outbox 列级 INSERT（触发器用）；worker 得 SELECT、列级 INSERT、`UPDATE(attempts,next_attempt_at,state,drop_reason,finalized_at)`、DELETE；Observer、retention 无。

写入时自定（lead，一行理由）：

- r3-L4 终态时间（#434 bot P2 采纳）：OB-R1 加列 `finalized_at timestamptz NULL`，state 变为 `delivered`/`dropped` 时同一 UPDATE 写入 `clock_timestamp()`（CHECK：终态必须有 `finalized_at`，pending 必须为空）；7 天清理对终态行按 `finalized_at`、对未开导出时的 pending 行按 `created_at` 计算。理由：Q16「投递成功或丢弃 7 天后」需要持久化的转移时间，仅有 `created_at` 时 worker 重启后无法正确计算；随 0024 一次建好，PR-2 不再加迁移。
- r3-L5 未投递的 pending 过期（#434 bot P2 采纳）：「投递未在运行」= 开关关闭，或 `OPSPILOT_TRACE=outbox` 但 OB-Q2 前提缺失而 fail-closed；两种情况下 pending 行都按 `created_at` 满 7 天删除（`purge_expired(export_enabled=False)` 即「投递未在运行」）。投递在运行时 pending 行经重试转 `dropped` 再按 r3-L4 过期。理由：否则配置错误会让 pending 标识无限期留存，违反 Q16 的 7 天本地保留。
- r3-L1 清理线程归属：Q16 写「由投递线程删除」，R8 要求开关关闭也清理，而 PR-1 不投递。定为 worker 内一个 outbox 维护线程：PR-1 只做 7 天清理（不联网），PR-2 在同一线程加投递；池 `capacity + 3` 与 Compose 4→5 随 PR-1 落地。理由：删除者始终是 worker 的这一个线程，满足 Q16 与 R8，PR-2 不再改池。
- r3-L2 钉值：`tests/acceptance/test_347_release_reframe_input.py` 头版本 `0023_retention_raw_cleanup`→`0024_*` 与 `RUNTIME_GRANTS` sha256 指纹（预审 :93），以及预审 :94 列出的非验收层头版本钉值与 Compose 池钉值，属每次加迁移的例行钉值更新，不算削弱验收（0022、0023 的 PR 同样改过）。

### 切分（预审 :81-84，均目标 `main`，不做 stacked PR）

1. PR-1 写入侧：0024 + 授权 + 白名单载荷构造函数 + StepStore/复盘写入点 + Run 状态触发器 + worker 清理线程（r3-L1）；不投递、不出境。证据：PG 集成测试 + 一次真实 Run 后的 outbox 行导出（本地）。
2. PR-2 投递侧：`OPSPILOT_TRACE=outbox`、fail-closed 原因码、投递、指标、Compose/install 配置、两次故障注入、LangSmith 真实证据（R13）与 Q17 成本对比。

### 未确认（预审 :103-108）

LangSmith 对同一 OTLP span ID 重复投递是否去重；子 span 先到、根 span 晚到能否接受（退路：Run 停下时成批发）；Compose + otel-demo 下 `OPSPILOT_OTEL_PROMETHEUS_URL` 的实际取值；上游 `a0009195` 是否仍无 outbox。

## 第 3 步合同（r3）：带在途事故的升级与回滚演练

- 预审：全新上下文 Agent（Opus 5.5），只读，基线 origin/main `ea2d6e16`，2026-10-31；全文在仓库外 `~/.agent/handoffs/opspilot-prereview-f8-step3-2026-10-31.md`（下称「预审」）。全量探针（预审 :76-81）：草稿（`schema downgrade` 动作 + `stop_grace_period: 40s` + UPGRADE.md 一句）**探针 0 失败**（3014 passed、303 skipped）；`tests/test_392_compose_static_impl.py`、`tests/test_393_*` 未跑，实现时定向跑。
- 裁决（r3，用户 2026-10-31 grilling，d1 会话 Q24–Q26）：F8S3-Q1–Q3 按推荐 (a)；F8S3-R1–R11 照预审。
- 关键现状（预审 :11-34）：今天所有可安装提交 schema head 都是 0023、Run `versions` 相同，`install.sh upgrade` 是 `migrate: unchanged`、在途 Run 原样续跑，无可演练的变化；没有回滚入口（`opspilot.schema` 只有 `migrate|check`）；UPGRADE.md「there are no down-migrations」与 `docs/development.md:28` 冲突；Compose 未设 `stop_grace_period`（推断 10s 硬杀，未实测）。

### 需用户裁决（r3 已裁决）

| 编号 | 问题 | 裁决要点 | 影响面 |
|---|---|---|---|
| F8S3-Q1 / Q25 | 用哪两个版本 | (a) 新版 = #243 PR-1（带 0024）合并提交，旧版 = 它在 main 上的父提交；均本地由各自 checkout 的 `install.sh` 构建，同为 0.1.0，按 git revision 与镜像 ID 区分；排在 PR-1 合并后，要 trace 链接作证据则等 PR-2；F9 第 4 步（固定 digest 升级）不因本步算通过，收口时在租用主机另做 | 排期、证据可信度 |
| F8S3-Q2 / Q26 | 回滚定义与 downgrade 被拒 | (a) 停全部运行时 → 新版镜像执行 `python -m opspilot.schema downgrade --to <旧 head>` → 旧 checkout `install.sh upgrade`（migrate unchanged、init-roles、启动、`verify_head`）；downgrade 因丢数据被拒时不还原备份，前滚修复（兼容恢复），写入 UPGRADE.md；C3 §11 补同义规则 | 运维文档；C3 §11；新增 owner CLI 动作 |
| F8S3-Q3 / Q24 | 在途事故演练哪些形状 | (a) 必测 4 种，升级与回滚两段都判：①模型调用中被停 → 新 worker 租约到期后续跑到终态、`versions` 不变；②`queued` 被新 worker 领取；③授权观察会话继续采样，停机缺口在样本时间上可见、不补采；④`human_owned`/`waiting_human` 原样保留、不开 Run。加做⑤不兼容版本段：只改 `tool_schema_revision` 的演练构建（不合并）→ `blocked(INCOMPATIBLE_STATE)`、旧行保留 → 人 `new_run` 在新版跑完。约 3 个真实 Run | 验收判定；真实调用量 |

### 可逆默认（照预审，一行理由）

| 编号 | 默认 | 理由 |
|---|---|---|
| F8S3-R1 | 只加 `opspilot.schema downgrade --to <rev>`（owner DSN，与 migrate 同服务/凭据；`--to` 须为已知且低于当前的 revision），经 `install.sh compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to <rev>` 调用；不加 `install.sh rollback` | 回滚要两个 checkout 配合，子命令省不掉；少一个面 |
| F8S3-R2 | UPGRADE.md「Rolling back」按 stop → downgrade → 旧 checkout `upgrade` 写；命令打印当前与目标 revision | 与 C3 §11 全停迁移同一前提 |
| F8S3-R3 | worker、observer 设 `stop_grace_period: 40s` | 让已有 30s 排空在安装路径生效（C3 §7） |
| F8S3-R4 | 本机 colima Compose 栈 + kind lab，持 `lockf ~/.agent/locks/kind-lab`；开窗前查 swap、不并行跑全量测试 | 形状③要真实 Prometheus；与 #393/#414 接线一致 |
| F8S3-R5 | 每次构建记录镜像 ID 与 OCI `revision`，另打本地 tag `opspilot:f8s3-<rev12>` 留存；各阶段导出一次 `opspilot_build_info` | 证明回滚真跑回旧 revision |
| F8S3-R6 | 升级前、升级后、回滚后三次只读快照（`alembic_version`、各 `opspilot_*` 行数、在途事故/Run/会话状态与代际）写进 `summary.json` 逐表比对 | 确定性断言，不靠 judge |
| F8S3-R7 | 阶段边界用 install.sh 日志 UTC；Run/会话/样本用 DB 时钟；停机窗 = 第一次 stop 到三个 `/readyz` 全 200；续跑延迟以租约 420s 为界，Observer 120s | 两个时钟只做区间对照 |
| F8S3-R8 | 证据 `docs/evidence/f8-step3-upgrade-rollback/README.md` + 冻结 `summary.json` + 时间线、build_info、alembic 版本、行数快照、Run 事件、样本时间、`opspilot-self` 记录；有 PR-2 时附产品模式 trace 链接，否则写明无 trace 及原因 | AGENTS.md 验证与汇报 |
| F8S3-R9 | 顺序：升级段 → 回滚段 → 不兼容段 | 每段只变一个因素 |
| F8S3-R10 | 模型 key 由用户放入安装栈 `worker_env`（K2，#393 先例）；余额差由用户对账写入任务记录 | Agent 进程不接触 key |
| F8S3-R11 | UPGRADE.md 改为「旧镜像不能直接起在已迁移的库上；回滚见 Rolling back」 | 与 `development.md:28` 一致 |

写入时自定（lead，一行理由）：r3-L3 行数快照「只增不减」不含 `opspilot_trace_outbox`：0024 的 downgrade 按 Q18 会删表，该表单列 downgrade 输出的三类计数；其余业务表照 R6 逐表比对。

与 #243 的衔接：OB-Q5 定 0024 的 downgrade 不拒绝，所以以 PR-1 为新版时回滚段的 downgrade 一定能跑通（预审 :71、#243 预审 :49）；跨越升级/回滚的在途 Run 在 LangSmith 上只有部分 span，证据照实写。

### 未确认（预审 :88-93）

本机 compose 版本 `docker compose stop` 默认超时实际值；Observer 停机后同一会话多次到期是只采一次还是逐次补；`OPSPILOT_TRACE=lab` 在安装栈下能否通过 `check_lab_target`（本步改用 PR-2 的 `outbox` 模式，不依赖此项）。
