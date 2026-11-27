# 工作台复验：一次真实事故，从告警到复盘获批（2026-11-27）

- 依据：#879；[任务记录](../../tasks/2026-11-26-portfolio-media.md)（D2：素材来自租机上的真实注入与真实 Run）
- 结论：工作台重设计后的主路径在新装主机上走通，告警、调查、报告、自动恢复观察、复盘草稿、人工批准各一次，均为真实运行。lab 证据，不是生产证明。

## 环境

| 项 | 值 |
|---|---|
| 主机 | Vultr 4 vCPU / 15 GB 内存 / 75 GB 磁盘，Ubuntu 24.04.5 LTS，全新开机 |
| 检出 | `main` `271a09a7`（worker 日志的版本号标签仍是 `0.1.1`） |
| 安装 | 按 README「The OpenTelemetry Demo lab on the same host」：`install_linux.sh --user opspilot`、`control-secrets`、写 `worker_env`、`OPSPILOT_START_AGENT=1 OPSPILOT_TRACE=lab install.sh install`、`connect-control`；按 `control-secrets` 的提示安装后再跑一次 `control-secrets` 与 `connect-control`（README 未写这一步，#887） |
| 工具档位 | `otel-demo`（Prometheus 与 Jaeger，只读） |
| 模型 | DeepSeek `deepseek-flash` |

## 时间线（UTC）

| 时间 | 事件 | 来源 |
|---|---|---|
| 04:18:50 | lab 脚本注入 `paymentFailure`（flagd） | 操作者 shell 记录 |
| 04:23:04 | Alertmanager `CheckoutPlaceOrderErrorRatioHigh` 送达，开事故 `d5fa7059` 与 Run `69323f20` | `summary.json` `alert_deliveries`、`audit` |
| 04:24:47 | 第 11 步写出结论，报告发布：`supported`，根因 `Deployment otel-demo/payment` | `conclusion_step`、`report` |
| 04:25:14 | 合格结论后自动开始恢复观察 | 工作台事件 |
| 04:28:20 | lab 脚本撤销故障 | 操作者 shell 记录 |
| 04:44:55 | Observer 确认恢复（5 个 degraded 采样后转 healthy），未记录修复动作 | 工作台恢复观察 |
| 04:46:11 | 复盘草稿 v1 生成：10 条模型陈述（7 supported、3 uncertain、0 争议），引用检查通过 | 复盘审核页 |
| 04:48:39 | 以 `operator` 账号批准（录制脚本点击，不是人审阅后的决定），发布 2 条知识到 OpsPilot 自己的知识库 | 复盘审核历史 |

## 数字

- Run：11 次模型请求（上限 100），18 次工具调用，其中 2 次 `traces_search` 因 `RESULT_TOO_LARGE` 被拒；告警到报告 1 分 43 秒。
- Token：prompt 268,679（缓存命中 242,176、未命中 26,503），completion 19,178。按此前记账用的 deepseek-flash 闲时价（命中 0.003、未命中 0.15、输出 0.60 美元/百万 token）约 $0.016；本次在非闲时段，实际单价未核。复盘生成另用 prompt 35,467、completion 12,385，不在 `summary.json` 内。
- 冻结摘要：[summary.json](summary.json)（`scripts/run_freeze.py`，owner DSN 只读事务）。原始 ledger 只留在主机，sha256 `c896a669…9534`，288,671 字节。

## 未达成与发现

- **无 LangSmith trace。** `OPSPILOT_TRACE=lab` 在启动时被安全检查关闭（`PROMETHEUS_NOT_LAB,JAEGER_NOT_LAB,KUBERNETES_NOT_LAB`），同机 Compose lab 的数据源地址不被认作 lab；`run_freeze` 回读为 `LangSmithNotFoundError`。按设计失败即关闭，不是故障；文档缺口见 #887。
- **结论标题在「i.e.」处截断**，后半句落到正文（仅显示，#886）。见 [report.png](../../assets/workbench/report.png)。
- 未覆盖：人对复盘草稿的实际审阅（批准由录制脚本点击）、人工接管、交接、结论后追问、Chat；只注入一种故障、跑一次。

## 截图

[调查中](../../assets/workbench/investigating.png)、[报告](../../assets/workbench/report.png)、[恢复观察](../../assets/workbench/recovery-watch.png)、[复盘审核](../../assets/workbench/postmortem-review.png)。同一次运行的录屏另作演示视频（#880），不入库。
