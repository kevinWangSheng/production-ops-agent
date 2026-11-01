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

## 下一步与交接

- 第 1 实施步骤：PR-1 #413（PR #418）与 PR-2 #414（PR #421）均已合并。
- 结构化日志：合同 r2 + r2.1 已裁决，2026-11-01 Q1 转为执行；排在 #428 合并后的首条空闲代码线（Q4），Compose 抽检并入 F14 第 3 步 kind 窗口（Q3）。
- #243 outbox：合同 r3 已裁决；#427（PR-1 写入侧）→ #428（PR-2 投递侧）。
- 第 3 步升级回滚演练：合同 r3 已裁决；#429，等 #427 合并（trace 证据等 #428）。
