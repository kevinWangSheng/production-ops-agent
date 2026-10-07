# M1-02 第 1 步：checkout HealthProfile 数值校准

- 日期：2026-10-07；issue #83；任务记录 [2026-10-03-m1-02-recovery-observation.md](../../tasks/2026-10-03-m1-02-recovery-observation.md)「第 1 步执行」。
- profile：`opspilot/observer/profiles/otel-demo-checkout.json`，revision 见任务记录。
- 产品代码只读；本目录的 `collect_baseline.py` 是工程脚本，只向 Prometheus 发只读查询。

## 本次没有新采集：kind 启动中止

2026-10-07 05:42 PDT `kind_lab.py up`：脚本自检宿主可回收内存 3.36 GiB（≥ 3 GiB 门槛）后启动 colima `m1-kind`；VM 起来约 2 分钟内宿主 swap 从 8.9 GB 升到 10.9 GB（上限 11.3 GB），`helm repo update` 仍在等网络。判断为「明显不够」，05:44 `pkill` 中止 `up` 并 `kind_lab.py stop`（VM 已停，集群与 release 未动，helm upgrade 未执行）。`collect_baseline.py` 因此**未运行**，`tmp/` 下没有原始返回。

## 数值来源：第 0 步同一实验环境的独立观察（2026-10-06）

来自 `docs/evidence/m1-02-lab/regression/*/observe-*.json`（`scripts/otel_demo_observe.py` 直读 Prometheus，每窗 300 s；2 个 locust 用户、无浏览器流量；分支 `chore/m1-02-kind-lab`，PR #111）。标签形状已由这些返回确认：`traces_span_metrics_calls_total{service_name="checkout", span_kind="SPAN_KIND_SERVER", span_name="oteldemo.CheckoutService/PlaceOrder", status_code}`；kube-state-metrics 序列 `kube_deployment_status_replicas_available{namespace="otel-demo"}`、`kube_pod_status_phase`、`kube_pod_container_status_restarts_total` 见 `m1-02-lab/lab-health.json` 与 run.md。

| 窗口（UTC） | 状态 | PlaceOrder server spans / 300 s（UNSET + ERROR） | 错误占比 | checkout client spans / 300 s | client 错误 |
|---|---|---:|---:|---:|---:|
| normal-1 10:30–10:35 | 正常 | 3.82 + 0 | 0 | ~45 | 0 |
| fault-1 pre 10:38–10:43 | 故障（payment Charge 失败） | 3.75 + 4.13 | 0.52 | ~63 | 4.13（Charge） |
| fault-1 after restore 10:49–10:54 | 恢复后 | 11.25 + 0 | 0 | ~118 | 0 |
| normal-2 10:57–11:02 | 正常 | 3.75 + 0 | 0 | ~45 | 0 |

延迟：fault-1/normal-2 报告里由调查工具按 10 分钟窗算的 PlaceOrder server p95 为 37.5 / 122.5 / 175.7 / 182.5 ms（直方图桶粗、每窗约 10 个 span）；client 侧各依赖 p95 ≤ 25 ms。

## 校准结果（写入 profile）

| 参数 | 值 | 依据 |
|---|---:|---|
| `effective_traffic.minimum` | 0.008 /s（≈ 2.4 单 / 5 min） | 稳态 3.75–3.82 单 / 300 s = 0.0125 /s，门槛取其 ~0.6 倍；撤流量后降到 0 会被挡住 |
| `error_ratio.healthy.max` | 0.01 | 正常窗 0；稳态每窗只有 ~4 单，任何一次错误即 ≥ 0.25，故障窗 0.52 |
| `latency_p95_milliseconds.healthy.max` | 500 ms | 观测 p95 37.5–182.5 ms，取 ~2.7 倍上限 |
| `dependency_error_ratio.healthy.max` | 0.01 | 正常窗 0 / ~45–118 次；故障窗 4.13 / ~63 ≈ 0.07 |
| `deployment_available_replicas` / `pods_running` / `dependency_deployments_available` min | 1 | 实验环境 spec.replicas = 1（`m1-02-lab/run.md` 20 个 Deployment 全部就绪） |
| `pod_restarts_in_window.healthy.max` | 0.5 | `increase()` 对常数计数器恰为 0，一次重启约为 1 |
| `evaluation_window_seconds` / `freshness_seconds` / `query_timeout_seconds` | 300 / 90 / 20 | 与第 0 步观察窗一致；超时取 C3 §13 数据源请求超时 |
| `session`：deadline / max_samples / interval / sustained | 3600 s / 40 / 60 s / 600 s | 故障钩子生效与恢复在指标上各需约 3 分钟（`m1-02-lab/run.md`），600 s 持续窗覆盖两个这样的周期；40 × 60 s 可覆盖 2340 s ≥ 600 s |
| `minimum_samples` | 状态信号 1；比率/分位信号 2 | **未校准**：抓取/flush 间隔未实测；采样器按窗内点数填 `sample_count` 后再收紧 |
| `traffic_dependent` | error_ratio、latency_p95、dependency_error_ratio 为 true，其余 false | 比率/分位在低流量下无意义，不判定；副本/pod 状态与流量无关，照判 |

## 限制

- 稳态流量很薄（2 用户，~4 单 / 5 min），门槛与错误率阈值的分辨力都受限；若 F6 验收需要更稳定的比率，应在实验环境提高 locust 用户数（第 0 步脚本 `values.yaml`，不在本步范围）。
- `latency` 桶粗、样本少，500 ms 是宽松上限，不是回归检测阈值。
- `collect_baseline.py` 留作下次环境可用时的复核脚本：`.venv/bin/python docs/evidence/m1-02-health-profile/collect_baseline.py --minutes 30 --out tmp/m1-02-health-profile/baseline-raw.json`，输出摘要与原始文件 sha256。
