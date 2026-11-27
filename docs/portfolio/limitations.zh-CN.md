# 限制

[English](limitations.md) | 简体中文

截至 2026-11-11，本仓库中的证据能说明什么、不能说明什么；2026-11-17 至 2026-11-22 的基准运行补充在[评测](#评测)一节。

## 没有生产观察

OpsPilot 从未针对真实的生产系统部署过。本仓库中没有任何形式的生产证据。

M3 的每一次运行、测试和测量都使用同一个 lab：kind 集群上的 OpenTelemetry Demo，由 [scripts/kind_lab.py](../../scripts/kind_lab.py) 创建，运行在本地 VM 或一台租用的共享 vCPU 主机上。之后的基准运行使用各基准自带的 fixture 和应用，跑在租用主机上的 kind 集群里。

- **流量是合成的。** 来自 demo 自带的 load generator。部分运行中还直接向接收端点投递了 Alertmanager 格式的告警。
- **故障是注入的。** 几乎都是用 `kind_lab.py fault inject` 切换的 flagd feature flag，大多是 `paymentFailure`。F1 考试和 soak 另外加了一份固定白名单中的故障、噪声开关和运维演练（停掉 worker、切断模型出口、隧道中断）。
- **真实的部分：** 模型（DeepSeek `deepseek-flash`）、lab 中的开源软件（Prometheus、Jaeger、Alertmanager 和 demo 服务）以及产品镜像。事故本身是安排出来的。

所有证据页都标明自己是集成运行、演练或 lab soak。它们都不是生产证明。

## 尝试过的范围

- 一个目标工作负载（demo 的 checkout 路径）、一个 HealthProfile、一家模型供应商、一个团队、每个运行时一个副本。
- M3 证据只用了 Prometheus 和 Jaeger 工具。之后加入的只读 Kubernetes、日志、数据库、Kafka、Argo CD、Helm 和 Redis/Valkey 工具（`otel-demo-ext` profile）只在下面的基准运行中用过，没有做过 M3 那样的验收或 soak。
- 发布观察只对 dev kind 集群授权。校准清单（[release-calibrations.json](../../opspilot/observation/policies/release-calibrations.json)）把目标钉在该集群的 `cluster_uid` 上，路径固定在 `release_policy.py` 中。新的集群（租用主机、staging 集群）不会自动获得发布观察；租用主机上的安装甚至用 `cluster_uid` `opspilot-m1` 注册 lab 目标，而清单与它不匹配。要在另一个集群上使用发布观察，需要一个经用户批准的 PR 为该集群添加一条清单条目，沿用原校准的数值和证据，其 `approval_ref` 写明没有在该集群上重新校准（[#391](https://github.com/kevinWangSheng/production-ops-agent/issues/391)，已作为已知限制关闭）。
- soak 只跑过一次，持续 72 小时。控制侧运行在一台与其他负载共享的主机上，目标侧运行在共享 vCPU 主机上（[m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md)）。它没有数值目标，其延迟和资源数字只描述那一次运行。

## 评测

- 与 HolmesGPT 的同条件对比规模很小：12 个配对的 dev 场景和 6 个配对的 holdout 场景，各 3 次尝试。所有置信区间都有重叠。
- LLM judge 没有对照人工标注做校准，所以 F1 第 5 步未达成（[#295](https://github.com/kevinWangSheng/production-ops-agent/issues/295)）。
- holdout 集由同一环境中的一个独立会话编写。没有技术隔离，测试不是盲测。公开之后，它就算作已见过的题。
- 考试开始后修复了评测脚本。这些修复在[结果页](../results/f1-same-condition-comparison-2026-11.md)（英文）上公开说明。
- 没有做延迟对比，因为考试主机是共享 vCPU。
- HolmesGPT 公开评测集（53 题 × 3 遍，同模型、同 judge）：2026-11-21 的全量运行中 OpsPilot 得 133/159，HolmesGPT 得 150/159。修复后的 145/159 由 2026-11-22 只跑 OpsPilot 的定向重跑拼合而成，不是单次全量运行；HolmesGPT 没有重跑，两边都没有给结果摆动的题追加尝试。HolmesGPT 按上游默认配置运行，其中包括 OpsPilot 不提供的工具；它在数据库组丢掉的 5 遍是 pytest 报错或跳过、没有回答，是否为上游 harness 的问题未确认。见 [README](../../README.zh-CN.md#评测结果2026-11) 和[重跑记录](../evidence/rental-2026-11-20/rerun-2026-11-22/run.md)。
- 基准 judge 是 Z.ai `glm-5.3-flashx`，不是上游评测用的 judge，同样没有对照人工标注校准。
- ITBench-Lite（82/105）是官方评测器在离线快照上的根因 pass@1，用的是上面那个 judge；按 OpsPilot 自己的确定性实体匹配，同样的 trial 是 22/105，而且大多数结论是 `partial`。ITBench-Lite 和 SREGym 上没有跑 HolmesGPT。
- 失败题背后的已知缺口：回看窗按设计固定为 24 小时（[#779](https://github.com/kevinWangSheng/production-ops-agent/issues/779)）；没有按目标常驻的操作员说明作为输入（[#780](https://github.com/kevinWangSheng/production-ops-agent/issues/780)）；SREGym 第 p12 题（Valkey，0/3）没有可交给 Redis/Valkey 工具的只读账号（[#773](https://github.com/kevinWangSheng/production-ops-agent/issues/773)）。

## 验收状态

[feature_list.json](../../feature_list.json) 显示 11 个功能中有 9 个通过。以下两个仍未完成：

| 功能 | 未完成项 | 证据 |
|---|---|---|
| F1 评测 | 第 5 步（校准后的判分）未达成 | [结果](../results/f1-same-condition-comparison-2026-11.md)（英文） |
| F3 证据优先的调查 | 第 2 步：在 #646 引入的报告提示词下，两次受审 Run 中有一次在 summary 里把假设写成了事实；2026-11-16 改回未通过 | [#664](https://github.com/kevinWangSheng/production-ops-agent/issues/664) |

F9 交付只凭 lab 证据通过。一位独立审查者判定第 1、3、5 步达成，第 2、4 步有限制地达成。证据：第 1 步[干净主机终轮](../evidence/f9-final-clean-host-2026-11-11/README.md)，第 3 步[第 2–4 步](../evidence/f9-steps-2-4-2026-11-10/README.md)，第 2、4 步[第二次终轮](../evidence/f9-final2-2026-11-11/README.md)，第 5 步即本作品集。限制如下：

- 这是在一台租用 VM 上、配合 kind lab、合成流量和注入故障的集成运行。它不是生产证明，也不是 soak。
- 第 2 步：交接是通过切断模型出口约 2.2 秒强制触发的。交接之前没有发布报告，失败的模型调用细节没有导出。
- 第 2 步：主机记录（系统日志里的一条 `su`、ConfigMap 的 `managedFields`）佐证了外部故障恢复的时间为 21:59:59Z，在接手与 `register_remediation` 之间。它们无法说明是谁执行的，因为用户和 Agent 从同一台机器、用同一个 key 连接。「是用户执行的」依据的是用户本人的说法加上这些佐证。
- 第 2 步：复盘仍处于 `under_review`，知识库中有 0 条。
- 第 4 步：`v0.1.0` 和 `v0.1.1` 的运行时代码和 schema head 相同，所以升级没有跨越任何代码或 schema 变更。staging 是同一台 VM 上的第二个 Compose 项目，加入 lab 需要两个文档未记载的步骤（不带别名加入网络、告警转发）。staging 上一次进行中的恢复观察跨过了 digest 切换并到达 `recovery_confirmed`；主项目的升级和回滚过程中没有进行中的工作。
- 那次 staging 观察中两个 `no_data` 采样的原因不明。这些运行没有 LangSmith trace，也没有按供应商余额做费用对账。

调查质量参差不齐。已发布的报告常常是 `partial` 而不是 `supported`；例如干净主机上的三个 Run 中有两个是 `partial`（[第 2–4 步](../evidence/f9-steps-2-4-2026-11-10/README.md)）。M1-01 的严格标准（任何被接受的 Run 都没有未解决的 P1/P2 审查发现）从未达到（[收尾记录](../tasks/2026-09-28-m1-01-closure.md)）。

## 安全与架构缺口

- worker 同时持有模型 key 和目标读取凭据。C3 的 Controller / Tool Gateway 分离没有实现：web 进程充当 Controller，worker 在自己的租约和代数栅栏下写入自己 Run 的步骤、证据和结论（受数据库授权和 role-guard 触发器约束），各运行时共用一个网络（[#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)，已作为已知限制关闭；C3 §3）。
- 不附带 TLS 代理，也不附带出站白名单。
- 对数据源的只读访问取决于部署方提供的凭据。
- 凭据脱敏依据名称进行。Pod 环境变量按变量名判断：名称像凭据（env 名中的 `PASS`/`PWD` 也算）时隐藏 `env[].value`，JSON 文本中同样的 `"name":"X","value":…` 对也会隐藏；Kubernetes jq 和 tabular 工具在运行模型给出的表达式之前先对集群对象脱敏（[#574](https://github.com/kevinWangSheng/production-ops-agent/issues/574)）。文本形式会匹配任何 `"name":X,"value":Y` JSON，不只是 env 条目，所以会过度脱敏 `{"name":"golden_pass","value":"3/3"}` 或 `{"name":"auth_token_ttl","value":3600}` 这样的普通键值对（已接受）。名称像凭据的命令行 flag 会隐藏它的值：连写形式（`--db-password=v`、`--password: v`），或作为下一个 token——可跨空格、换行、空行、`\` 续行、`# comment` 和 YAML 列表项——也可以是 `args` 列表、文本中的 JSON 或 Python 列表（包括在 JSON 字符串里被转义的情况，例如 `last-applied-configuration` 注解）或 YAML flow 列表中的下一个元素。任何不以 `-` 开头的下一个 token 都会被隐藏，所以这类 flag 之后的普通单词或下一行日志（`--session S`、`--auth-local and`）也会被隐藏，值里未闭合的引号会让后面的全部文本被隐藏；`--max-tokens 4000` 保持不变（已接受；[#802](https://github.com/kevinWangSheng/production-ops-agent/issues/802)）。紧贴在凭据值后面的凭据键值对（`"password":x&"secret": "v"`、`password=x|token :v`、`token: "password": "v"`，以及带认证 scheme 的 `password=x|authorization=Bearer v`）会在其他所有规则运行之后被隐藏，一直到下一个空白或逗号为止，所以同一段里紧贴着的普通文本也会被隐藏（已接受；[#803](https://github.com/kevinWangSheng/production-ops-agent/issues/803)）。它仍会漏掉：没有命名上下文的密钥、用普通名称存放的密钥（`STRIPE=sk_live…`）、`value` 写在 `name` 之前或多转义一层的 env 对、字符串里以 YAML 文本写的 env（ConfigMap 中的 `deploy.yaml`）、ConfigMap 数据键（不被当作 env 名，所以 `DB_PASS` 这类键只适用通用规则，经 `envFrom` 使用时也一样）、base64 数据（`binaryData`、`@base64d`；没有命名上下文）、混合或双重 URL 编码，以及以下 flag 形式：紧贴在前面的字母、数字、`_`、`.`、`-`、`}`、`)` 或 `]` 之后的 flag（`{{ .Release.Name }}-credentials`）、`/password` 或 Unicode 破折号形式的 flag、本身以 `-` 开头的空格分隔值（`--password -x`，会被读成下一个 flag）、未加引号的值中在一个看起来像外层字符串结尾的引号之后的部分（`--password ab",x`）、转义单引号内带空格的值（`\'a b\'`）、flag 和它的值分处不同嵌套列表（`[["--password"], ["v"]]`）、列表之外逗号后的值（`use --token, s3cret then`；这是有意收窄，以免把行文标点当作分隔符）、带引号的整个 flag 字符串中空格之后的词（`"--password ab s3cret"` 会保留 `s3cret`，就像 `password=a b` 会保留 `b`）。Redis/Valkey 的密码指令（`requirepass`、`masterauth`、`primaryauth`）算作凭据名，它们的配置行形式（redis.conf 中的 `requirepass v`）会隐藏同一行的下一个词（命令列表中单独的 `requirepass` 元素，无论是结构化的还是 JSON 文本中带引号的，都会隐藏下一个元素；未加引号的 YAML flow 列表 `[requirepass, v]` 不被识别），所以普通行文中 `requirepass` 之后的词也会被隐藏，而在 JSON 转义的 redis.conf 中，被隐藏的词会越过转义的 `\n` 延伸到下一行。本身以带引号的凭据键值对结尾的赋值值不会隐藏那个键值对的值（`password: x"api-key=": v` 会显示 `v`；旧的名称此前就如此，现在 Redis 名称也一样），还有少数其他刻意构造的、由凭据名接入另一个凭据结构的链条，可能让那个结构的值保持可见（已接受；[#818](https://github.com/kevinWangSheng/production-ops-agent/issues/818)，在 [#841](https://github.com/kevinWangSheng/production-ops-agent/issues/841) 跟踪）。
- 在一次 F1 尝试中，注入的 canary 进入了报告。

见[威胁模型](threat-model.zh-CN.md)。

## 已知的行为限制

2026-11-24 作为已知限制关闭，并记录在 C3 中：

- 接手之后不能再交还给 Agent。要让 Agent 重新调查，需要关闭事故；下一次告警会开启新事故（[#124](https://github.com/kevinWangSheng/production-ops-agent/issues/124)，C3 §10）。
- 恢复观察在 Deployment 层面检查副本可用性。在两次抓取之间（lab 中为 60 秒）发生又消失的 pod 重启看不到（[#134](https://github.com/kevinWangSheng/production-ops-agent/issues/134)）。
- Observer 不输出 trace。只改 Observer 的变更以冻结的 `summary.json`、离线重放和 Observer 的 `/metrics` 作为证据（[#159](https://github.com/kevinWangSheng/production-ops-agent/issues/159)）。
- 关闭之后只清理五类原始 payload，其他都不清理：Run 步骤（模型回复和工具结果）、人工消息、Run 输入快照、原始问题、Chat 面板和反馈都随事故保留，因为恢复和追问要依靠它们重建；报告、审计记录和知识也都保留，没有 90 天删除或归档（[#374](https://github.com/kevinWangSheng/production-ops-agent/issues/374)、[#375](https://github.com/kevinWangSheng/production-ops-agent/issues/375)，C3 §11）。

## 已知缺陷

- 未解决的问题在 [GitHub Issues](https://github.com/kevinWangSheng/production-ops-agent/issues?q=is%3Aopen+label%3Apost-m3) 中带 `post-m3` 标签。

## 文档语言

合同、任务记录和大部分证据页是中文。README 和这些作品集页面有中英两版，结果页是英文。
