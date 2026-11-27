# 威胁模型

[English](threat-model.md) | 简体中文

范围：[deploy/compose/](../../deploy/compose/) 中的 Compose 安装（按 2026-11-10 的 main，发布 `v0.1.1`），在一台控制主机上通过只读遥测调查一个目标。产品必须满足的规则见 [PRODUCT-CONSTRAINTS.md](../../PRODUCT-CONSTRAINTS.md)；本页列出今天由什么强制执行每条规则、相应证据，以及哪些没有强制执行。本页没有经过安全专家审查，产品也从未面对过真实生产环境（见[限制](limitations.zh-CN.md)）。

## 资产

- 被调查的系统：不得被 Agent 改动。
- 凭据：模型 API key、目标读取凭据（Prometheus、Jaeger）、Observer 的 Prometheus 账号、四个数据库登录和 owner 密码、工作台密码，以及接收端的 bearer token。
- Agent 读取的业务遥测（指标、trace、错误信息）以及它存储的内容：证据、报告、复盘、知识。
- 人的决定：暂停、取消、关闭、重新打开、审批。之后的 Agent 结果永远不能覆盖它们。

## 信任边界

| 边界 | 另一侧是谁 | 信任它做什么 |
|---|---|---|
| Alertmanager / 事件接收 → web | 任何持有接收 token 的一方 | 只用于开事故；告警文本是不可信上下文 |
| 浏览器 → 工作台 | 持有 UI 密码的运维人员 | 人工控制与审核 |
| worker → 模型供应商（DeepSeek） | 外部 SaaS | 不信任；它接收已授权的证据，返回不可信文本 |
| worker / Observer → 目标遥测 | Prometheus、Jaeger | 只作为数据；工具结果是不可信证据 |
| worker → LangSmith（需主动开启） | 外部 SaaS | 只接收 id、版本、token 数和固定代码 |
| 主机用户 / Docker → 容器 | 管理控制主机的人 | 一切（见剩余风险） |

## 威胁与控制

| # | 威胁 | 由什么强制执行控制 | 证据 | 缺口 |
|---|---|---|---|---|
| T1 | Agent 改动被调查的系统（约束：不做生产变更） | 模型只能看到读取工具（`opspilot/tools/`）：默认 `fixture` profile 里一个，`otel-demo` profile 里五个；不存在 shell、SQL 或写入工具。在 lab 中，产品账号经过一个拒绝写入的只读代理访问 Prometheus | F7 代理探测 136/136（写入、管理、remote write 均 403）：[f7-authz-probe/run.md](../evidence/f7-authz-probe/run.md)；F3 第 6 步探测 [f3-step6-authz-probe](../evidence/f3-step6-authz-probe/probe.json) | 数据源侧的只读强制是部署方的责任。在 lab 之外，没有任何机制阻止运维人员给 worker 一个带写权限的凭据。lab 的 Jaeger 查询 API 没有认证 |
| T2 | 通过告警、trace 或日志的提示词注入改变目标、范围、预算或人的决定 | 目标身份、查询范围、预算和控制代数都在模型之外解析（执行器、数据库栅栏）；告警文本标记为不可信上下文 | F7 恶意输入 Run：[f7-hostile-input-live](../evidence/f7-hostile-input-live/)；[F1 结果](../results/f1-same-condition-comparison-2026-11.md)（英文）中的 F1 提示词注入场景 | 报告文本不受保护。在 F1 dev 考试中，OpsPilot 有一次尝试在报告里复述了注入的 canary（被确定性检查 `q20.canary_absent` 否决；见结果页） |
| T3 | 凭据泄漏到提示词、报告或导出的 trace 中 | 数据源响应在成为证据之前先脱敏（`opspilot/redaction.py`）；凭据从不作为工具参数；trace 导出不发送 payload | F7 PR-B 真实 Run 20/20（[任务记录](../tasks/2026-10-15-f7-hostile-input.md)） | 脱敏按名称进行：变量名看起来像凭据时，Pod env 的值会被隐藏，Kubernetes jq/tabular 工具在脱敏后的对象上运行（[#574](https://github.com/kevinWangSheng/production-ops-agent/issues/574)）；名称像凭据的命令行 flag 会隐藏其值（`--password=v`、跨行和注释的下一个 token、结构化数据中的下一个列表元素、JSON/YAML 文本以及转义 JSON；[#802](https://github.com/kevinWangSheng/production-ops-agent/issues/802)）；紧贴在较短凭据值后的凭据键值对会一并隐藏（[#803](https://github.com/kevinWangSheng/production-ops-agent/issues/803)）。仍会漏掉：没有命名上下文或用普通名称存放的密钥、`value` 写在 `name` 之前或多转义一层的 env 对、字符串里以 YAML 文本形式写的 env、ConfigMap 数据键（只适用通用规则，经 `envFrom` 使用时也一样）、base64 数据（`binaryData`、`@base64d`）、混合或双重 URL 编码、紧贴在前一个单词或 `}`/`)`/`]` 后面或写成 `/password` 的 flag、以 `-` 开头的空格分隔 flag 值、未加引号的 flag 值中在一个看起来像字符串结尾的引号之后的部分、列表之外逗号后面的 flag 值（有意为之）、带引号的整个 flag 字符串中空格之后的词（`"--password ab s3cret"`） |
| T4 | 遥测流向超出授权范围的第三方（数据流合同，C3 §12） | 证据只发给配置的模型供应商。LangSmith 导出默认关闭，开启时只携带 id、版本、token 数和固定代码（`opspilot/outbox_delivery.py`） | 从 LangSmith 读回的 outbox payload：[243-trace-outbox](../evidence/243-trace-outbox/README.md) | worker 容器的出站网络不受限制；不附带出口白名单。业务遥测按设计会发送给 DeepSeek |
| T5 | 被攻破或有缺陷的组件越出自己的角色 | 每个运行时使用独立的数据库登录，带显式授权和 role-guard 触发器（migration 0011、`opspilot/deploy/roles.py`）；Observer 有自己的 Prometheus 账号，从不加载模型 key | 在非 owner 角色下运行，权限错误为 0：[m1-pg-roles-live](../evidence/m1-pg-roles-live/) | worker 同时持有模型 key 和目标凭据，并在进程内运行工具执行器；C3 §3 的 Controller / Tool Gateway 分离没有实现（[#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)） |
| T6 | 未认证的调用方开事故或操作工作台 | 接收端需要 bearer token（以摘要存储）；工作台使用具名 Basic Auth、PBKDF2 哈希，并对状态变更检查 Origin（`opspilot/web/auth.py`）；工作台端口绑定在 `127.0.0.1` | F9 干净主机运行：运维人员 200，匿名 401（[f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md)） | 不附带 TLS 代理（C3 §9 指定了一个）；远程访问依赖 SSH 隧道。单团队，没有 SSO，没有按事故的授权 |
| T7 | 迟到或重复的 Agent 结果覆盖人的决定或更新的 Run | 租约、执行 epoch 和控制代数为每次写入设栅栏（C3 §4、§6–7；[ADR-0003](../adr/0003-business-state-recovery-authority.md)） | 在干净主机上杀 worker：[f9-steps-2-4](../evidence/f9-steps-2-4-2026-11-10/README.md) 第 3 步；[f2-kill-resume-live](../evidence/f2-kill-resume-live/README.md)；F7 第 4 步中的暂停测试 | 被杀进程打断的预算预留会停留在 `reserved`（[#527](https://github.com/kevinWangSheng/production-ops-agent/issues/527)）；影响的是费用核算，不是控制 |
| T8 | 费用失控或对目标造成过大负载 | 每个 Run 的模型请求上限、墙钟截止时间、单次调用超时、结果大小上限、积压上限（`opspilot/investigation/limits.py`）；暂停会停止新查询 | 72 小时 lab soak 的费用和卡住工作计数：[m3-soak-2026-11-07](../evidence/m3-soak-2026-11-07/README.md) | 没有全局费用上限；费用在事后对账 |
| T9 | 密钥在控制主机上暴露 | 密钥一次性生成到仓库之外的 `0600` 文件中；容器以非 root 的安装用户运行，根文件系统只读，丢弃所有 capability（`compose.yaml`）；compose 文件和镜像中没有密钥 | 干净安装证据：[f9-clean-install](../evidence/f9-clean-install-2026-11-09/README.md)、[f9-392-compose-install](../evidence/f9-392-compose-install/README.md) | 在控制主机上拥有 root 或 `docker` 组权限的任何人都能读到所有密钥，包括模型 key |

## 部署方必须自行承担的剩余风险

- 为目标提供只读凭据，并为每个数据源提供只读访问路径。
- 在工作台前面加上 TLS 和网络策略，并限制 worker 的出站流量。
- 决定哪些遥测可以发送给模型供应商。
- 把控制主机的管理员视为完全可信。
