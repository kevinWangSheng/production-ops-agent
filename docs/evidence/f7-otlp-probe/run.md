# F7 U3 第一步：lab Prometheus OTLP 写入探测

- 依据：[任务记录「待执行：Prometheus OTLP 写入探测」](../../tasks/2026-10-15-f7.md)；配置 `scripts/kind_lab/values.yaml:119-122`（`web.enable-otlp-receiver`）。
- 时间：2026-10-16T04:05:06Z – 04:05:31Z（UTC）。不注入故障、不部署、不调用模型，无费用。
- 环境：colima `m1-kind` 上的 kind 集群 `opspilot-m1`，Prometheus 经 NodePort `127.0.0.1:19090`。探测前 `kind_lab.py health` 为 `ok: true`，flagd-config 中 11 个 flag 的 `defaultVariant` 全部为 `off`。
- 凭据：只在子 shell 里 `source` `tmp/m1-kind-lab/prometheus-{investigator,observer,lab}.env`，经 `curl -K -` 从标准输入传入；未上命令行、未回显、未写入本文件。三个用户名经脚本比对分别等于角色名 `investigator`、`observer`、`lab`。
- Prometheus 版本（`GET /api/v1/status/buildinfo`，两个账号返回一致）：`3.5.0`，revision `8be3a9560fbdd18a94dedec4b747c35178177202`。

## 请求与结果

| 步骤 | 方法与路径 | 账号 | 状态码 | 结果 |
|---|---|---|---|---|
| 1 | `GET /api/v1/query?query=up` | investigator | 200 | `status: success`，凭据有效 |
| 2 | `POST /api/v1/otlp/v1/metrics`，`application/x-protobuf`，空 `ExportMetricsServiceRequest`（0 字节） | investigator | 200 | 接受写入 |
| 4a | `POST /api/v1/admin/tsdb/snapshot` | investigator | 500 | `errorType: unavailable`，`admin APIs disabled` |
| 4b | `POST /-/reload` | investigator | 403 | `Lifecycle API is not enabled.` |
| 5 | `GET /api/v1/status/buildinfo` | investigator | 200 | `3.5.0` |
| 1 | `GET /api/v1/query?query=up` | observer | 200 | `status: success`，凭据有效 |
| 2 | `POST /api/v1/otlp/v1/metrics`，同上空请求 | observer | 200 | 接受写入 |
| 4a | `POST /api/v1/admin/tsdb/snapshot` | observer | 500 | `errorType: unavailable`，`admin APIs disabled` |
| 4b | `POST /-/reload` | observer | 403 | `Lifecycle API is not enabled.` |
| 5 | `GET /api/v1/status/buildinfo` | observer | 200 | `3.5.0` |
| 对照 | `POST /api/v1/otlp/v1/metrics`，同上空请求，不带凭据 | 匿名 | 401 | `Unauthorized` |
| 3 | `POST /api/v1/otlp/v1/metrics`，gauge `opspilot_f7_probe{account="investigator"}=1`（74 字节，`.venv` 的 `opentelemetry.proto` 序列化） | investigator | 200 | 04:05:28Z 写入 |
| 3 | `POST /api/v1/otlp/v1/metrics`，gauge `opspilot_f7_probe{account="observer"}=1`（70 字节） | observer | 200 | 04:05:28Z 写入 |
| 3 读回 | `GET /api/v1/query?query=opspilot_f7_probe` | lab | 200 | 04:05:31Z 返回两条序列，见下 |

第 3 步读回原文（时间戳为 Prometheus 求值时刻）：

```json
{"status":"success","data":{"resultType":"vector","result":[{"metric":{"__name__":"opspilot_f7_probe","account":"investigator"},"value":[1792123531.891,"1"]},{"metric":{"__name__":"opspilot_f7_probe","account":"observer"},"value":[1792123531.891,"1"]}]}}
```

与任务记录预期的差异：第 4a 步预期 404，实际 500 `admin APIs disabled`。含义相同（admin API 未开），状态码照实记录。

## 判据结论

第 2 步两个产品只读账号都返回 200，第 3 步写入的 gauge 由 lab 账号读回：**investigator 与 observer 都能经 OTLP 接收端向被调查的 Prometheus 写入样本**。basic auth 只认证、不按路径授权的推断成立。按任务记录判据，U3 走 U3-a：在 PR-C 加只读鉴权代理。

## 遗留数据

lab Prometheus 里留有两条 `opspilot_f7_probe` 序列（`account` = `investigator` / `observer`，值 1，写入时间 2026-10-16T04:05:28Z），未清理（admin API 未开，无法删序列）；按 Prometheus 默认陈旧规则，即时查询约 5 分钟后不再返回，原始样本随 TSDB 保留期过期。后续 lab 证据若查询全部指标名，应忽略此名。
