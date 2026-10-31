# M3 运行可观测（F8 第 1–3 步）

- 状态：进行中（第 1 实施步骤合同 r1 已裁决；PR-1 产品 #413 已实现，待独立验收测试合入与审查）
- 更新日期：2026-10-31
- 依据：[M3 开放决策](2026-10-27-m2-exit-m3-opening-decision.md) Q2 第 4 条、Q5、Q6；feature_list.json F8；C3 §11（可观测性、outbox、部署与 schema 迁移）；#243 #159
- 工作区：#413 `/Users/shenghuikevin/dev/AI/poa-413-impl`，分支 `feature/F8-413-health-listeners`（基 main `659bcd9c`）

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

切分：PR-1 产品（health 模块 + 三入口接线 + 模型调用计数包装器 + Compose healthcheck 与 lab override + 测试调整）→ PR-2 lab 抓取与规则 + 两次演练（PR-1 合并后）。之后各自预审：结构化日志、#243 outbox、升级回滚演练（第 3 步）。

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

## 下一步与交接

- 第 1 实施步骤：PR-1 产品 #413 → PR-2 lab 与演练 #414（被 #413 阻塞）。
- 结构化日志、#243 outbox、第 3 步升级回滚演练：PR-2 落地后各自预审。
