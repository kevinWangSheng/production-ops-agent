# F7 第 1 步：lab Prometheus 只读代理部署与授权探测（S1b、S1d、S1f）

- 时间：2026-10-16T16:34Z 部署，16:37–16:40Z 探测。不注入故障、不调用模型。协调者批准的 lab 时段，持共享重负载锁。
- 部署：本分支 `scripts/kind_lab.py up`（共享 lab 目录 `production-ops-agent/tmp/m1-kind-lab` 以符号链接接入 worktree）。结果：`prometheus` Service 改为 ClusterIP，`prometheus-readonly-proxy`（`nginx:1.28.0-alpine`）占 NodePort 30090（宿主 19090）；auth 文件只补了 `proxy` 账号，原四个密码与 env 文件不变；Prometheus Pod 未重启（web 配置文件每次请求重读）；`up` 末尾 `health` 为 `ok: true`（lab 账号经代理读成功，说明 nginx alpine 接受 `$2y$` bcrypt）。部署后 span 指标样本持续写入（`max(timestamp(traces_span_metrics_calls_total))` 距当前约 10 秒），collector 写入链路仍通；otel-collector 在 `up` 期间重启 2 次后 Running。
- 探测：`scripts/f7_authz_probe.py`，账号 investigator、observer、lab 与匿名，凭据只从 env 文件读。

| 运行 | 文件 | 结果 |
|---|---|---|
| 1 | [probe-run1.json](probe-run1.json)（sha256 `56e5fea6…`） | `ok: false`：136 个请求中 18 个不符——三个账号对 6 个读路径发 HEAD，期望 200，实得 405。其余全部符合：读 GET 200；OTLP、remote write、admin、lifecycle、federate、POST 查询、OPTIONS 403；5 种路径变形全为 403；匿名 401；`opspilot_f7_probe` 无新样本 |
| 2 | [probe.json](probe.json)（sha256 `ae2110c9…`） | `ok: true`，136/136 符合 |

运行 1 的不符是探测期望错误，不是授权缺口：代理按 A4 放行 HEAD，Prometheus API 本身没有 HEAD 处理，返回 405（请求到达上游但不是读也不是写）。修正：读路径的 HEAD 期望改为 405，并改对应单测替身；修正后只重跑一次（运行 2）。

## 判据结论

产品两个只读账号与 lab 账号经 19090 只能 GET 读接口；#249 中返回 200 的 OTLP 写入现在返回 403，admin/lifecycle/remote write/federate 同样 403，宿主上不再有直连 Prometheus 的端口。第 1 步「外部授权拒绝」对 Prometheus 通道成立；PostgreSQL 通道沿用 `docs/evidence/m1-pg-roles-live/`；web 入口与 Kubernetes 不适用由 `tests/acceptance/test_f7_step1_entry_points.py` 断言。

未确认：OTel Demo 的 frontend-proxy（18080）是否有面向浏览器遥测的匿名 OTLP 入口（经 collector 进入 Prometheus）。那不是产品凭据的路径，本次未探测。

## 与 main 版工具的兼容

lab 共享 auth 文件现有 5 个账号。main 版 `scripts/kind_lab.py`（本 PR 合并前）读 auth 文件时要求恰好 4 个账号：设置了 `OPSPILOT_LAB_PROMETHEUS_*` 时 `health` 不读该文件、可用；未设置时会以 `does not list exactly` 退出；main 版 `up` 也会拒绝运行（若绕过则会把 Prometheus 改回 NodePort 30090，与代理冲突）。F11 U2 harness 用 `OPSPILOT_LAB_PROMETHEUS_*` 环境变量经 `kind_lab.PROMETHEUS`（19090）读，走代理可用。产品 observer/investigator env 文件未变，探测已证明二者经代理读 200。本 PR 合并前，lab 的 `up` 只能用本分支脚本。
