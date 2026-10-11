# #169 有界真实运行：web、worker、Observer 各用非 owner 登录角色

- 日期：2026-10-11 09:44–10:20Z；分支 `feature/m1-pg-runtime-roles`，commit `0d263a3`（运行期间工作区干净，本目录为运行后新增）
- 合同：任务记录草稿 r1（U7/U9：一次有界真实 Run，web、worker、Observer 全部用非 owner 角色，证明授权完整）与公开接口 r2–r5
- 冻结摘要：[live-runs/8e8ad43b-0b9e-5545-8b19-a3c202bc7855/summary.json](live-runs/8e8ad43b-0b9e-5545-8b19-a3c202bc7855/summary.json)。原始 ledger（Run 快照、恢复与复盘投影全文、F13 审计行、Observer 实测权限）不入库，sha256 `a9fc1a6b…e24fafefc`，785496 字节（ADR-0006）
- 类别：有界真实软件环境运行加真实模型调用。告警走真实路径 Prometheus 规则 → Alertmanager → 工作台。这不是生产证明
- 执行者：未参与实现的新上下文 Agent
- **结论：第 1–3 项通过；第 4 项失败**。复盘生成在调用模型前被产品输入上限 `INPUT_TOO_LARGE` 拒绝，原因与权限无关，所以没有版本可审，web 审核动作没有执行。全程 PG 日志里 `permission denied` / 42501 为 0

## 环境

- PostgreSQL 17.9：本 worktree 新建 `tmp/pg-live`，端口 55481，超级用户 `m0_lab`，库 `m0_budget`。日志设置为 `log_connections=on`、`log_disconnections=on`、`log_min_error_statement=error`，`log_line_prefix` 含 user、application_name、SQLSTATE。运行结束后执行 `pg_ctl -D tmp/pg-live stop`
- owner 只用在下面几处：`make migrate`（空库 → `0011_runtime_roles`）；`python -m scripts.m0.postgres_lab runtime-roles`（`OPSPILOT_LAB_DSN` 指向 55481，建 `opspilot_web_lab ∈ opspilot_web`、`opspilot_worker_lab ∈ opspilot_worker`）；`CREATE ROLE opspilot_observer_lab LOGIN IN ROLE opspilot_observer`；运行中和运行后的只读 SELECT（application_name 为 `lab-readonly` / `lab-monitor`）
- 三个进程都用 `env -i` 启动，DSN 各带 application_name：
  - web：`python -m opspilot.web serve`，`OPSPILOT_WEB_DSN` 的用户是 `opspilot_web_lab`，监听 127.0.0.1:8080（启动前确认端口空闲）。配置为 `OPSPILOT_TOOL_PROFILE=otel-demo`；`OPSPILOT_EVENT_TOKENS` 取自 `workbench-alertmanager.env`，只含哈希；实验 Basic Auth 账号 `pgroles-lab`；目标身份文件只有一条 `m0-otel-20260909`（`cluster_uid=kind-opspilot-m1`，`match.labels={cluster: opspilot-m1, namespace: otel-demo, service: checkout}`）
  - worker：`python -m opspilot.worker_main`，`OPSPILOT_WORKER_DSN` 的用户是 `opspilot_worker_lab`。同一 profile，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号。版本为 `prompt-replay-candidate-bdb4914edefa` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-d61d6f2a3b04`
  - Observer：`python -m opspilot.observer`，`OPSPILOT_OBSERVER_DSN` 的用户是 `opspilot_observer_lab`，Prometheus 用 observer 账号，`POLL_SECONDS` 用默认值
- 实验环境：kind `opspilot-m1`，Alertmanager v0.28.1，webhook 目标为 `192.168.5.2:8080`。注入前 frontend 的 2 分钟 span rate 为 1.27/s，没有重启 otel-collector。故障钩子用本 worktree 的 `scripts/kind_lab.py`，kubeconfig 复制到本 worktree 被忽略的 `tmp/m1-kind-lab/`，没有写主仓库
- trace：round `m1-pg-roles-live-20261011`，project `opspilot-lab-m1-pg-roles-live-20261011`。运行前用 `lab_evidence.prepare_lab_project` 建立 project，回读到 `trace_tier=longlived`
- 防循环上限：沿用产品默认，每个 Run 最多 100 次模型请求，每次复盘生成最多 2 次请求

## 时间线（UTC）

| 时间 | 事件 |
|---|---|
| 09:45:07 | `make migrate` → `0011_runtime_roles`，随后建三个登录角色 |
| 09:45:45 | 负向启动：只设 `OPSPILOT_DSN`（owner）时 web、worker 都以退出码 1 退出，没有打开 PG 连接 |
| 09:46:18 | web、worker、Observer 启动；worker 日志显示 `trace export enabled mode=lab` |
| 09:46:36 | `fault inject --experiment-id m1-pg-roles-live`：ConfigMap 由 `1ee5c025…` 变为 `e47d27c2…`，`verified: true` |
| 09:51:54 / 09:52:03 | 告警 `startsAt` / 工作台收到 firing，返回 200，建出事故 `b23a19fd-cf49-5e1b-bd03-a3a89973e3fe` 与 Run `8e8ad43b-…` |
| 09:52:05–09:53:28 | worker 调查：6 次模型请求、15 次工具调用，`status=published` |
| 09:53:35 | `fault restore`：ConfigMap 回到 `1ee5c025…`，`verified: true` |
| 09:53:36 | 用实验账号在工作台登记处置，返回 200（generation 0 → 1），授权观察会话 `51efe0ed-…` |
| 09:56:03 | 工作台收到 resolved，`resolved_attached` |
| 09:54:39–10:08:43 | Observer 采样 15 次（2 次 degraded，13 次 healthy），10:08:43 `recovery_confirmed`，事故变为 `resolved` |
| 10:08:49 | worker 领取复盘生成：`failed` / `INPUT_TOO_LARGE`，模型请求 0 |
| 10:18:34 | 按 PID 用 SIGTERM 停 web、worker、Observer 与采样器，都正常退出 |
| ~10:20 | 只读投影、trace 回读、冻结摘要，之后 `pg_ctl stop` |

## 结果

| 项 | 预期 | 实际 | 判定 |
|---|---|---|---|
| 1 告警接入 | 真实告警 → `/intake/alertmanager` 建事故与 Run（web 角色） | 投递 1：`firing` / `created`，`actor=alertmanager`，fingerprint `83f7541ae0105d42`；投递 2：`resolved` / `resolved_attached`（identity 列 INSERT 成功） | 通过 |
| 2 调查 | worker 领取，用真实 `deepseek-flash` 跑完，得到报告或交接 | Run `completed`，`decision=report_available`，没有交接，13 个证据 ID，`actions=[read_only_query]`。报告 `assessment_status=completed`、`conclusion=partial`：错误来自 payment `Charge` 的「Invalid token」，payment 为何拒绝没有证据（摘要 `investigation.conclusion`）。结论已发布，不是交接 | 通过 |
| 3 恢复 | restore；web 登记处置；Observer 读真实 Prometheus 给出判定 | restore 已校验；登记 200；15 次采样，`recovery_verdict=healthy`，`recovery_confirmed=true`，`healthy_window_seconds=602`，`observation_ended_reason=recovery_confirmed`，生命周期 `resolved`。actions：record_handling 1、advance_incident_lifecycle 2、read_only_query 360、persist_observation 15 | 通过 |
| 4 复盘与审核 | worker 用真实模型生成草稿 → web 执行一次审核动作 | **失败**。worker 领取生成任务并写下尝试行（没有 42501），但 `build_input` 在调用模型前抛出 `InputTooLarge("MAX_VIEW_BYTES")`：13 个已采纳证据视图中，有一个 `traces_search` 视图投影后为 49521 字节，超过 `MAX_VIEW_BYTES = 32768`（`opspilot/knowledge/generation.py:76,348`）。页面显示 `generation_failed`，「Generation stopped at this watermark after 3 failed attempts」。没有版本可审，所以 web 审核动作未执行，worker 写版本、web 写审核审计这两条路径没有真实证据 | 失败（与权限无关） |
| PG 日志 | 来自三个进程的 `permission denied` / 42501 为 0 | 整份日志 1194 行（09:44:59 起，到摘要读取之后）：ERROR 0、FATAL 0、`sqlstate=42501` 0、`permission denied` 0，SQLSTATE 非 `00000` 的行 0 | 通过 |
| 连接身份 | 各进程以自己的登录角色连接，web、worker、Observer 都不以 owner 连接 | `pg_stat_activity` 每 20 s 一次快照，共 96 次（09:46:18–10:18:20）：只出现 `opspilot_web_lab/opspilot-web`、`opspilot_worker_lab/opspilot-worker`、`opspilot_observer_lab/opspilot-observer`，带 `opspilot-*` 应用名的 owner 会话为 0。另在 10:10Z 把 `client_port` 与各 PID 的 `lsof` 对照：web PID → 64869（web 角色），worker PID → 62189/62190（worker 角色），Observer PID → 62185/62186（observer 角色）。这项对照不依赖客户端自报的 application_name | 通过 |
| 负向启动 | 只设 `OPSPILOT_DSN` 时 web、worker 拒绝启动 | web 退出码 1，stderr `OPSPILOT_WEB_DSN is required`；worker 退出码 1，stderr `OPSPILOT_WORKER_DSN is required`；该时段 PG 日志没有新连接 | 通过 |
| trace | LangSmith 可见 | 调查 [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/704ad7ef-672b-48a6-832b-9e0ae976d743/r/00000000-0000-0000-edf0-819d41422b6d?poll=true)：OTel `8cbc609c390a614b5503211e1ba2ac78`，子 run 为 llm 6、tool 15，与库里的 `budget_spent=6`、`tool_operations_used=15` 一致，token 126918 输入 / 20017 输出。复盘生成 [根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/704ad7ef-672b-48a6-832b-9e0ae976d743/r/00000000-0000-0000-6028-48ec01689b4c?poll=true)：OTel `ad3565b867525265c0208c3290f5e57e`，0 次 llm 调用（在调用模型前失败） | 通过 |

按 user / application_name 统计的连接数：web 5、worker 4、Observer 2（另有 4 个来自运行后 `table_privileges()` 的只读测量，应用名 `lab-observer-privileges`）；owner 只有 migrate 3（其中 1 个没有应用名，发生在 migrate 期间）、psql 4、`lab-monitor` 97、`lab-readonly` 159。

## 观察到但不属本项判定

- 第 4 项失败的根因是 D21 的单视图上限：真实调查的 `traces_search` 视图大小通常就会超过 32 KiB，所以带真实调查的事故目前无法生成复盘。M1-03 的真实验收用的是没有调查证据的 F6 归档事故，这个问题当时没有暴露。需要另开 issue（未开，等 lead 决定）。
- Observer 的实测权限出现 `database_temp`，这是 PUBLIC 默认的数据库 TEMP 权限。按 r3 Q4，它属于部署层，本次没有 `REVOKE TEMP`。
- 事故行 `state` 列仍是 `queued`，与 M1-04 的观察相同，未核查。

## 失败与未覆盖

- 第 4 项失败，见上。worker 写复盘版本，以及 web 的批准、退回等审核写路径，在本次真实运行中没有执行。它们在非 owner 角色下的合法性只有确定性集成测试覆盖，没有真实证据。
- 本次没有重试，没有改代码。实验环境动作只有 inject / restore 两次，都已校验，flag 已回到注入前的 sha256。

## 费用

DeepSeek 余额从 3.58 降到 3.46 CNY，差 0.12 CNY，分别取于 09:45:37Z 和 ~10:20Z。同一账户在此期间可能有其他会话调用，所以余额差是本次费用的上界。token 为 126918 输入 / 20017 输出（调查根 run）；复盘生成没有调用模型。

## 凭据

证据中没有 token、口令或 API key。gitleaks 8.30.1 对本目录做 `dir` 扫描，结果是 `no leaks found`。另把私有 env 文件、Prometheus 账号文件、webhook token 和实验 UI 口令中的每个值，逐一在本目录做字面匹配（不打印原值）：DeepSeek / LangSmith key、webhook token、各 Prometheus 口令、UI 口令都是 0 次。命中的只有三类非机密值：LangSmith 组织 ID（trace 链接里的 `/o/…`，与已入库的 M1-04 证据相同），以及 Prometheus 账号名 `investigator`、`observer`（开发指南已公开）。
