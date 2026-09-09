# 固定 OTel Demo 实验环境证据

2026-09-09；实验进行中。此记录是 M0 环境/遥测证据，不是产品实现或 F6/F7/F14 完整验收。

## 实际环境和版本

专属 Colima `m0-otel` / Docker context `colima-m0-otel`，aarch64、4 CPU、6 GiB、24 GiB稀疏盘；原default保持Stopped。[资源盘点](resource-inventory.json)和[真实VM挂载](vm-mounts.txt)。VM仅挂任务tmp目录rw和Colima cache ro，没有用户home或主项目.env。操作必须继续在 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-environment`；将分支合入其他worktree不迁移tmp或VM。

OTel Demo 2.0.2 / `63649d6d6a59de88fb421b88c3c3a6185b6d21ad`，公开tar及Holmes固定源[下载hash](source-downloads.json)。实际上游25服务，另1只读代理：[26项镜像digest](image-lock.json)。第一次部署前全部改为RepoDigest引用；没有本地build或latest镜像。上游模板DEMO_VERSION默认latest，因此显式2.0.2；上游公开.env是无凭据模板，未读取项目私有.env。

必要实验差异：专属容器名/network、仅loopback端口；Collector去掉宿主根目录/socket挂载及host/docker receivers；Prometheus提升integration/version并固定5m lookback；Grafana关闭未固定版本的自动插件下载；增加Prometheus/OpenSearch持久卷。组件源码未修改。基于实际负载，关闭load-generator额外Chromium流量，LOCUST_USERS从5改2，保留HTTP购物与checkout流量。浏览器覆盖因此不属于本轮。

## 实际失败与修复

- 首次Prometheus退出2，原因是实验生成器以`service.name`前缀替换误匹配`service.namespace`，重复提升integration/version。原[错误日志](prometheus-first-failure.txt)保留；精确完整行匹配修复，重启后真实指标可查。
- 最初几秒数据尚未就绪：[first-normal-attempt](first-normal-attempt/capture.json)的up/target_info空、Jaeger仅accounting；不能登记为正常。后续正常窗口单独采集。
- 首轮资源测量中Chromium load-generator约886 MiB/139% CPU、Kafka103%、OpenSearch128%。减载后[stats](stats-after-load-reduction.txt)中load-generator约54.6 MiB/0.16% CPU，Kafka4.91%，OpenSearch39.39%；未停止其他项目。宿主free31%，swap13.6 GiB，无启动前swap基线，不能将全部swap归因本次实验。
- 只读proxy日志初稿字段未根据实际mapping，不作结果。实际mapping为`resource.service.name.keyword`及`@timestamp`，已适配并查询cart真实记录。初始checkout/payment/frontend无OTLP日志，不把0命中当健康。

## 无注入基线遥测与身份

本记录“正常”仅指未主动注入故障的对照，11个公开默认故障flag在工程盘点时均off，不预设服务必然健康。

正常独立观察窗口：epoch秒`1788975149.661 → 1788975449.661`。见[调用量](normal-calls.json)、[支付完成量](normal-payments.json)、[实际身份标签](normal-identity.json)。5分钟Prometheus increase含外推：checkout spans112.80、payment spans15.04、交易8.59；所有已有error series增量0。不是长期健康或独立恢复认证。原正常案同窗RPC server p95 checkout为10000ms；较晚新5m为71.25ms，见[尾延迟复核](normal-latency-followup.json)。旧尾延迟是正常对照的限制，不能以零error或新窗口覆盖，也未证明具体成因。

实际metrics包括`traces_span_metrics_calls_total{service_name,status_code}`、`app_payment_transactions_total{service_name,service_version,opspilot_integration_id}`及HTTP/RPC duration系列。支付指标实际integration=`m0-otel-20260909`、version=`2.0.2`；[部署注册](deployment-registry.json)保存容器ID、image ID/digest、创建/启动时间、专属network身份。没有Kubernetes UID、deployment/pod API或RBAC证据。

[proxy checkout traces](proxy-normal-traces-manifest.json)真实9条；[cart logs](proxy-normal-logs-cart.json)真实166条匹配，接口返回最新20条。日志、指标、trace在**整个专属Demo接入**可用；checkout/payment/frontend未导出OTLP日志，属于按服务的instrumentation gap。cart日志有真实container.id、service/version/integration；frontend-proxy日志缺integration/version属性，只有专属实例和部署注册外部关联，不能声称每条遥测都有统一身份字段。

## 查询和权限边界

固定接入为整个Demo实例`m0-otel-20260909`，包括服务依赖。`metrics`是end时刻instant PromQL，允许该实例全部指标，**不是单服务隔离或range API**。声明窗口至少300秒，Prometheus明确5m lookback；禁止offset/@和超过声明窗的range/subquery。日志/trace还需允许服务名，最多20条；单源响应最多1 MiB，超限413/incomplete；源请求10秒，Prometheus查询5秒。不开放flagd配置或注入器。

真实HTTP拒绝：[初始拒绝](proxy-denial-initial.json)含错误接入403、写405、未知后端/服务和非法查询400；修复后新增[redirect拒绝](redirect-denial.json)：真实302未跟随，目标命中0。代理200次是当前进程本地额度，重启不持久；Holmes runner另固定每案工具次数/共享模型预算，不宣称跨重启查询预算验证。

内部调查网络测试：[DNS/宿主/外网/socket](network-denials.json)及[直接IP](network-direct-ip.json)。uid65534、无capabilities/host/socket挂载的探针可访问proxy，不能直接访问Prometheus/OpenSearch、宿主映射端口或Internet。此为该容器网络权限证据。若Holmes实际运行在宿主，须单列该执行方式差异，不能用探针代替其自身OS隔离证明。

实际PromQL语义错误也保留：原Holmes normal-01查询得到502，重放确认backend422 `vector cannot contain metrics with the same labelset`；代理已安全分类为400并提供官方source_error，见[promql-error-reproduction](promql-error-reproduction.json)。原失败未删除。

## 保留和后续

Prometheus1h留存且持久卷；OpenSearch持久卷、当前未设自动删除；Jaeger内存最多25000 traces，无时间/跨进程持久保证，停止前须导出需要的trace。日志/trace源原始证据和所有历史失败保留。只读代理不接收凭据、任意URL、shell或SQL。

尚未：故障与恢复观察、Holmes正常/故障对照结果整合、完整权限/数据出口合同、Kubernetes HealthProfile、72h soak。结束使用compose stop和colima stop专属profile，保留容器/卷/数据；不使用down或删除虚机。
