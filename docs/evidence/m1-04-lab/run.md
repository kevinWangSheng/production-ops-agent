# M1-04 第 1 步：实验环境 Alertmanager 真实运行

- 日期：2026-10-10（UTC）
- 对象：kind 集群 `opspilot-m1`（colima `m1-kind`），分支 `chore/m1-04-lab-alertmanager`
- 版本：OTel Demo chart 0.37.8；Alertmanager chart 1.24.0（`Alertmanager/0.28.1`，见 webhook 的 User-Agent）；kube-state-metrics 8.6.0
- 合同：[任务记录](../../tasks/2026-10-10-m1-04-alert-intake.md) r2 的 E13（专用 token/actor，经 `credentials_file` 读取）、E14（webhook 路径可验证）、E15（独立且版本固定的 release）
- 类别：有界真实软件环境运行，不涉及产品代码与模型调用

## 做法

第 2 步的 `/intake/alertmanager` 还不存在，所以宿主 `127.0.0.1:8080` 上跑一个临时接收器（不入库），只记录路径、bearer 的 sha256 是否等于 `workbench-alertmanager.env` 里 `alertmanager` actor 的哈希、以及 payload 的告警字段。接收器不保存 token。

1. `kind_lab.py up`：三个 release `deployed`，health 全 ok（含 Alertmanager StatefulSet）。
2. 第一次核对时 Prometheus 没有加载规则、也没发现 Alertmanager：ConfigMap 已更新，但 chart 的 pod 模板不带配置校验和，configmap-reload 侧车又是关闭的。手动 `rollout restart deploy/prometheus` 后规则 `CheckoutPlaceOrderErrorRatioHigh` 为 `health=ok`，`activeAlertmanagers` = `alertmanager.otel-demo.svc:9093`。之后在 `up` 里加了 ConfigMap 哈希注解（见下文「幂等」）。
3. 10:16:41Z `fault inject --experiment-id m1-04-lab-step1`。20 分钟没有收到告警：`traces_span_metrics_calls_total` 在 VM 重启后停滞（frontend 2 分钟 rate = 0，checkout PlaceOrder 各状态 rate 都是 0），属于已知现象。**实验环境动作**：10:37:15Z `rollout restart deploy/otel-collector`，计数器恢复。
4. 10:41:54Z 告警 `startsAt`；10:42:03Z 接收器收到 firing（[webhook-firing.json](webhook-firing.json)）。
5. 10:42:10Z `fault restore`；10:47:03Z 收到 resolved（[webhook-resolved.json](webhook-resolved.json)），`endsAt` 10:46:54Z。

## 结果

| 检查 | 预期 | 实际 |
|---|---|---|
| 路径与方法 | `POST /intake/alertmanager` | 两次都是 |
| 认证 | bearer 的 sha256 等于 `alertmanager` actor 的哈希 | 两次都匹配（`bearer_matches_actor = alertmanager`） |
| payload 版本 | webhook v4 | `version = "4"` |
| 网络路径（E14） | Pod → 192.168.5.2:8080 → 宿主回环 | 送达；工作台保持 `127.0.0.1` 绑定 |
| 目标匹配用的标签（E6） | `namespace`、`service`、`cluster` | `otel-demo` / `checkout` / `opspilot-m1`；`label_replace` 同时保留了原有的 `k8s_namespace_name`、`service_name` |
| 告警身份 | firing 与 resolved 的 fingerprint 和 `startsAt` 一致 | `83f7541ae0105d42` / `2026-10-10T10:41:54.365Z`，两次一致 |
| 分组 | 按 alertname/namespace/service | `groupKey` 与此一致 |
| 凭据不落库 | 证据与 values 里没有 token | 证据文件中 `Bearer`、`token` 出现 0 次；values 只写 `credentials_file` |

供第 2 步参考：
- resolved 时 annotation `description` 从「100%」变成「60.51%」，身份不变。这正是决定 B（只按身份判重放并记录新值）要处理的情形。
- `generatorURL` 主机名是 Prometheus pod 名，PromQL 在 `g0.expr` 里，可以解析。

## 幂等

`up` 把 Prometheus ConfigMap 数据的 sha256 写到 Deployment 的 pod 注解 `opspilot.lab/config-sha256`，配置变化才会滚动。连续跑了两次 `up`：
- 第一次：generation 4 → 5，`deployment.apps/prometheus patched`。
- 第二次：generation 仍是 5，`patched (no change)`；中间的 `helm upgrade` 没有去掉这个注解。

规则与 Alertmanager 发现之后仍然正常。

## 未覆盖

- 重复通知（`repeat_interval` 1h）没有等，留给第 2 步的合同测试。
- 组内多条告警、超大或恶意 annotation 由第 2 步测试覆盖。
- 计数器停滞的原因仍未查，处置方式与 M1-02 相同。
