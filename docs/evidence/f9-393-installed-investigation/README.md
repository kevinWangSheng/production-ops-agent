# #393 安装后的栈跑通一次真实调查（2026-10-31）

类别：集成运行（本机 colima `m1-kind` VM，Compose 安装，真实 DeepSeek 模型，`fixture` 工具档）。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)；没有原始 ledger、没有凭据值。

## 做了什么

| 时间 (UTC) | 步骤 | 结果 |
|---|---|---|
| 09:09:55–09:10:29 | 第一阶段：`install.sh install`（项目 `opspilot-393`，web 端口 18180，密钥目录 `~/.cache/opspilot-393-run/secrets`），写 Observer 只读账号，起 Observer 并 `docker network connect kind` | web 登录 HTTP 200；Observer 用自己的账号查 `http://opspilot-m1-control-plane:30090` 返回 200；未放 key 时 `OPSPILOT_START_AGENT=1 upgrade` 被预检拒绝、不改动 |
| 12:53:50–12:53:57 | 第二阶段：`OPSPILOT_START_AGENT=1 OPSPILOT_OBSERVER_PROMETHEUS_URL=http://opspilot-m1-control-plane:30090 install.sh upgrade` | 预检通过；顺序 stopping runtimes → postgres → migrate（`schema unchanged: 0023_retention_raw_cleanup`）→ init-roles（4 个运行时登录均在 head）→ starting runtimes: web worker observer；三者 healthy，日志 `worker started`、`observer started` |
| 13:00:16 | kind lab 注入 `paymentFailure`（`scripts/kind_lab.py fault inject`，实验 `f9-393-run2`） | 5 分钟内 checkout ERROR span 约 22.6、payment 约 11.3 |
| 13:03:40 | 第一次合成 Alertmanager 告警 | Run `5732baeb…` 跑到 `waiting_human`，交接 `INCOMPLETE_INVESTIGATION`：目标身份写成 `fixture-393`，fixture worker 只注册 `checkout-prod`，两次工具调用都被拒（`TARGET_NOT_REGISTERED`） |
| 13:14:31 | 把 `target_identities.json` 改为以 `checkout-prod` 为键，`compose restart web`，再发一次告警 | intake `created`，incident `606d9341-22f8-5f99-ad10-f852a892be64`，Run `796fd3a1-1b79-5a74-98a3-dc6ea9259dfd` |
| 13:15:10 | 轮询工作台 `/incidents/<id>` | `run-state` = `completed`，worker 日志 `status=published`；报告在工作台显示 |
| 13:15:18 | 恢复 `paymentFailure` → off | ConfigMap 校验通过，所有 Failure 标志均为 off |

隔离（`docker inspect` 挂载）：worker 只有 `worker_pgpass`、`worker_env`；observer 只有 `observer_env`、`observer_pgpass`；web 只有 `web_event_tokens`、`web_pgpass`、`web_ui_users` 与 config `target_identities.json`。worker 环境 `OPSPILOT_WORKER_CAPACITY=2`、`OPSPILOT_POOL_MAX_SIZE=4`、`OPSPILOT_TOOL_PROFILE=fixture`。镜像 `sha256:7b311f88…`（0.1.0，revision `0aad093c`），三个运行时同一镜像。

## 工作台看到的

- Run `796fd3a1…` `completed`，epoch 1，预算 3 spent / 0 reserved / 1 unknown of 100；当前目标 `checkout-prod`。
- 调查报告：Assessment `completed` · conclusion `partial`，content sha256 `22767fd0…`，2 条事实、1 条假设。模型执行了唯一授权的 `rate(http_errors[5m])`，指出 fixture 样本 `data_as_of` 是 2026-09-14，不覆盖告警窗口，因此不归因；重复查询被 `DUPLICATE_TOOL_CALL` 拒绝；traces/k8s/logs/changes 标为 not_integrated。
- Recovery observation：未授权（只在人工 `register_remediation` 后开始），本次未触发。

注意：`fixture` 档的调查读的是固定 fixture 视图，lab 里注入的真实故障只在 Observer 侧的 Prometheus 可见，调查本身看不到；这符合 #393 合同（fixture profile）。

## 未做 / 局限

- LangSmith trace：无。产品 compose 没有 `OPSPILOT_TRACE`；产品 trace 属 #243（outbox）范围；本次没有 trace 链接。
- 费用：前后 DeepSeek 余额 Agent 未观察（K2，Agent 不接触 key），由用户对账。
- opt-in live 测试（`OPSPILOT_393_LIVE=1`）未运行：两份测试都需要把 DeepSeek key 放进 Agent 进程或复制它，与 K2 冲突。
- 第一次 Run（incident `9869b041…`）留在 `waiting_human`，未人工关闭。
