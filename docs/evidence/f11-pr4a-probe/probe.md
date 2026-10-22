# F11 PR-4a 只读探测：before 窗旧实例切片能否稳定映射

- 时间：2026-10-22 UTC 20:53:57–21:12:42（kind 占用 holder/batch `pr4a-probe`，已 release，`occupation show` = null）
- 范围：只读。Prometheus HTTP GET（lab 账号，经 readonly proxy）与 `kubectl get`；未 apply/patch/delete，未发布，未注入，未跑 make check/pytest/Codex。
- 方法：直接复用 main 上 PR-2 的 `build_release_query_plan` + `_mapped_instances` + `evaluate_release_sample`（`opspilot/observer/release_evaluation.py:166-300, 341-462, 509-`）。旧切片 = 把 before 身份（`before_pod_template_hash`、`before.images`）放进同一算法的 "after" 槽，在 `released_at` 求值，窗长 `evaluation_window_seconds=300`。context 以 `u2-f-boundary1500-2/context.json` 为底，替换 release 身份。
- 脚本：`/private/tmp/claude-501/-Users-shenghuikevin-dev-AI-production-ops-agent/923c6c5a-354f-4d80-b818-cd62113f3099/scratchpad/probe_pr4a.py`、`.../scratchpad/q4_compare.py`
- 原始结果：`/Users/shenghuikevin/dev/AI/production-ops-agent/tmp/lab-ledgers/pr4a-probe/`（`<tag>.json` 为原始响应，`<tag>.summary.json` 为判定摘要，`range-*.json` 为时间线，`q4-*.json` 为 Q4 对比）

样本：主样本 `f11-pr3-live-1`（released_at 20:49:02.036Z，RS `ff6bd6fdc`→`645dc77999`，配置发布，前后镜像 digest 相同）；补充两次早先 U2 发布 `u2-f-boundary1500-2`（10-22 04:21:34Z）和 `u2-f-healthy-1`（10-17 08:07:23Z），均在 Prometheus 15d 保留期内（`--storage.tsdb.retention.time=15d`）。实验环境 checkout `replicas=1`，策略 RollingUpdate maxSurge/maxUnavailable 25%（即先起 1 个新 Pod 再删旧 Pod）。KSM 抓取 60s；spanmetrics 经 OTLP 推送，约 60s 一个点。

## Q1 旧 RS Pod 的 KSM 序列（released_at 时刻）

查询：PR-2 plan 的 `mapping:{replicaset_owner,pod_owner,container_info}:{query,coverage,freshness}`，RS=`checkout-ff6bd6fdc`，`at=released_at`。

| 发布 | 旧 Pod | 三类序列存在 | coverage(300s) | 新鲜度(at − 最新原始点) | image_id 与 before.images |
|---|---|---|---|---|---|
| f11-pr3-live-1 | checkout-ff6bd6fdc-z5tvt / 6faa7e83… | 是 | 5/5/5 | 8.8s | 相等（sha256:5ab0d7b9…） |
| u2-f-boundary1500-2 | checkout-6b9b8545bb-k8v5g / 4292c765… | 是 | 5/5/5 | 35.4s | 相等 |
| u2-f-healthy-1 | checkout-85fbbb6c74-sb7rf | 是 | 5/5/5 | 25.5s | 相等 |

`_mapped_instances` 原因码为空，UID 集合恰好 = 旧 RS 的唯一 Pod。结论：在 released_at 求值，三类 KSM 序列齐全、新鲜（< freshness 90s，上限约等于一个抓取间隔 60s），镜像完全相等。注意：配置发布前后 digest 相同，镜像相等只是一致性校验，区分新旧完全靠 RS 名（pod-template-hash）。

## Q2 span 指标按旧 Pod UID 切片

查询：plan 的 `after:*:{query,coverage,freshness,instances}`，matcher `service_instance_id=~"<旧 UID>"`，`at=released_at`。span 序列上 `service_instance_id` = `k8s_pod_name` 对应 Pod 的 UID，`k8s_pod_uid` 标签不存在（只有 `instance`=UID）。

| 发布 | 旧切片 verdict | request_rate | p95 | sample_count | 最新点距 at | 整体服务同窗 |
|---|---|---|---|---|---|---|
| f11-pr3-live-1 | healthy，原因码空 | 0.0292/s | 95.6ms | 5 | 3.4s | 完全相同（单副本） |
| u2-f-boundary1500-2 | healthy | 0.0375/s | 96.8ms | 5 | 30.0s | 相同 |
| u2-f-healthy-1 | healthy | 0.0292/s | 376.7ms | 5 | 20.0s | 相同 |

f11 旧 Pod 在 before 窗内 `increase(calls[300s])` = 8.75 次 PlaceOrder。覆盖率 1/1（有流量的旧 Pod / 旧 Pod）。结论：能切出，样本 5 个（≥ minimum_samples 3）。**多副本覆盖率未测**：实验只有 1 副本，"部分旧 Pod 无流量"的情况没有真实数据（未确认）。

## Q3 滚动时间线与 staleness（f11-pr3-live-1）

| 时间（UTC） | 来源 | 事件 |
|---|---|---|
| 20:48:53.186 | KSM 抓取 | 旧 Pod 最后一个 kube_pod_owner/container_info/ready 原始点 |
| 20:48:58.625 | spanmetrics | 旧 Pod 最后一个 before 窗内 span 点 |
| 20:49:02.036 | harness | patch 回执 = released_at |
| 20:49:02 | k8s 事件 | 新 Pod Scheduled |
| 20:49:03 | k8s 事件 | 新 Pod Started；旧 Pod Killing |
| 20:49:04.59 | harness watch | 新 Pod Ready，旧 Pod 已不在 API |
| 20:49:53.186 | KSM 抓取 | 新 Pod 首个 KSM 点；旧 Pod 序列收到 staleness marker |
| 20:49:58.625 | spanmetrics | 新 Pod 首个 span 点 |

- 真实并存只有约 2 秒（20:49:02–04），**Prometheus 里看不到并存**：60s 抓取把它量化掉了。20:49:02–20:49:53 之间 KSM 仍只显示旧 Pod；之后只显示新 Pod。
- 旧切片按 at 偏移求值（`old@<offset>.summary.json`）：−60s、0、+30s 成功；+60s、+90s、+120s 起 `VERSION_MAPPING_MISSING`/`VERSION_MAPPING_STALE`；+300s、+400s `VERSION_MAPPING_MISSING`。即 KSM 旧 Pod 序列在删除后的下一次抓取（≤60s）即从即时查询消失，但历史数据在 15d 内按 `time=released_at` 可重查。
- 新切片按偏移：+0 `VERSION_MAPPING_AMBIGUOUS`（新 RS 尚未被 KSM 看到）、+60s `VERSION_MAPPING_INSUFFICIENT_SAMPLES`（只有 1–2 个 KSM 点）、+300s、+420s healthy。
- **span 序列不过期（重要）**：collector `spanmetrics: {}` 用默认配置（未设 metrics_expiration），已死 Pod 的 span 序列一直以新时间戳推送、计数器不再增长。现在 checkout 有 16 个 `service_instance_id` 序列，只有 1 个 Pod 活着（`range-span_ts.json`：13 个历史 Pod 的 span 点到 20:58:58 仍在更新）。后果：
  - span 的 `timestamp()` 新鲜度与 `count_over_time` 不能证明实例存活，旧切片的存活/归属只能由 KSM 映射保证（PR-2 算法已经这么做）。
  - 旧切片若晚于 released_at 求值，span 侧仍会"新鲜"但 KSM 侧失败，结果是 unknown 而不是误判，安全。
  - 整体服务查询中僵尸序列 rate 为 0，不改 sum(rate)/p95；但整体服务的 coverage/freshness 被僵尸序列"撑满"，这是 PR-2 现有 profile 的既有局限，不是 4a 新增。

## Q4 after 窗整体服务与新版本切片 vs PR-2b

- f11-pr3-live-1 本身没有 PR-2b 样本（授权被 `RELEASE_POLICY_UNCALIBRATED` 拒绝）。用同一代码在 +300s 求值：新切片 UID = `4e732e2f…`（checkout-645dc77999-wt2pd），healthy，rate 0.027/s、p95 93.3ms、5 个样本；与同窗整体服务完全相同（单副本 + 僵尸 rate 0）。
- 与真实 PR-2b 样本对照（`q4-u2-f-boundary1500-2.json`）：取 U2 运行中 Observer 实际落库的 sample 002（window_end 04:24:28）与 015（04:54:37），用当前 main 的 `build_release_query_plan` 重建：49 个 query key 与查询文本（normalize 后）完全一致；在原 `evaluated_at` 重发 98 个查询，返回向量与当时存储的 raw 完全相同（0 差异）。该次发布 PR-2b 映射的新 Pod 正是 f11 的旧 Pod 6faa7e83，与本次旧切片映射交叉一致。
- 结论：4a 的 after_service / after_new 直接复用 PR-2b plan 与 anomaly 样本即一致；历史时刻重查可复现（15d 内），支持重放校验。

## Q5 结论：P4-01=A 对比采集可行

在本实验条件（单副本、配置发布、released_at = patch 回执时间、60s 抓取）下，三次真实发布的旧切片都能用 PR-2 映射算法在 released_at 确定性求出，UID 集合精确，样本数与新鲜度满足 profile，且可在 15d 内按历史时刻重查复现。主要风险不在映射本身，而在 released_at 的时间语义与抓取间隔：

- 若 released_at 晚于真实滚动 ≥ 一个抓取间隔（如部署器在 rollout 完成后才发 webhook），旧 Pod 在 KSM 已消失，旧切片会 `VERSION_MAPPING_MISSING`；若晚得少于一个间隔，KSM 仍显示旧 Pod（本次 +30s 成功）。
- 若两次发布间隔 < ~3 个 KSM 抓取（约 3 分钟），旧 Pod 在 before 窗内 KSM 点数 < 3，判 `VERSION_MAPPING_INSUFFICIENT_SAMPLES`（推断，未实测）。
- 多副本、部分旧 Pod 无流量、旧 Pod 在 before 窗中途替换：未实测。

## 对 PR-4 合同的补充建议

| # | 建议 | 类别 |
|---|---|---|
| S1 | before 窗 = `[released_at − evaluation_window_seconds, released_at]`，旧切片映射与所有 before 查询都以 `time=released_at` 求值（不是采集时刻）；窗长取 profile 值（实验为 300s），不另设。 | 可逆默认（与 P4-01 推荐一致，实测支持） |
| S2 | 旧切片完全沿用 PR-2 映射与判定（同一 minimum_samples=3、freshness_seconds=90、≤64 UID、镜像完全相等），只把身份换成 `before_pod_template_hash` 与 `before.images`；原因码沿用 `VERSION_*`，前缀 scope 用 `before_old`。不新增阈值。 | 可逆默认 |
| S3 | `before_old` 的 unknown 条件明确列出：缺 `before_pod_template_hash` → `VERSION_IDENTITY_MISSING`；前后 hash 相同 → `VERSION_SLICE_NOT_DISTINGUISHABLE`；任一 `VERSION_*`/`QUERY_*` 映射原因 → 旧切片无值、只保留 before_service；记录 `status=partial`。镜像 digest 相同不算不可区分（配置发布的常态）。 | 可逆默认 |
| S4 | "覆盖阈值"：沿用 PR-2 after 切片的规则——instances 查询返回的 UID 集合必须恰好等于映射集合（多、少、外来都判 `VERSION_INSTANCE_MISMATCH`），不引入"有流量旧 Pod ≥ x%"的比例阈值。代价：多副本时任一旧 Pod 在 300s 内无 span 点即整切片 unknown。多副本未实测。 | 需裁决（严格全等 vs 比例阈值；推荐严格全等，与 6.11 验收"多、少或外来实例都判不充分"一致） |
| S5 | released_at 时间语义：对比采集信任 released_at 为"滚动开始前"时刻；若部署器在滚动后才报，旧切片会 `VERSION_MAPPING_MISSING` 而 unknown，不回退到更早时刻。备选是在 released_at − 一个抓取间隔求值，但会让 before 窗依赖实验参数。 | 需裁决（推荐不回退；把"released_at 必须为变更提交时刻"写进 PR-3 接入文档/C3 来源要求） |
| S6 | 查询数上限：旧切片 = 9 个映射 + 4 个 AFTER_SIGNALS × 4 = 25 个；before_service = 8 signals × 3 = 24；after_service 与 after_new 取自 anomaly 样本不重查。每次尝试约 49 个查询，3 次尝试上限 147；单查询超时沿用 `query_timeout_seconds`=20。实测一次旧切片采集（含 before_service）在本机约 40–60s。 | 可逆默认（写成常量，计入"每 Run 防循环上限"之外的 Observer 预算） |
| S7 | 不依赖 span 的 freshness/coverage 证明实例存活：spanmetrics 默认不过期，已死 Pod 序列持续有新时间戳。合同写明"旧/新切片的实例归属与存活仅由 KSM 映射证明"，span 查询只取值。是否给 collector 加 `metrics_expiration` 属于实验环境改动，不在 PR-4 范围。 | 可逆默认（文档化）；collector 配置改动另开 issue |
| S8 | 重放：before 查询以 `evaluated_at=released_at` 存 raw 与 sha256，重放按同 plan 校验（同 PR-2 `query_evidence_consistent`）；实测 16h 后重发结果逐字节等价向量，可行。 | 可逆默认 |
| S9 | 最小样本：before 窗与 after 窗同用 profile `minimum_samples`（3）。不足时该 scope 为 `INSUFFICIENT_SAMPLES:<scope>:<signal>`，对比记录 `partial`。连续发布间隔 < 3 个抓取的情况会落到这里（推断）。 | 可逆默认 |

## 未确认

- 多副本、maxUnavailable>0 的真实滚动、旧 Pod 在 before 窗内重启或被替换：本实验 `replicas=1`，均未测。
- released_at 晚于滚动的真实部署器行为：本实验 released_at = patch 回执时间。
- 连续发布间隔 < 3 分钟时的 `VERSION_MAPPING_INSUFFICIENT_SAMPLES`：按算法推断，未实测。
- Prometheus status 接口被 readonly proxy 挡住，保留期取自 Deployment 参数，抓取间隔取自 ConfigMap。
