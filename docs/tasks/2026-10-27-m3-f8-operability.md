# M3 运行可观测（F8 第 1–3 步）

- 状态：进行中（第 1 实施步骤合同 r1 已裁决；PR-1 #413 已合并 `6845ffc7`（PR #418）；PR-2 #414 lab 抓取、两条自告警与两次演练已合并 `7cd29308`（PR #421）；结构化日志合同 r2 已裁决（2026-10-31），2026-11-01 补 r2.1 并转为执行（#428 合并后首条空闲线）；#243 outbox 与第 3 步升级回滚合同 r3 已裁决（2026-10-31），实施 #427 → #428，演练 #429）
- 更新日期：2026-11-01
- 依据：[M3 开放决策](2026-10-27-m2-exit-m3-opening-decision.md) Q2 第 4 条、Q5、Q6；feature_list.json F8；C3 §11（可观测性、outbox、部署与 schema 迁移）；#243 #159
- 工作区：#413、#414 的 worktree 均已在合并后删除；结构化日志未创建

## 目标与范围

健康端点 `/healthz`、`/readyz`：只在内网监听，只报 schema 是否在 head、队列深度、最近一次模型调用状态类别；lab Prometheus 抓取，Alertmanager 两条规则（Agent 不在、模型连续失败），演练停 worker 与断模型出口各一次（第 2 步）。F8 第 1 步字段对照表（provider 状态、工具错误、卡住的 Run、token 与成本按事故和版本关联）在预审中逐项定；#243 产品模式 trace outbox 按 C3 §11 实现；结构化日志。第 3 步：带在途事故的版本升级与回滚（上一镜像 + 允许时 downgrade，否则兼容恢复）演练。不含：72 小时 soak（收口线）。

## 前提与完成条件

- 前提：交付底座线的产品镜像（升级/回滚演练需要两个固定版本）。
- 完成条件：第 1–3 步各有真实运行证据；首个实施步骤开工前完成合同预审。

## 合同（r1）

用户 2026-10-31 裁决：F8-Q1–Q4 全部按推荐 (a)，可逆默认 F8-R1–R11 照预审。完整决定表、指标名与规则原文：[F8 第 1 实施步骤合同](2026-10-27-m3-f8-operability-contract.md)。

- F8-Q1：本步 = 三进程 `/healthz` `/readyz` `/metrics` + lab Prometheus 抓取 + 两条规则 + 停 worker / 断模型出口两次演练；#243、结构化日志、#159 不在本步；F8 `passes` 保持 false。
- F8-Q2：三进程各起独立内网监听（`OPSPILOT_HEALTH_BIND`，容器内 9464），无认证、只回固定键 + 版本，不挂 web 路由表；只在 lab/开发 override 发布到宿主 127.0.0.1；跨主机接线随 F9 A4 另做预审。
- F8-Q3：自监控告警走 Alertmanager 子路由到不调用 OpsPilot 的 `opspilot-self` 接收器；交接证据 = firing 记录 + 工作台 Run 交接状态；不新增外发通知。
- F8-Q4：停 worker 不改产品；Run 停在 `queued`、`queue_depth` 上升，缩短墙钟后经页面清扫或 worker 回归得到 `DEADLINE_EXCEEDED`；自动清扫不做。
- F8-R1–R11：见合同文件。

## 合同（r2，结构化日志）

用户 2026-10-31 裁决（原话「没有问题,写入进去,但是不要实现」）：F8L-Q1–Q3 按推荐 (a)（Agent 按用户常设指示自定），可逆默认 F8L-R1–R9 照预审。完整决定表与日志行形状原文：[结构化日志合同（r2）](2026-10-27-m3-f8-operability-contract.md#结构化日志合同r2f8-r10-的后续小-pr)。出处 `~/.agent/handoffs/opspilot-rulings-2026-10-31-f14s3-f8logs.md`。

- F8L-Q1 关联字段：固定字段只加进程常量 `version`、`revision`（与 `/metrics` `opspilot_build_info` 同源）；事故/会话/Run 沿用现有 kv（`incident=` `session=` `observation_id=` `run=`），不注入上下文、不改键名。
- F8L-Q2 不记 provider 文本/凭据：源头纪律 + 哨兵确定性测试；formatter 不调 `redact_credentials`；web 5 处 `.exception()` 保留 traceback 并列入复核清单。
- F8L-Q3 验收：确定性测试 + 一次 Compose 真实抽检（含一次真实模型调用、一次停 PG）；不附 LangSmith trace；F8 `passes` 保持 false；PR 正文写明 web 的 INFO 行开始出现。
- F8L-R1–R9：照预审（新模块 `opspilot/logs.py`、`OPSPILOT_LOG_FORMAT=json|text`、kv 嵌套在 `kv`、`exc`/`stack` 单行、uvicorn `log_config=None` 等，原文见合同文件）。
- ~~执行约束：实施等 M3 方向 Q4 第 ① 步全部完成后再排~~（被 2026-11-01 Q1 取代）。
- r2.1（用户 2026-11-01，`~/.agent/handoffs/opspilot-rulings-2026-11-01-m3-optimization.md`）：Q5 `configure()` 接管 `sys.excepthook`/`threading.excepthook`，崩溃 traceback 输出单行完整 ERROR JSON；拒绝启动的 `SystemExit` 提示与镜像入口脚本保持文本；两处 traceback 纳入凭据复核与哨兵测试。可逆默认 O2–O8 见合同「M3 优化项补充（r2.1）」；Compose 抽检并入 F14 第 3 步 kind 窗口。

切分：PR-1 产品（health 模块 + 三入口接线 + 模型调用计数包装器 + Compose healthcheck 与 lab override + 测试调整）→ PR-2 lab 抓取与规则 + 两次演练（PR-1 合并后）。之后各自预审：结构化日志、#243 outbox、升级回滚演练（第 3 步）。

## 合同（r3，#243 outbox + 第 3 步升级回滚）

用户 2026-10-31 grilling 裁决（d1 会话 Q14–Q18、Q24–Q26，`~/.agent/handoffs/opspilot-rulings-2026-10-31-m3-batch4.md`）：OB-Q1–Q5、F8S3-Q1–Q3 全部按推荐 (a)；OB-R1–R13、F8S3-R1–R11 照预审；lead 写入时自定 r3-L1–L3。完整表与字节级原文：[合同文件 r3 两节](2026-10-27-m3-f8-operability-contract.md#243-产品模式-trace-outbox-合同r3f8-第-2-实施步骤)。

- Q14 出口白名单：产品模式只发标识、版本、数字和固定码；不发提示词/回复、工具视图与原始结果、查询、目标名/服务名/告警标签、报告正文、`reasoning_content`、凭据。
- Q15 开关：`OPSPILOT_TRACE=outbox` 默认关；endpoint 在白名单、`LANGSMITH_PROJECT` 以 `opspilot-lab-` 开头、key 存在，缺一 fail-closed 并在 `/metrics` 报原因码；不再要求数据源回环。ADR-0006 决定 2 追加一行。
- Q16 保留：本地行投递成功或丢弃 7 天后删除（worker 只对 outbox 表有 DELETE）；LangSmith 产品项目只用 14 天基础保留；C3 §11 偏离改写为「产品项目 14 天自然过期」。
- Q17 成本：token 数 + `ls_provider=deepseek` + `ls_model_name`，平台估算，证据写「估算，权威为余额差」，对比一次。
- Q18 迁移：0024，downgrade 删表照删（`RAISE NOTICE` 计数）；PR-1 写入侧（#427，不出境）→ PR-2 投递侧（#428）。
- Q25 两个版本：PR-1 合并提交 vs 它的父提交，本地构建，同为 0.1.0；F9 第 4 步不因本步通过。
- Q26 回滚：停全部 → 新版镜像 `opspilot.schema downgrade --to <旧 head>` → 旧 checkout `install.sh upgrade`；downgrade 被拒时前滚修复，写入 UPGRADE.md，C3 §11 补同义规则。
- Q24 在途形状：①模型调用中被停 ②queued ③授权观察会话 ④human_owned/waiting_human，升级与回滚两段都判；加做不兼容段（只改 `tool_schema_revision`、不合并 → `blocked(INCOMPATIBLE_STATE)` → 人 `new_run`）；约 3 个真实 Run（#429）。
- r3-L1 清理与投递同在 worker 一个 outbox 线程，PR-1 只清理、池 `capacity + 3` 随 PR-1；r3-L2 头版本与 `RUNTIME_GRANTS` 指纹等为例行钉值；r3-L3 行数快照不含被 downgrade 删除的 outbox 表。

## #413 实现（对齐合同 r1）

行为：web、worker、Observer 各起一个 stdlib 监听（`opspilot/health.py`），只答 GET `/healthz` `/readyz` `/metrics`，其余路径与方法一律 404；web 的 8080 `app.routes` 未动。worker/Observer 的监听线程不查库，只读轮询线程记下的时间与计数；web 每次探测用自己池里的一个只读事务。模型调用计数包在 `slot_runner` 与复盘线程的 `DeepSeekClient` 外面，三者共用一份进程级计数；`loop.py`、runner、persistence 未改。

可逆技术选择（Agent 自定，一行理由）：

- 容器默认 bind 由镜像 `ENV OPSPILOT_HEALTH_BIND=0.0.0.0:9464` 给出，代码默认 `127.0.0.1:9464`：容器外跑镜像也一致，`install.env` 不加键（R11）。
- 镜像加 `ENV OPSPILOT_REVISION=${OPSPILOT_REVISION}` 供 `opspilot_build_info{revision}`；不合 `[0-9A-Za-z._-]{1,64}` 时记 `unknown`：标签值不受外部文本影响。
- bind 格式错或绑不上（含端口被占）都拒绝启动，非零退出，日志写明 `OPSPILOT_HEALTH_BIND` 与地址；只有显式空值才不起监听。同机跑两个裸进程时由操作者给各自不同的 bind 或空值（lead 裁决，#418 审查 r1：没有监听的进程会躲过外部检测）。
- web healthcheck 同时要求 8080 可连和 `/readyz` 200，`timeout: 12s`：监听先于 uvicorn 启动，保留旧检查的保证；探测事务最长约池等待 5s + 语句 5s。
- worker/Observer healthcheck `interval 5s / timeout 5s / retries 12`：`/readyz` 不查库，立即返回。
- worker「成功轮询」= 定时轮询里 deadline 清扫、backlog 与随后的可领取清单三步都没被存储拒绝（槽位全满时不列清单，不算拒绝），记在 `WorkerLoop.last_poll_success`：清扫每个间隔都跑，槽位全忙时也不会误报陈旧（PR #418 机器人 P1 后补上清单一步）。
- Observer 一轮里会话清扫、发布清扫和领取都没被存储拒绝时，才在轮询线程上按 `store.py:1866` 的到期条件跑一条 `count(*)`（只算事故观察会话，不含发布采样，合同 R2 口径），它也成功才算「成功轮询」并更新 `opspilot_observation_due`；被拒的一轮什么都不记（PR #418 机器人 P1）：监听线程不查库，也不占新连接。已知：一轮里有多个慢采样、总时长超过 15s 时 `/readyz` 会短暂 503 `POLL_STALE`。
- 计数器：非 `ModelError` 的异常原样抛出、不计数；码不合固定码形状记 `UNKNOWN`：标签集有界。
- `/metrics` 超过 4 KiB 时按行截断；首次成功轮询前 `opspilot_last_poll_age_seconds` 为 `+Inf`；web 读库失败时 `opspilot_queue_depth` 不出现（不报 0）；worker/Observer 不出 `opspilot_schema_at_head`（只在启动时校验过）。
- 探测抛异常回 500 `{"status": "error"}`，日志只记异常类型；不记每次请求的访问日志；`Server` 头不带 Python 版本。
- 防洪（PR #418 机器人 P2，bind 仍按 F8-Q2 为容器网络 `0.0.0.0:9464`）：三进程的监听同时最多处理 8 个请求，多出的在接收线程上立即回 503 `{"status": "busy"}` 并关闭，不排队、不新开线程；web 的 `/readyz` 与 `/metrics` 共用一次探测事务并缓存 5s（低于 15s 抓取间隔）：无论请求多快，最多每 5s 占工作台池一条连接；代价是 `/readyz` 与指标最多滞后 5s。库不可用时探测结果（503 `POOL_UNAVAILABLE`）同样缓存 5s，lead 裁定（审查 r3）：这样库宕机期间洪水也不会每个请求都去等池；恢复后最多晚 5s 回 200，与上面的滞后对称；探测抛出的异常不缓存。

真实运行（2026-10-31，本机 colima `m1-kind`，项目 `opspilot-413`，`compose.yaml` + `compose.lab.yaml`，镜像 `opspilot:413` 由 `c3aedcd3` 构建）：密钥由本次自行生成在仓库外并已删除；worker 的模型 key 是占位串，库里没有 Run，没有发生模型调用；Observer 的 Prometheus URL 是不可解析的占位地址，没有到期采样，没有发出查询。`up --wait` 后三个容器都是 `(healthy)`，宿主端口 `127.0.0.1:18181->8080`、`127.0.0.1:19464/19465/19466->9464`。原始输出（每进程第一条带响应头，其余只列 body）：

```text
### web /healthz
HTTP/1.0 200 OK
Server: opspilot-health
Content-Type: application/json
Content-Length: 36
Cache-Control: no-store

{"status": "ok", "version": "0.1.0"}
### web /readyz   -> 200 {"status": "ready", "version": "0.1.0"}
### web /metrics  -> 200 text/plain; version=0.0.4; charset=utf-8
# TYPE opspilot_build_info gauge
opspilot_build_info{version="0.1.0",revision="c3aedcd3e66fefe50c03fff0da05e1444c29d5c1"} 1
# TYPE opspilot_schema_at_head gauge
opspilot_schema_at_head 1
# TYPE opspilot_queue_depth gauge
opspilot_queue_depth{state="queued"} 0
opspilot_queue_depth{state="running_lapsed"} 0
### worker /healthz -> 200 {"status": "ok", "version": "0.1.0"}
### worker /readyz  -> 200 {"status": "ready", "version": "0.1.0"}
### worker /metrics -> 200
# TYPE opspilot_build_info gauge
opspilot_build_info{version="0.1.0",revision="c3aedcd3e66fefe50c03fff0da05e1444c29d5c1"} 1
# TYPE opspilot_last_poll_age_seconds gauge
opspilot_last_poll_age_seconds 0.6230616670000018
# TYPE opspilot_in_flight_attempts gauge
opspilot_in_flight_attempts 0
# TYPE opspilot_model_calls_total counter
# TYPE opspilot_model_consecutive_failures gauge
opspilot_model_consecutive_failures 0
# TYPE opspilot_last_model_call_category gauge
### observer /healthz -> 200 {"status": "ok", "version": "0.1.0"}
### observer /readyz  -> 200 {"status": "ready", "version": "0.1.0"}
### observer /metrics -> 200
# TYPE opspilot_build_info gauge
opspilot_build_info{version="0.1.0",revision="c3aedcd3e66fefe50c03fff0da05e1444c29d5c1"} 1
# TYPE opspilot_last_poll_age_seconds gauge
opspilot_last_poll_age_seconds 3.9677896140055964
# TYPE opspilot_observation_due gauge
opspilot_observation_due 0
### web POST /healthz                -> 404
### worker GET /                     -> 404
### 8080 app GET /healthz (via 18181) -> 404
```

故障注入：13:38:40Z `stop postgres`，13:38:56Z 起探测：

```text
### web /readyz      {"status": "not_ready", "reason": "POOL_UNAVAILABLE"} -> 503
### worker /readyz   {"status": "not_ready", "reason": "POLL_STALE"} -> 503
### observer /readyz {"status": "not_ready", "reason": "POLL_STALE"} -> 503
### web /metrics     opspilot_schema_at_head 0（opspilot_queue_depth 不出现）
### worker           opspilot_last_poll_age_seconds 26.61068274599529
```

`start postgres` 后 13:39:24Z 三个 `/readyz` 都回到 200。`STOPPING` 没抓到现场：对 Observer 发 SIGTERM 后进程 5 ms 内退出（日志 `signal=15` 13:39:32.607 → `observer stopped` .612），监听随之关闭，curl 得到空响应；该分支由确定性测试覆盖。三个容器日志里没有占位 key 或 `password=`。之后 `down --volumes` 只删了 `opspilot-413` 项目，镜像 `opspilot:413` 与生成的密钥目录已删除。

验证：`tests/test_413_health_impl.py`、`tests/test_413_compose_impl.py`（实现者测试，先红后绿）；定向回归 `tests/test_392_*`、`tests/test_394_image_version_impl.py`、`tests/acceptance/test_392_*`、`tests/acceptance/test_f7_step1_entry_points.py`、`tests/test_m1_observer.py`、`tests/test_f2_b1_impl.py`、`tests/test_f2_b2_impl.py`、`tests/test_m1_web_worker.py`、`tests/test_runtime_roles.py`、`tests/test_runtime_dsn_contract.py`、`tests/test_m1_otel_demo_contract.py`；`ruff check`、`ruff format --check`、`mypy`。未跑：全量 `make check` 与全量 pytest（本机 swap 紧张）；独立验收测试 `tests/acceptance/test_413_*` 由另一位作者编写，尚未合入本分支；独立审查未做。F8 `passes` 保持 false。

## #414 实现（PR-2，对齐合同 r1）

行为：lab Prometheus 加 job `opspilot`（三目标 `192.168.5.2:19464/19465/19466`、`role` 标签、15s），规则组 `opspilot-self` 两条（`OpsPilotAgentAbsent`、`OpsPilotModelCallsFailing`，表达式、`for`、标签照合同原文）；Alertmanager 加子路由 `opspilot_self="true"` → 无集成的接收器 `opspilot-self`。演练用两个 lab override：`compose.drill-egress.yaml`（worker `extra_hosts`，F8-R8 原文）与 `compose.drill-run-wall.yaml`（web `OPSPILOT_RUN_SECONDS=240`，F8-Q4）；`install.sh` 不引用。产品代码未改。

可逆技术选择（Agent 自定，一行理由）：

- `extraScrapeConfigs` 写成字符串：chart 0.37.8 子 chart 用 `tpl` 字符串追加（已核对模板），合同未确认项就此关闭。
- 新增 `kind_lab.py rules-check`：用 `helm template` 渲染 values，交给 lab pod 内的 promtool v3.5.0 / amtool v0.28.1 校验，并测试自监控路由只到 `opspilot-self`。宿主没有这两个二进制，pod 内版本与运行版本一致。
- `kind_lab.py up` 的 demo `helm upgrade` 加 `--force-conflicts`：2026-10-10 的手工 `kubectl patch` 拥有规则文件字段，改规则时 server-side apply 冲突；values 是 lab 的事实来源。副作用是 `up` 会收回故障钩子 patch 过的 flagd 字段。
- 两份既有测试（`test_kind_lab_alertmanager.py`）原先假设只有一个接收器、一条规则，改为按名字选取。

真实运行（2026-10-31 16:13–16:33Z，持 kind-lab 锁；栈 `opspilot-393`，镜像由 `install.sh upgrade` 从本 worktree 构建）：证据 [f8-step1-drills](../evidence/f8-step1-drills/README.md)。Prometheus pod 经 `192.168.5.2` 抓到三个监听（合同未确认项已实测）。演练一：停 worker 后 Run `queued`、`queue_depth` 0→1，`OpsPilotAgentAbsent` 约 3m46s 后 firing → `opspilot-self`；过 deadline 后打开事故页，清扫得到 `DEADLINE_EXCEEDED`。演练二：两次 `MODEL_UNAVAILABLE`，Run `waiting_human`，连续失败数 2，`OpsPilotModelCallsFailing` firing → `opspilot-self`；去掉 override 后人工 `resume`，两次模型调用 `ok`，Run 因演练目标不在 fixture 工具登记里以 `INCOMPLETE_INVESTIGATION` 交接。所有集成的 Alertmanager 通知计数均为 0。promtool/amtool 全部通过。

费用：被拦截的调用在本机就被拒绝，没有供应商费用；人工 `resume` 产生 2 次真实调用。余额差待用户对账（K2）。

未做：独立审查；全量 `make check`/pytest；F8 `passes` 保持 false。lab 留在本分支的配置上：停栈后 `OpsPilotAgentAbsent` 持续 firing 到 `opspilot-self`，不通知任何东西。

## 结构化日志实现（对齐合同 r2 + r2.1）

worktree `/Users/shenghuikevin/dev/AI/poa-386-logs`，分支 `feature/F8-386-structured-logs`（起点 origin/main `2e9db380`）。测试接缝说明（给验收测试作者）在仓库外 `~/.agent/handoffs/m3-opt-2026-11-01/seams-386.md`。

做了什么：新模块 `opspilot/logs.py`，`configure()` 给 root 装一个 stderr handler（INFO），按 `OPSPILOT_LOG_FORMAT` 出 JSON 行或旧文本格式，并接管 `sys.excepthook`/`threading.excepthook`；worker、Observer 用它替换 `basicConfig`，web 在 `_serve()` 开头调用并以 `log_config=None` 启动 uvicorn。`health._revision()` 加一个可选 `env` 参数供日志复用同一校验。没有改任何日志调用点的措辞与键名（R8）。

可逆选择（Agent 自定，一行理由）：

- `OPSPILOT_LOG_FORMAT` 为空串按未设置（json）处理：env 文件里 `KEY=` 通常表示未设置；拒绝启动提示不回显非法取值。
- 拒绝启动文本 `OPSPILOT_LOG_FORMAT must be json or text`，`SystemExit` 退出码 1，与其他 env 拒绝一致。
- handler 不传流时每次输出取当时的 `sys.stderr`（同 `logging.lastResort`）：进程内多次调用 `main()` 的旧测试不会留下指向已关闭流的 handler。
- 未捕获异常记到 logger `opspilot.uncaught`，消息 `uncaught exception thread=<线程名>`（线程名进 `kv`）；线程里的 `SystemExit` 照 stdlib 默认忽略；记日志本身失败时退回 stdlib 默认钩子，traceback 不丢。
- 格式串与参数不匹配（`getMessage()` 抛错）时 `msg` 取原始格式串、不出 `kv`，保持一行 JSON；stdlib 会打印多行 `--- Logging error ---`。
- formatter 是全函数（review-450 发现 1）：组装一行时任何异常都退回只含固定键的一行 JSON，`msg` 为 `repr(record.msg)`（repr 也失败时写 `<unrepresentable 类型>`），加 `format_error` 字段记异常类型；`created` 非法时 `ts` 取当前时间；含孤立代理字符的行改用 `ensure_ascii` 转义写出。此前参数 `__str__` 抛错、`created` 为 inf/NaN、UTF-8 流写孤立代理字符都会落到 `Handler.handleError`、整条丢失。
- 同一格式串同名 kv 出现多次时后者覆盖；`%r` 的 kv 值同样取 `str()`；非有限浮点取字符串（JSON 不允许 NaN）。
- `tests/conftest.py` 加 autouse fixture，每个测试后撤掉 `configure()` 装的 handler、恢复 root 级别与两个钩子：`test_runtime_roles`、`test_f2_b1_impl`、`test_427_outbox_impl` 在进程内调用 `main()`，不撤销会把 INFO 级别与钩子漏给后续测试（含 pytest 自己的会话级 `threading.excepthook`）。

调用点复核（O6，PR HEAD 上 AST 扫描 `opspilot/` 88 处日志调用（不含 `logs.py` 自身），按进程；结论一行一条）：

- worker 槽：`attempt crashed`（`worker_main.py:215`）只记类型；`attempt refused`/`sweep refused`/`listing refused`/`backlog pass refused` 的 `code=%s` 是 `PersistenceError`，所有构造点只放固定码（`persistence/base.py:318` 等 9 处已逐一核对）。
- worker：`attempt … reason=%s` 的 `RunnerOutcome.reason` 是 `str | None` 固定码；`worker slot … versions=%s` 是版本修订字典；`signal`/`grace` 是数字。无凭据或模型文本。
- 复盘（`knowledge/worker.py`）：`generation crashed` 只记类型；`postmortem … code=%s version=%s` 是固定码与整数版本；`input over limit` 只记 limit；`event not appended` 只记类型。
- 模型客户端（`investigation/client.py`）：本身无日志；4xx/5xx 只抛 `ModelError(<码>)`，哨兵测试证实 401 响应体里的 provider 文本与 key 不进异常；`opener.open` 抛出的非 `URLError/OSError/HTTPException` 异常会原样冒出，被槽/复盘边界截住只记类型。
- tracing：`trace export disabled … %s` 只拼 `check_lab_target` 的固定码；`trace export raised` 只记类型；`OPSPILOT_TRACE=%r is not a mode` 回显运维填写的模式值（不是凭据，照旧）。
- Observer：`observer owner=… prometheus=%s` 只含 scheme/host/port（带 userinfo 的 URL 拒绝启动）；`sample/comparison crashed`、`poll hook failed`、`reading construction failed`、`replay failed`（`observer/replay.py:1160`）只记类型；`profile unavailable … error=%s` 是 `PersistenceError` 码；其余是 ID、计数与状态码。
- 共用：`health probe failed` 只记类型；`health.py:508` 记绑定地址与 errno；`outbox cleanup refused: %s` 是 `PersistenceError` 码。
- web `service.py:566,1656`：`PersistenceError` 码。`service.py:1576`（review-450 发现 2，F8L-Q2 源头修正）：原先对没有 `.code` 的 pydantic `ValidationError` 记 `str(exc)`，会把输入值（可含 provider 文本或凭据）带进日志；改为经 `_failure_code()` 只记固定码，否则只记异常类型名，`ValidationError` 只加错误个数与每条的 loc+type（不记 input/msg/ctx，非字段名的 loc 段记 `<key>`）；消息措辞与键名不变，只改记录的值。
- web 5 处 `.exception()`（`web/__main__.py:155,159,163`、`service.py:2523,2628`）：保留完整 traceback（R8），现在是一行 JSON 的 `exc`。web 进程不 import 模型客户端；异常来自存储（psycopg 消息带 host/port，不含密码，带密码 DSN 哨兵已实测）、回放计算与图表渲染（输入是入库时已脱敏的证据）。
- `schema.py:642` 多行列差异与迁移 `0024` 的 PG notice 只在 schema/migrate 路径出现，内容是列定义；现在是一行 JSON。
- 未捕获异常钩子（Q5）：worker/复盘/Observer 的模型与 Prometheus 异常在各自 `except Exception` 边界被截住，到不了钩子；能到钩子的是启动期存储失败（实测为 `PersistenceError: TIMEOUT` 链 psycopg `PoolTimeout`，不含 DSN 密码）与循环线程里的存储异常。

实现者测试 `tests/test_386_logs_impl.py`（58 条）：行形状与键序、revision 校验、单行转义、`exc`/`stack`、不脱敏、kv 规则与反例、text 回退、非法取值拒绝且不改状态、幂等与保留他方 handler、两个钩子、主动 `SystemExit` 保持文本、三入口子进程（不可达 DSN：`psycopg.pool` 与 `opspilot.uncaught` 行均为 JSON，带密码 DSN 哨兵不出现；非法格式；text；缺 DSN）、uvicorn 行同格式且只在 stderr、`_serve()` 传 `log_config=None`、按进程的哨兵（worker 槽、模型客户端 401、复盘、Observer 采样、web 自愈 `.exception()`）。

定向验证（2026-11-01，worktree 本地，未起 PG）：`ruff check .`、`ruff format --check .`、`mypy`（`opspilot/` 151 文件）通过；`tests/test_386_logs_impl.py` 加 11 个相关文件同一进程跑 370 passed（56.5s）。加 conftest fixture 之前，这组合跑会卡在 `test_m1_web_worker.py::test_stop_ends_the_batch_before_the_next_claim_and_the_loop_returns`：泄漏的 INFO 级别让槽线程变慢，暴露了该测试原有的竞态（单槽时它依赖第一个 attempt 线程在轮询走到 b 之前结束），在 `2e9db380` 上单独跑这两个文件能过。未跑全量 pytest/`make check`、`tests/acceptance`（kind 窗口约束）。

未确认：`service.py:1576` 的 pydantic 校验消息里可能出现的输入值类型；uvicorn 记录 ASGI 应用异常时的 traceback 内容（web 请求处理异常，未逐路径核对）；`socketserver` 在健康监听连接级错误时直接向 stderr 打多行 traceback，不经 logging（非 JSON，内容为 socket 错误）；Docker 宿主 json-file 轮转；上游 HolmesGPT 日志格式。

Compose 抽检（F8L-Q3/O8，2026-11-02 00:00–00:03Z，持 `~/.agent/locks/kind-lab`；集成运行，不是生产证明）：

- 栈 `opspilot-393`（colima `m1-kind`，`compose.yaml`，`fixture` 工具档），`install.sh upgrade` 从本分支 HEAD `b713ea25` 构建 `opspilot:local`；沿用已安装栈的 `worker_env`（未打开）。原始记录在仓库外 `~/.cache/opspilot-386-logs/`。
- 00:00:23Z 第一条合成告警用未登记标签，intake 答 `handoff_created`、`TARGET_UNRESOLVED`，不建 Run；00:00:30Z 第二条用 `target_identities.json` 里 `checkout-prod` 的标签，建事故 `def8ea51…`、Run `7f3b8f97…`。
- 真实模型调用 00:00:30–00:00:54Z：worker `/metrics` `opspilot_model_calls_total{category="ok"} 3`，无 401；Run 以 `INCOMPLETE_INVESTIGATION` 交接（fixture 档，同 #393/#414）。
- 00:00:59Z `install.sh compose stop postgres`，00:01:59Z `start`；refusal 行到 00:02:10Z 为止，之后无新错误。
- `install.sh compose logs` 三服务（web 37 行、worker 53 行、observer 42 行），加停栈后 `docker logs` 全量（web 41、worker 78、observer 63）：**每行都能 `json.loads`，前 6 个键依次为 `ts level logger msg version revision`，`revision` 均为 `b713ea256e26…`，没有非 JSON 行**；出现的 logger 有 `opspilot.worker/postmortem/observer/health`、`__main__`（web）、`psycopg.pool`、`uvicorn.error`、`uvicorn.access`（R6 生效，web 的 `opspilot.health` INFO 行开始出现）。
- 人工核对：`sk-`、`bearer`、`api_key`、`authorization`、`password`、`deepseek`、`token=`、`postgresql://` 在全部日志中 0 命中；消息只有 ID、固定码、状态与 psycopg 的 host/port；无模型输出文本。web 3 处 `.exception()` 在停 PG 时触发，`exc` 是一行 JSON（31–34 行 traceback），末行 `PersistenceError: TIMEOUT`，链上是 `psycopg_pool.PoolTimeout`，不含 DSN。
- 收尾：先按原计划停栈（`install.sh compose stop` 不停 `agent` profile 的 worker/observer，补 `--profile agent stop`），随后按 lead 更正（栈要留给 #428）00:08Z 重新 `compose start postgres` 与 `compose --profile agent start web worker observer`（依赖链重跑 migrate/init-roles，均 exit 0）。最终状态 00:08:56Z：web/worker/observer/postgres 均 healthy，三个 `/readyz` 200，镜像 `opspilot:local` = `sha256:8bfc5078…`（revision `b713ea25`），卷 `opspilot-393_pgdata` 保留，重启后日志 18 行全部 JSON。`target_identities.json` 未改动，sha256 前后均 `1714ab7b…`。事故 `def8ea51…`（checkout-prod）经工作台 control 路径 `close`（generation 0→1）；`4dc7065b…`（未登记目标的 handoff 事故）页面不提供 close，保持开放。
- 告警令牌照 #393/#414 先例只在触发脚本内存里读用，未打印；`worker_env`、模型 key 未读取（K2）。费用：3 次真实调用，余额差待对账。

摘录（原样，UUID 未改）：

```text
{"ts": "2026-11-02T00:00:54.038Z", "level": "INFO", "logger": "opspilot.worker", "msg": "attempt incident=def8ea51-800d-52e7-af1e-4db48325a300 status=handed_off reason=INCOMPLETE_INVESTIGATION", "version": "0.1.0", "revision": "b713ea256e266d5303bef6eda24f508641d59a69", "kv": {"incident": "def8ea51-800d-52e7-af1e-4db48325a300", "status": "handed_off", "reason": "INCOMPLETE_INVESTIGATION"}}
{"ts": "2026-11-02T00:00:30.559Z", "level": "INFO", "logger": "uvicorn.access", "msg": "172.20.0.1:55120 - \"POST /intake/alertmanager HTTP/1.1\" 200", "version": "0.1.0", "revision": "b713ea256e266d5303bef6eda24f508641d59a69"}
{"ts": "2026-11-02T00:01:05.766Z", "level": "WARNING", "logger": "opspilot.worker", "msg": "sweep refused code=TIMEOUT", "version": "0.1.0", "revision": "b713ea256e266d5303bef6eda24f508641d59a69", "kv": {"code": "TIMEOUT"}}
```

## 下一步与交接

- 第 1 实施步骤：PR-1 #413（PR #418）与 PR-2 #414（PR #421）均已合并。
- 结构化日志：合同 r2 + r2.1 已裁决；2026-11-01 用户要求提高并发后提前开工（原排 #428 合并后），实现在 `feature/F8-386-structured-logs`（见「结构化日志实现」）；Compose 抽检已做（2026-11-02，全部行为 JSON、无凭据与 provider 文本）；独立验收测试 31 项通过，待独立审查。
- #243 outbox：合同 r3 已裁决；#427（PR-1 写入侧）→ #428（PR-2 投递侧）。
- 第 3 步升级回滚演练：合同 r3 已裁决；#429，等 #427 合并（trace 证据等 #428）。
