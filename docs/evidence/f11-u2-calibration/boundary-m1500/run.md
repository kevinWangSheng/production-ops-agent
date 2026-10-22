# F11 U2 M=1500 边界轮（批次 `u2-20261021-b1500`）

- 日期：2026-10-22（UTC），kind-opspilot-m1 / otel-demo，执行者：lane-f11-u2-boundary（占用记录 holder 同名）
- 合同：[任务记录](../../../tasks/2026-10-14-f11-u2-calibration.md)「待补：M=1500 边界轮」、U2-01／U2-03（r2）；用户 2026-10-21 选 M=1500
- 冻结汇总：[`summary.json`](summary.json)（`aggregate --batch u2-20261021-b1500`，compact-v1，sha256 `6951e1e39d158dc289f6259f9326e83b54ec0027f31fd4955a24f410e8b235c8`）；逐轮摘要 [`rounds.json`](rounds.json)；原始响应哈希 [`raw-response-hashes.json`](raw-response-hashes.json)（2 轮 2,565 条）
- 原始 ledger：LangSmith 项目 `opspilot-lab-u2-20261021-b1500`（longlived），链接与归档哈希见 [`platform-links.json`](platform-links.json)
- 批次 `u2-20261017` 的冻结文件（上级目录 `summary.json` 等）未改动
- **结论：M=1500 边界判据未通过**（重跑轮为产品漏检；首轮为环境干扰）。判据未放宽。

## 方法（与 `u2-f-boundary-1` 相同）

- 目标五项身份、HealthProfile `otel-demo-checkout@b72bbe2e30be`（sha256 `b72bbe2e…cbfa4`）、W300／F90／超时 20／I60／H600／deadline 3600／40 次，均未改；采集与折叠代码 `91a011c`（main）。
- 发布：`release` 以 `OPSPILOT_U2_RELEASE_ID` 触发 checkout 真实配置滚动发布，R=patch 成功回执；`watch`→`register`（`--minimum 1200`，只决定 UNAPPROVED 实验壳）→`context`→`collect` 旁路只读采集。
- 注入：`fault --offset 1420 --duration 900`，`paymentFailure` off→100%（R+1420 写配置，使 flagd 生效落在 R+1450–1500）。
- 折叠：`fold --minimum 600 900 1200 1500 1800 2100 --scenario boundary`，判定看 M=1500，其他候选照常记录。
- 真值：Jaeger checkout PlaceOrder／ad server span（每 20 分钟快照，25 分钟回看）与 `fault.jsonl` 配置回执、flagd 解析，生成方式与批次 `u2-20261017` 相同。
- 凭据：旁路只用 lab 账号，`source` 进子进程，未打印；上传前凭据扫描与字面值检索均无命中（见下文）。

## 逐轮结果

| 轮次 | R | 故障生效／恢复 | 首个错误 | M=1500 终态 | 预热后 unknown | 轮前 swap／p95 | 分类 |
|---|---|---|---|---|---|---|---|
| u2-f-boundary1500-1 | 03:18:12.8 | 03:42:54（R+1481）／03:59:17 | 03:43:42.8（R+1530） | anomalous 03:33:24（R+912，故障前） | 0 | 10.3G／97ms | **环境干扰**（失败轮，保留） |
| u2-f-boundary1500-2 | 04:21:34.4 | 04:46:04（R+1470）／05:02:17 | 04:47:33.7（R+1559） | **healthy_confirmed 04:48:55（R+1641）** | 0 | 10.7G／97ms | **产品漏检**（M=1500 边界未覆盖） |

时间为 2026-10-22 UTC；故障生效／恢复为 flagd 首次解析到 100%／off 的时间；首个错误为 Jaeger checkout PlaceOrder server span。

### u2-f-boundary1500-1：环境干扰

- email Pod 03:42:13Z `OOMKilled`（exit 137，`containerStatuses.lastState`）。此前 checkout PlaceOrder p95 自约 03:31 起升高，03:35–03:47 为 8.5–14s（Prometheus 范围查询），03:49 恢复。
- 03:33:24 样本 `DEGRADED_SIGNAL:after/context:latency_p95_milliseconds`，全部候选 M 在此终结为 anomalous，早于故障生效约 9.5 分钟，因此本轮不能检验边界。故障 error_ratio 在 03:47:03 样本（R+1730）首次 degraded。
- 处置：保留为失败轮，按任务允许的一次重跑执行 `-2`。真值 `failure_classification` 已写明原因，原 summary 改名 `summary.superseded-truth-v1.json` 留在 ledger 后重新 fold（差异仅 `truth`／`fold_code`／`artifact_hashes`）。

### u2-f-boundary1500-2：产品漏检（判据未通过）

- 环境：轮前及故障前 checkout p95 90–98ms、错误率 0；轮内无 Pod 重启（email 最近一次重启是 `-1` 轮内的 OOM）。注入按计划，flagd 04:46:04（R+1470）生效。
- 流量低（约 0.03 单/秒），首个错误 span 晚于 flagd 生效 89s，在 R+1559。
- 样本时刻（R+秒）：…1369、1505、**1641**、**1784**…；1641 样本仍为 healthy（查询时刻的 5 分钟窗口内尚无可见错误点），M=1500 在此以 `healthy_confirmed` 终结（剩余 28 次／1959s）。1784 样本首次 `DEGRADED_SIGNAL:after:error_ratio`。
- **余量为负**：检出（R+1784）比 M=1500 健康终结（R+1641）晚 143s；首个错误到检出 225s（boundary-1 为 188s）。
- 其他候选：M≤1200 更早 healthy 终结；M=1800／2100 在 R+1784 判 anomalous（剩余 27 次／1816s）。
- 预热后非预期 unknown：0（全程 unknown 2/12 均在预热窗：首样本 `INSUFFICIENT_TRAFFIC`＋版本映射不足、次样本 `WINDOW_BEFORE_AUTHORIZATION`）。

## 判据（U2-03，M=1500）

| 判据 | 结果 |
|---|---|
| 临近健康终结起病：在 M=1500 健康终结前、deadline 内检出 | **未通过**（`-2`：终结 R+1641，检出 R+1784）；`-1` 受环境干扰不计 |
| 预热后非预期 unknown | 0（两轮） |
| 余量 | −143s（`-2`） |

## 给决策者的事实与推断

- 事实：M=1500 下，起病 R+1224／首错 R+1234 的回归（`u2-f-boundary-1`）在终结前检出（R+1422）；起病 R+1470／首错 R+1559 的回归（本批 `-2`）漏检。本轮实测的 M=1500 健康终结为 R+1641，早于 10-17 批次 healthy／unrelated 的 R+1693–1722，原因是样本相位不同（本轮样本间隔约 137–150s）。
- 推断（未单独验证）：首错到检出约 190–225s（scrape 60s、窗口 300s、样本间隔约 140s 叠加），任何 M 在健康终结前约 3–4 分钟内首次出错的回归都会漏检。边界覆盖只到「首错不晚于约 R+(终结时刻−225s)」。M=1500 时为约 R+1416–1500，按本批相位约 R+1416。
- 选项（由用户裁决，本轮不改合同）：保留 M=1500 并把覆盖承诺收窄为「起病不晚于约 R+1230（已验证）」；改 M=1800（本轮检出，但 10-17 unrelated-1 健康备用预算 94s<300s 不满足）；或明确 U2-01「临近健康终结」边界的可接受盲区定义后再判。

## 环境与收尾

- swap：开轮前 10.3G／11.3G，轮内 10.1–11.7G／12.3G；基线首次检查 p95 560ms（20:19Z，瞬时，前后均约 95ms），5 分钟复查 94ms；实际开轮因执行者等待通知丢失推迟到 03:18Z，开轮前复查 p95 97ms。
- 两轮故障均由 harness 恢复（`restore_config_verified`，flagd 回到 off），无 `fault-active.json` 残留；收尾时 `paymentFailure`／`adFailure` 均 off，checkout 错误率 0。flagd 端口转发（本任务启动）已停止。
- 占用：`occupation claim` 2026-10-21T20:19:59Z（批次 `u2-20261021-b1500`），因执行者停滞过期后于 03:17:54Z 释放并重新 claim 至 06:30Z，任务结束释放。

## 文件与复现

- `summary.json`：两轮均列入，以逐轮完整 summary 的 sha256 绑定（`-1` `ab22c952…`，`-2` `3394eaa6…`），并绑定 `run-starts.jsonl` 前缀（25 条，sha256 `899693ce…`）。
- 逐轮完整 summary 与原始 ledger 不入库，在主仓库 `tmp/lab-ledgers/u2-f-boundary1500-{1,2}/`；真值、span 快照与驱动日志在主仓库 `tmp/u2-b1500/`。复现命令：`python -m scripts.f11_u2_harness --holder lane-f11-u2-boundary --batch u2-20261021-b1500 fold --run <轮> --minimum 600 900 1200 1500 1800 2100 --scenario boundary --truth tmp/u2-b1500/truth-<轮>.json`（fold 拒绝覆盖，复核时在 ledger 副本上删去该轮 summary 后重跑）。
- `platform-links.json`：每轮一个根 run 加一个批次级 run（`run-starts.jsonl` 与 `tmp/u2-b1500/` 顶层真值、日志），附件 `<轮>_tar_gz` 与逐文件 `manifest_json`。上传前扫描（authorization／bearer／password／api_key／secret／token／x-api-key／lsv2_ 及字面凭据模式）非代码文件 0 命中，代码快照命中仅为 Secret 对象名与文档示例；lab Prometheus 密码与 Basic 头字面值在 ledger、tmp 与本目录中 0 命中。
