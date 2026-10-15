# OpsPilot M1 端到端演示：从告警到批准的复盘知识

OpsPilot 是只读的事故调查 Agent。它不修改目标系统，只在三处产生结果：调查报告、独立的恢复观察结论、经人审核后发布的复盘知识。M1 的完整路径在一次真实运行中走通过，证据见 [M1 退出端到端运行记录](../evidence/m1-exit-e2e/run.md)（2026-10-15）。

## 系统做了什么

1. **告警进来**：kind 实验环境里的 OTel Demo 被注入支付故障（flagd `paymentFailure` 100%），Prometheus 规则触发，Alertmanager 以 webhook 推给工作台（Bearer token 认证），工作台按告警标签匹配目标、建事故和调查 Run。
2. **调查**：常驻 worker 领取 Run，用真实模型（DeepSeek `deepseek-flash`）调用只读工具查 Prometheus 指标和 Jaeger trace，写出带证据引用的报告并发布结论。本次 8 次模型请求、19 次工具调用，定位到 payment `Charge`「Invalid token」导致 checkout 下单失败，根因机制标为未确立（工具读不到 flag 配置）。
3. **自动恢复观察**：结论发布后，web 进程内的循环以 `system` 身份自动授权一个观察会话，不改人工控制代际。独立的 Observer 进程用自己的数据库角色和 Prometheus 账号，按 HealthProfile 每 60 s 采样 8 个信号（副本、请求量、错误率、p95 延迟、依赖错误率等），连续健康 600 s 才确认恢复；次数或期限用完仍未确认就把事故交还给人。
4. **复盘与知识**：观察结束后 worker 自动生成复盘草稿，每条结论的证据引用都要通过校验，才进入待审核。审核人在工作台批准后，草稿里提议的知识条目发布为带版本的知识 revision。

人工随时可以暂停、接管、登记处置、追问，人工动作优先于自动流程（本次演示全程无人工控制，只有最后的审核批准）。

## 复现步骤

前提：macOS + colima；已 `make setup`；`.env` 里有 `DEEPSEEK_API_KEY`（可选 `LANGSMITH_API_KEY` 看 trace）；宿主可回收内存 ≥ 3 GiB，运行期间不要并行跑其他重负载任务（见「已知限制」）。下面的 `<…>` 都是占位符，凭据不写进命令行历史以外的任何文件。

```sh
# 1. 实验环境：kind + OTel Demo + Prometheus/Alertmanager；首次约 20 分钟
.venv/bin/python scripts/kind_lab.py up
.venv/bin/python scripts/kind_lab.py health          # 全部 ok、所有 flag off 再继续

# 2. 空库并迁移到 head（owner 连接只用于迁移）
#    本次是一个新起的 PG 17 临时实例；用 55431 的 lab 也行，但要确认库里没有别的 Run
export OPSPILOT_DSN="host=127.0.0.1 port=<端口> dbname=m0_budget user=<owner>"
make migrate
#    非 owner 运行角色：web/worker 用 postgres_lab runtime-roles 建；Observer 角色在仓库外建
OPSPILOT_LAB_DSN="$OPSPILOT_DSN" .venv/bin/python -m scripts.m0.postgres_lab runtime-roles
#    CREATE ROLE <observer_login> LOGIN IN ROLE opspilot_observer;   -- 组角色与授权来自迁移 0003

# 3. 三个进程，各开一个终端
#  web：读 Prometheus 调查账号与 Alertmanager token 哈希两个 env 文件，加工作台登录哈希
set -a; . tmp/m1-kind-lab/prometheus-investigator.env; . tmp/m1-kind-lab/workbench-alertmanager.env; set +a
OPSPILOT_WEB_DSN="host=127.0.0.1 port=<端口> dbname=m0_budget user=opspilot_web_lab" \
OPSPILOT_TOOL_PROFILE=otel-demo OPSPILOT_TARGET_IDENTITIES=<targets.json> \
OPSPILOT_UI_USERS="<审核人>=<hash-password 打印的哈希>" \
  .venv/bin/python -m opspilot.web serve             # 127.0.0.1:8080，自动观察循环默认开启
#  worker：真实模型 + lab trace
set -a; . tmp/m1-kind-lab/prometheus-investigator.env; set +a
OPSPILOT_WORKER_DSN="host=127.0.0.1 port=<端口> dbname=m0_budget user=opspilot_worker_lab" \
OPSPILOT_TOOL_PROFILE=otel-demo OPSPILOT_ENV_FILE=$PWD/.env OPSPILOT_TRACE=lab \
  .venv/bin/python -m opspilot.worker_main
#  Observer：干净环境，只带自己的 DSN 与账号文件
env -i PATH=/usr/bin:/bin HOME=$HOME \
  OPSPILOT_OBSERVER_DSN="host=127.0.0.1 port=<端口> dbname=m0_budget user=<observer_login>" \
  OPSPILOT_OBSERVER_PROMETHEUS_URL=http://127.0.0.1:19090 \
  OPSPILOT_OBSERVER_ENV_FILE=$PWD/tmp/m1-kind-lab/prometheus-observer.env \
  .venv/bin/python -m opspilot.observer

# 4. 注入故障；约 5–7 分钟后告警到达，工作台自动建事故
.venv/bin/python scripts/kind_lab.py fault inject --experiment-id <id>

# 5. 看到事故进入 observing_recovery（自动授权）后恢复故障
.venv/bin/python scripts/kind_lab.py fault restore --experiment-id <id>
```

`targets.json` 把操作者 target_id 映射到不可变身份（`integration_id`、`cluster_uid`、`namespace`、`workload`、`health_profile_id`）并带告警匹配标签，格式见 [开发指南](../development.md) 第 0004 迁移与三进程流程段。本次运行用一个只读轮询脚本盯数据库状态，看到自动授权就执行第 5 步；脚本、启动脚本与日志都在 git 忽略的 `tmp/m1-exit-e2e/`，哈希列在运行记录里。

之后无需操作：Observer 采样到确认恢复或用完次数，worker 自动生成复盘草稿。最后在工作台批准（或用 [approve.py](../evidence/m1-exit-e2e/approve.py) 以审核人身份提交页面上的批准表单）。

## 在工作台看什么

- `http://127.0.0.1:8080/incidents/<incident_id>`：事故生命周期（`open → observing_recovery → resolved` 或回到 `open`）、调查报告与每条结论的证据、恢复观察段（来源 `automatic`、逐样本信号读数、离线重放是否一致、结局与交接原因）。
- `http://127.0.0.1:8080/postmortems/<postmortem_id>/versions/1`：复盘草稿、每条结论的引用、提议的知识条目；状态为 `under_review` 时有批准/驳回/退回表单。
- LangSmith：`opspilot-lab-<round>` project 里的 `opspilot.run` 根 run，可以逐个看模型请求与工具调用。

## 已知限制

- 本次运行恢复未确认：故障 1 秒内就恢复了，但宿主内存压力让 checkout p95 两度超过 500 ms，40 次采样用完时健康窗只有 421 s，事故按合同交还给人。演示时看到 `open` + 交接是合法结局，不是崩溃。
- 实验流量极低（约 0.01–0.05 req/s），p95 由少数请求决定，恢复判定对偶发慢请求很敏感。
- 复盘输入超限：40 次采样的观察会让复盘输入超过 256 KiB 而失败，修复在 PR #240（本文写时未合并）；在 `main` 上复现会停在 `INPUT_TOO_LARGE`。
- 这是 kind 实验环境上的有界运行，不是生产证明；Prometheus basic auth 对所有账号权限相同；调查侧角色隔离是增量的，Controller/Gateway 全隔离未做（#197）。
