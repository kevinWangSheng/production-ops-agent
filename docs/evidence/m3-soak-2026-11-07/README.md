# 72h lab soak 证据（F8 第 4 步，2026-11-07 至 11-10）

一台租用的共享 vCPU 主机跑目标侧 kind lab（OTel Demo），一台共用控制主机跑 OpsPilot（Compose），两边用 SSH 隧道连接。soak 覆盖 72 小时，期间有背景流量、计划内故障与恢复。这是实验环境的 soak，不是生产证明，不是压测，没有数值目标；资源与延迟数字只描述这一次运行，不可比较。

- 窗口：T0 `2026-11-07T01:05:00Z`，`soak_end` `2026-11-10T01:05:18Z`，覆盖 259218 s（要求 259200 s），环境中断 0、顺延 0。
- 计划：`plan.json` sha256 `b3bef1034afba77d8ac82749d327a0c01bf1766c9c3191a5872b899f377c35e0`，T0 前冻结并推送（#524，提交时间 00:46:02Z）。
- 版本：产品镜像 `v0.1.0`（`ghcr.io/kevinwangsheng/opspilot@sha256:228df9f72e6964e1d7d7110365d3b59330d7fc93ebd89e62b70631de6c7aaabd`，产品范围摘要 `c841e2b5`，与冻结的 `a2a68ef4` 相同）；soak 与 lab 脚本 = main `1723d589`（v0.1.0 + #521，只改 `scripts/soak/lab_io.py`，产品范围摘要不变）。
- `summary.json` 由 `python3 -m scripts.soak summarize` 生成；`step4.achieved = true`，`unmet = []`。

本目录文件：`summary.json`（汇总器输出）、`cost-primary.json`（费用主数）、`reservations-by-run.csv`（每个 Run 的模型调用数）。原始事件、采样、PG 导出与 `pg_dump` 留在仓库外（S-R15）。三个文件不含主机地址与凭据（`host` 字段只是两台主机的角色标签）。

## 结果

| 项 | 结果 |
|---|---|
| 注入 | 33/33 执行，0 跳过，0 条件未满足。24 次 paymentFailure（第 8、16 次 75 min），4 次噪声，A1–A5 各一次 |
| 端到端 | 24 次 episode 全部在告警 firing 后 10 min 内有 Run（outcome 全为 `run`）；最慢 494 s（ep15，隧道停 10 min 的 A4 窗口内，恢复后投递并新建 Run） |
| 噪声 | 4 次噪声开关均未开工作 |
| 可用性 `/readyz`（30 s，8374 次） | web 100%，worker 99.95%，observer 97.74%（见下「Observer 可用性」） |
| 可用性 Prometheus `up` | web 99.75%，worker 99.74%，observer 99.75% |
| 卡住的工作 | 均为 0，除 `over_wall_clock_runs = 1`（见下「A3 的 Run」） |
| 脚本代人工关闭 | 24 次，全部 200：`resolved` 20，`observation_ended_unconfirmed_fault_cleared` 2（两次 75 min episode），`before_next_injection_run_waiting_human` 2；`attached` 0 |
| lab 存活（KSM 映射） | 21 个 Deployment 全程都在 |
| 恢复观察样本 | 应有 574，实有 417，缺 157，信号缺失 5，陈旧 0（见下「恢复样本节奏」） |
| 模型调用（PG） | 24 个 Run：成功 267，失败 5，未结算 1（见下） |
| 费用主数 | CNY 4.46（闲时 3.80 + 高峰 0.66），见下 |
| 时钟 | 两机最大偏差 0.376 s，无超过 1 s |
| Jaeger（#496） | soak 窗口内 436 个样本，RSS 211–345 MB，0 次重启，全程同一个 pod（到结束约 81 h；修复前 1Gi 时约 13.7 h OOM） |

资源（p95 内存）：控制侧 worker 266 MB（最大 305），web 86，postgres 80，observer 55，隧道 3；控制主机可用内存最低 3.37 GB，load1 p95 4.36（含共用主机上的其他工作负载，`summary.json` 中按 `shared` 分组单列）。目标侧 kind 节点 p95 3.59 GB（最大 3.65），load1 p95 0.63。

## 需要说明的几点

### Observer 可用性与恢复样本节奏（#522）

Observer 的 `/readyz` 只在恢复观察进行时返回 503。原因（读代码得出，现场未抓到 503 的 reason 字段）：Observer 在主线程执行一次采样后才刷新「最近一次成功轮询」时间，`/readyz` 在该时间超过 3 × 5 s 时报 `POLL_STALE`；一次采样经隧道发 24 个 Prometheus 请求，约 20–25 s，超过 15 s。所以 observer 的可用性被它自己的观察工作拉低，不是服务不可用。产品冻结（Q6），未改；post-m3 跟踪 #522。

同一原因让相邻样本间隔约 83 s，而汇总器按 60 s 计算应有样本数：每个观察会话约 22 个应有、实有约 16，合计缺 157 个（27%）。这是**采样节奏不足，不是数据丢失**：陈旧样本为 0，信号缺失只有 5 个。

### A3 的 Run（`over_wall_clock_runs = 1`）

A3 在 ep12 期间切断模型出口 20 min。ep12 的 Run `2c1120e2` 第一次调用就失败（2 次未结算调用，状态 `unknown`），按设计转人工（`waiting_human`，11-08 10:21Z）；下一次注入前 10 min，脚本操作员按 B1 规则关闭事故（13:05Z），Run 变为 `cancelled`。等待人工的约 2.75 h 超过了 Run 墙钟上限，所以计入 `over_wall_clock_runs`。这是计划内故障的预期结果，不是卡住的工作。

### 模型调用数与未结算的预留（#527）

模型调用数取自 PG `opspilot_budget_reservations`（每次实际模型调用一行；`spent` 成功，`unknown` 失败且无用量），按 Run 汇总在 `reservations-by-run.csv`，范围为 T0 之后创建的事故。worker 的日志与指标只作对照：A3 的断出口与恢复会重建 worker 容器，日志和计数器随之清零，A1 的重启也会清零计数器。

- 失败 5 次：ep03 的 Run（`MODEL_UNAVAILABLE`，2 次，转人工后按 B1 关闭）、A3 的 Run（2 次）、另一个 Run 的 1 次（重试后完成）。没有 `MODEL_REJECTED`。
- 未结算 1 次：Run `d687184a`（ep06）。A1 在它进行模型调用时杀掉 worker；Run 之后被接管并完成，但被打断的那次预留一直停在 `reserved`。不影响 Run 结果，也不影响用量统计（用量只取已提交步骤）。post-m3 跟踪 #527。

### 费用

- 主数：`cost-primary.json`，范围为 T0 之后创建的事故的 Run（不含彩排）。按每一步的 `usage` 分别计缓存命中、未命中与输出 token，并按调用时刻换算北京时间划分高峰（周一至周五 09:00–12:00、14:00–18:00）与闲时；11-07、11-08 是北京时间周末，全天闲时，只有 11-09（周一）有高峰。单价取 DeepSeek 官方价格页（`https://api-docs.deepseek.com/zh-cn/quick_start/pricing`，`deepseek-flash`，页面无日期，2026-11-07 读取）。闲时 239 步 CNY 3.80，高峰 28 步 CNY 0.66，合计 **CNY 4.46**。
- `summary.json` 的 `token_cost_cny = 27.17` 是按冻结计划的单一价格（高峰缓存未命中 2/8 CNY 每百万 token）算的上限。
- 控制台对照未做：用户 2026-11-09 告知 DeepSeek 控制台只按天、按账户汇总，给不出本 key 在 soak 窗口内的金额或 token；总管据此不做控制台对照。费用只有上面的主数（CNY 4.46）和下面的上限。
- 账户余额不用于费用：同一账户下其他 key 也在消耗，余额差不能代表本 soak。原 Q4 裁决定的是「余额差为主数」：用户 2026-11-06 告知余额是账户级、由其他 key 的实验共用，且本 key 在控制台的用量不到 CNY 1；总管据此改为本节口径（见 soak 记录 Q4 修订），`summary.json` 的 `balance_delta_cny` 留空。控制台对照见上一条，不做。
- 租用主机费用以供应商账单为准，未计入 `summary.json`。

## 偏差

- 操作员凭据检查：开跑前一次手动 `curl -u` 核对工作台登录时，凭据短暂出现在控制主机的进程参数里。soak 脚本本身从文件读凭据放进请求头，不经参数。
- 彩排（#497）共三轮：
  - 第一轮作废：worker 读到的是旧密钥文件，第一个 Run 因认证失败转人工，事故由人工关闭。
  - 第二轮发现 soak 脚本读 PG 的缺陷（多行 `json_agg` 输出只读最后一行），从第二个事故起关闭、Run 状态与条件判断全部失败；修复 #521，正式 soak 用修复后的脚本。
  - 第三轮补测受影响项，全部通过。第二轮遗留的一个事故由人工关闭。
- 第二轮的计划把断模型出口放在了 75 min episode 上，未覆盖「观察结束未确认」分支；第三轮补上。
- 控制主机是共用主机，soak 期间上面还有别的工作负载。
- 目标侧主机不是全新安装（Q3）：考试数据归档后拆掉 3 套考试 lab，用安装脚本重建 1 套。
- 共享 vCPU：资源与延迟数字只作描述。
