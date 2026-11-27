# 运维

[English](operations.md) | 简体中文

日常如何运行一个 Compose 安装。安装步骤见 [README](../../README.zh-CN.md#用-docker-compose-安装)；升级与回滚顺序见 [deploy/compose/UPGRADE.md](../../deploy/compose/UPGRADE.md)（英文）。下面每个流程都只在 kind lab（合成流量、注入故障）中演练过；链接的证据说明了在哪里、演练了几次。

所有命令都以安装用户身份在仓库检出目录中运行。`install.sh compose ...` 会把参数传给 `docker compose`，并带上正确的项目、文件和密钥目录。

## 健康与状态

| 检查项 | 方法 | 「健康」是什么样 |
|---|---|---|
| 容器 | `deploy/compose/install.sh compose ps` | `web`、`worker`、`observer` 处于运行状态且为 `healthy`（每个 healthcheck 读取自己在容器端口 9464 上的 `/readyz`） |
| 就绪 | 各运行时的 `/readyz` | `200 {"status": "ready", "version": "<product version>"}`；schema 不在 head 或运行时的轮询循环停滞时失败 |
| 指标 | 各运行时的 `/metrics`（只有 lab 覆盖文件 `compose.lab.yaml` 会把它发布到主机） | `opspilot_queue_depth`、`opspilot_model_calls_total{category}`、`opspilot_model_consecutive_failures`、`opspilot_last_poll_age_seconds`、`opspilot_schema_at_head`、`opspilot_build_info`、trace-outbox 相关 gauge |
| 日志 | `deploy/compose/install.sh compose logs worker`（或 `web`、`observer`） | 带固定代码和 id 的结构化 JSON 行；不含密钥 |
| 工作 | 工作台事故列表 | Run 依次经过 `queued → running → completed`，或带原因进入 `waiting_human` |

自身告警：lab 附带两条关于 OpsPilot 自身的 Prometheus 规则 `OpsPilotAgentAbsent` 和 `OpsPilotModelCallsFailing`，路由到单独的 receiver，因此它们永远不会在它们所针对的那个工作台里开出事故（[scripts/kind_lab/values.yaml](../../scripts/kind_lab/values.yaml)）。非 lab 安装需要在自己的 Prometheus 里加上等效规则；产品不会安装它们。

## 常见情形

| 情形 | 自动发生什么 | 运维人员做什么 | 证据 |
|---|---|---|---|
| worker 停止或被杀 | 排队的 Run 等待；2 分钟后触发 `OpsPilotAgentAbsent`；超期的 Run 被清扫停放为 `DEADLINE_EXCEEDED` 交接。执行中被中断的 Run 在租约到期后从已提交的行恢复（最多一个租约时长，记录的那次运行约 7 分钟） | 重新启动：`deploy/compose/install.sh compose start worker`。被 `docker kill` 停掉的容器，`unless-stopped` 重启策略没有把它重启 | [f8-step1-drills](../evidence/f8-step1-drills/README.md) 演练 1；[f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) 第 3 步 |
| 模型供应商不可达 | 连续两次调用失败后，Run 以 `MODEL_UNAVAILABLE` 交给人；触发 `OpsPilotModelCallsFailing` | 恢复出口网络或 key，然后在工作台对该事故执行 `resume`（交接不会自动重试） | [f8-step1-drills](../evidence/f8-step1-drills/README.md) 演练 2 |
| 需要让 Agent 停止查询 | — | 在工作台 `Controls` 页面全局或按目标暂停。暂停会阻止新的工具查询、Observer 采样和模型调用；取消暂停不会恢复旧的工作，每个事故需要显式恢复 | F7 第 4 步（[ROADMAP](../../ROADMAP.md) F7 行） |
| Run 已交接 | 事故保持打开，处于 `waiting_human` | 阅读交接原因，在 OpsPilot 之外处理系统，在事故页登记修复以便开始恢复观察，或者追问（会开启新 Run） | F12（[证据](../evidence/f12-live/)） |
| 告警没有匹配到目标 | 事故交给人，不启动 Run | 把目标加入 `$SECRETS/target_identities.json`，然后执行 `install.sh compose up -d --force-recreate web` | README 第 5 步 |
| Observer `/readyz` 短暂返回 503 `POLL_STALE` | 在一次较慢的恢复采样进行时出现 | 暂无处理；在 [#522](https://github.com/kevinWangSheng/production-ops-agent/issues/522) 跟踪 | [m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md) |

## 升级与回滚

- 升级：`OPSPILOT_IMAGE=ghcr.io/kevinwangsheng/opspilot@sha256:<digest> deploy/compose/install.sh upgrade`（拉取的镜像必须按 digest 钉住），或用 `git pull && OPSPILOT_IMAGE=opspilot:local deploy/compose/install.sh upgrade` 从检出目录重新构建（镜像设置会记在 `install.env` 中，所以不带这个设置时，上次运行拉取 digest 的安装会继续使用那个镜像）。安装器会停止所有运行时、执行迁移、检查登录，然后重新启动它们；不支持滚动升级。
- 回滚：按 [UPGRADE.md](../../deploy/compose/UPGRADE.md#rolling-back)（英文）中的步骤。两个版本的 schema head 相同时，跳过 schema 降级步骤，UPGRADE.md 现在也这样写（即使执行，它也会以 1 退出且不做任何改动）。回滚到已发布的 digest；本地重新构建的 `opspilot:local` 不是钉住的目标。
- 在 lab 中，每次 `upgrade` 之后都要再执行一次 `python3 scripts/kind_lab.py connect-control --project opspilot`，让各运行时重新加入 lab 网络。
- 证据：在干净主机上，进行中的工作经历了镜像升级、回滚、再升级后仍然完整（[f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) 第 4 步，两边 schema head 相同）；在本地 VM 上做过一次跨 schema 的升级与回滚 `0023 → 0024`（[f8-step3-upgrade-rollback](../evidence/f8-step3-upgrade-rollback/README.md)）。

## 数据

- 数据库卷 `<project>_pgdata` 和密钥目录要一起备份；卷里的登录只能配合该目录中的密码使用。不附带备份工具（未确认：备份与恢复没有演练过）。
- 原始 payload 保留：`deploy/compose/install.sh compose run --rm retention` 报告会被清理的内容（关闭超过 30 天的事故的原始 payload）；要删除，加上 `python -m opspilot.retention --days 30 --apply` 作为命令。清理逻辑在测试套件中针对 PostgreSQL 测试过（#244）；通过 Compose 服务运行它没有演练过（未确认）。
- 产品的 compose 文件不轮转容器日志（[#476](https://github.com/kevinWangSheng/production-ops-agent/issues/476)）。
- 卸载：`deploy/compose/install.sh uninstall` 保留卷和密钥；`--purge-data` 在你输入项目名之后删除数据库。

## 费用

每个 Run 的模型用量按步骤记录（`opspilot_budget_reservations`、步骤用量）。72 小时 soak 按 token × DeepSeek 标价计算其 24 个 Run 的费用为 4.46 元人民币，没有与供应商控制台对账（[soak 费用](../evidence/m3-soak-2026-11-07/README.md#费用)）。除了每个 Run 的上限之外，没有费用上限。
