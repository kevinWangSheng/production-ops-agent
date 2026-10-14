# F11 PR-2 遥测探测（只读，2026-10-13）

目的：确认 U3「按版本/实例切片」在 kind 实验环境能否确定性实现。只读查询实验环境 Prometheus（调查侧只读账号，凭据不入库），未改集群、未注入故障、未调用模型。

## 结果

- `traces_span_metrics_calls_total{k8s_namespace_name="otel-demo",service_name="checkout"}`：13 条序列。标签含 `k8s_pod_name`、`host_name`（均为 Pod 名）、`service_instance_id`（= Pod UID）、`k8s_deployment_name`、`service_version`（恒为 `2.0.2`）。**没有** `k8s_replicaset_name`，尽管 `scripts/kind_lab/values.yaml:163` 声明提升该属性；与 2026-10-06 归档（`docs/evidence/m1-02-lab/regression/normal-1/observe-pre.json`，27 组标签、ReplicaSet 0 次）一致。
- `kube_pod_container_info`（kube-state-metrics 2.20.0）：checkout Pod 带 `uid`（与 span 的 `service_instance_id` 相同）、`image_id`（`…@sha256:<digest>`）、`pod`。
- `kube_pod_owner`：同一 `uid` → `owner_kind="ReplicaSet"`、`owner_name="checkout-<pod-template-hash>"`。
- `kube_replicaset_owner`：checkout 有两个 ReplicaSet（当前与历史）指向 Deployment `checkout`。
- `kube_replicaset_labels`：0 条（未暴露标签，pod-template-hash 只能从 ReplicaSet 名后缀取）。

## 结论

span 流量可经 `service_instance_id = uid` 关联到实例的 image digest 与所属 ReplicaSet（pod-template-hash），版本切片可确定性实现；`service_version` 不可用作版本身份。仅改配置的发布 image digest 不变，须靠 ReplicaSet/pod-template-hash 区分新旧实例。kube-state-metrics 的 Pod 信息序列在 Pod 删除后消失（staleness），旧实例证据须在其存续期间采集（属 PR-4 before/old 比较）。
