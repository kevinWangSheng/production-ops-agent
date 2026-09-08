# 长期运行的真实业务对象：项目前头脑风暴

> 状态：历史讨论或研究证据，非当前规格。2026-09-06 已确认的产品边界以仓库根目录 SPEC.md 和 docs/adr/0001-readonly-investigation-boundary.md 为准；本文旧的候选、建议及待决表述不覆盖该共识。

> 用户后续明确：本轮先确定Agent是否值得做及生产标准的完整流程；具体工作负载后续讨论。本文仅为环境候选资料，以下“真实使用Immich”的推荐未被接受，不构成当前项目方向，也不要求用户经营其他个人应用。

日期：2026-09-06。仅研究官方文档，未部署或购买资源。本文讨论完整长期运行环境；此前实验场笔记不能代替项目全流程设计。下面三个是备选，只选一个真实愿意长期使用的业务，而不是全部部署。

本轮收敛建议：**一个长期使用的 Immich 实例 + 同配置、状态与凭据隔离的实验副本**。复用同一 chart/image/配置来源，环境差异显式记录；实验副本用获准复制的数据或专门测试资产，不能挂载真实实例的数据卷。当前只有讨论授权，任何既有照片、博客、文档业务迁移均未授权。

## 核心判断

真正长期使用的服务会产生版本变化、状态增长、备份、异步积压和用户体验问题。这比单纯保持 demo 容器运行更有运维学习价值。但个人服务不自动等于大规模生产：自己和家人的真实操作可以称为真实个人业务；定时脚本、压测和故障注入必须分别标记为 synthetic、load test、injected incident。

若目标是当前 Kubernetes 运维 Agent 的完整作品，**Immich 是条件性首选**：前提是愿意真正使用照片管理，并承担持续存储与恢复责任。**Paperless-ngx** 更适合有文档归档需求、希望研究异步处理的用户；**Ghost** 适合希望长期发布内容、对外展示运行状态的用户。选择应由真实使用意愿和目标岗位决定，不应为技术栈丰富而造一份没人用的业务。

## Immich：真实照片库与异步媒体处理

已查事实：官方建议 Docker Compose，同时目前明确提供 **official Helm chart**。最低 2 核/6 GB，推荐 4 核/8 GB；缩略图和视频转码平均额外占用照片库约 10–20%。Postgres 建议本地 SSD，不能放网络共享。[资源要求](https://docs.immich.app/install/requirements/)、[官方 Kubernetes 路线](https://docs.immich.app/install/kubernetes/)

资源原文：“RAM: Minimum 6GB, recommended 8GB.”；“CPU: Minimum 2 cores, recommended 4 cores.” [官方资源页](https://docs.immich.app/install/requirements/)

官方文档链接至 [immich-app/immich-charts](https://github.com/immich-app/immich-charts/blob/main/README.md)，实际 chart 地址为 `oci://ghcr.io/immich-app/immich-charts/immich`，旧 HTTP repo 已停止更新。该 chart 并非全栈一键交付：需预建 library PVC、自行提供带 VectorChord 扩展的 PostgreSQL，Redis 可启用或外接，需明确设置应用 `image.tag`，chart 不随每次应用版本发布更新。它比 Compose 增加数据库、卷与版本协调责任。

其 REST API 连接手机、网页和批量上传 CLI；包含 API、后台任务、ML 服务、Postgres、任务队列与文件存储。后台任务包括缩略图、元数据、视频转码和搜索。架构页自身对历史独立 microservices 容器与现行 server 执行 jobs 的叙述存在不一致，故这里只认定逻辑职责；实际容器数要按选定版本 Compose/chart 核对。[架构文档](https://docs.immich.app/developer/architecture/)

完整备份必须包含 DB 和图片/视频文件，内置 DB dump 不包含资产；官方建议停写保障一致性，不能停写时先备份 DB 再备份文件。[备份与恢复](https://docs.immich.app/administration/backup-and-restore/)

跨版本恢复可能需要 DB migration，不能把改回旧 image 当作完整回滚；官方升级资料还包含 pgvecto.rs→VectorChord 的扩展迁移。应固定兼容版本，在隔离副本完成备份恢复验证后再讨论真实升级。本文未验证任何具体版本迁移。[升级](https://docs.immich.app/install/upgrading/)、[恢复兼容性](https://docs.immich.app/administration/backup-and-restore/)

建议的真实业务链：手机上传→资产入库→后台生成缩略图/搜索数据→浏览和下载；持续维护上传成功率、任务最长等待、图片可读性、容量、备份新鲜度。自然学习问题是大批上传争用 CPU/IO、后台积压、卷空间增长、数据库连接异常、客户端/服务端升级兼容和资产恢复一致性。这是从架构推导的问题域，不是声称这些故障已经发生。

成本/复杂性：三者中存储和 CPU 峰值最突出；不要为了故障演练拿唯一照片副本冒险。真实实例长期提供个人服务；用独立数据副本/测试账户进行破坏性演练。Agent 可以调查这些问题，DB 迁移、数据恢复与存储变更仍由外部运维流程及人工控制，不能因业务需要而扩大 Agent 权限。

## Ghost：真实内容发布与读者访问

已查事实：官方 Docker Compose 工具仍标 preview；基础组成 Ghost+Caddy+MySQL，文档示例是 Linux 1 CPU/2 GB，要求域名/DNS 和事务邮件 SMTP；可选 ActivityPub 和分析服务。本次未找到官方 Kubernetes 安装路线，不能把第三方 chart 当官方支持。[Docker 文档](https://docs.ghost.org/install/docker)

官方备份文档明确 JSON 内容导出不含邮件分析、成员互动和评论；灾备需要直接备份数据库与 content 文件夹，提供 Docker 下备份/恢复说明。[备份文档](https://docs.ghost.org/faq/manual-backup)

建议的真实业务链：编辑→发布→页面/API访问→图片加载；若确实运营订阅，再加入邮件和成员。指标包括文章端到端可访问性、发布完成时延、图片错误、数据库延迟、证书有效期和备份恢复时间。自然学习问题包括升级后的路由/主题错误、反向代理配置、DB 连接、存储权限和外部 SMTP 失败。

成本/复杂性：基础业务轻，适合公开展示；但个人博客的真实流量可能很低，不能宣传为高并发生产。若强行移植到 K8s，新增的 chart、持久卷、探针和升级兼容维护是自己承担的工程工作；是否值得取决于目标岗位。如果只是为了观测而启用邮件、支付和社交联邦，反而增加外部依赖，缺乏真实业务理由。

## Paperless-ngx：真实文档入口、后台处理与搜索

已查事实：官方提供 Docker Compose 安装，推荐新安装采用 PostgreSQL；任务调度需要 Redis-compatible broker（Redis/Valkey）；Office 文档解析可使用 Tika/Gotenberg。本次未找到官方维护 Helm chart 的证明，配置文档提及 Kubernetes 不等于官方部署支持。[安装](https://docs.paperless-ngx.com/setup/)、[配置](https://docs.paperless-ngx.com/configuration/)

备份可用 document exporter，覆盖文档、缩略图、元数据和 DB；API token 不导出，导入需同版本。升级会执行数据库迁移，官方要求先停止消费和备份，当前 v3 有需人工处理的 breaking changes。[管理文档](https://docs.paperless-ngx.com/administration/)

本次未从官方文档确认统一 CPU/RAM 最低值。不能引用搜索结果中非官方 fork 的“2 GB最低”当官方要求。作为讨论预算可暂估 2–4 vCPU/4–8 GB 给低量文档处理实例，但这是待测规划值，OCR并发、大PDF和可选转换服务可能显著提高峰值。

建议的真实业务链：上传/扫描文件→消费与 OCR→持久化文档与元数据→搜索→下载原件。可持续处理个人真实资料或团队授权资料；合成文档仅做验证。自然学习问题是页面存活但消费卡住、OCR长尾、队列重复/失败、磁盘容量、检索新鲜度、broker不可达以及版本迁移后无法恢复旧备份。它能教会人区分 HTTP 健康和业务完成。

复杂性：状态比 Ghost 多、媒体计算一般比 Immich 少，但取决于输入文件；K8s 需要自己验证部署打包。真实资料可能敏感，遥测侧记录任务状态和脱敏诊断信息，不应默认把文档内容送进模型。

## 完整长期运行环境应包括什么

这是架构讨论，不把任何部分缩成“先做几个demo就够了”：

1. **真实服务面**：一个上述业务，明确用户、日常业务链、入口、身份、域名/TLS、持久状态及容量增长。真实使用与实验流量在指标中能分开。
2. **交付面**：固定上游版本和来源，自己维护配置/Helm values、镜像与依赖清单、CI、环境差异、变更记录；升级前在测试副本验证，再经批准发布。应用降级不自动代表 DB schema 可以降级。
3. **运行面**：业务探针、日志/指标/必要 traces、告警路由、容量、证书、重启与队列指标；告警以用户影响为主，保留人工值班/runbook途径。
4. **数据面**：DB+文件的一致性备份、异机/异地副本、保留周期、恢复演练、实测 RPO/RTO。运行机器和备份放在同一磁盘不构成独立灾备。
5. **Agent 面**：接收告警/发布事件、读取受限遥测、输出证据和 typed proposal；外部策略决定是否允许动作、执行器持有凭据，独立业务探针确认恢复；失败转人工。
6. **实验与反馈面**：单独故障副本验证 Agent 的策略和恢复；真实实例运行记录帮助发现覆盖空白，经过人工审阅再形成回归场景。LangSmith 负责 agent 调用/成本/反馈链，不能代替业务监控与灾备。

## 服务器数量的条件化判断

“几台”取决于要求哪种故障隔离，不是容器数量。以下均为规划推导，未经部署测量：

- 一台 Linux 主机可以承载单实例业务和初始 Agent，但该机掉线时业务、观测和 Agent 可能一起失效；适合承认这一边界的个人服务。
- **两台独立主机/VM** 能把业务与管理/外部探针分开，另加独立备份存储。这是长期个人运行的合理讨论起点；业务机对 Immich 可按 4 vCPU/8 GB 官方推荐起算，再增加 K8s/观测余量；管理面可先估 2–4 vCPU/4–8 GB，需测量。
- 若目标含节点故障与跨节点恢复，可以讨论 **1 control-plane + 2 worker，再加1台管理/探针**，不是宣称四台必需。单 control-plane 仍非 HA；跨 worker 恢复还受卷可迁移性、数据库和网络限制。单宿主机上的多个 VM 也不等于独立物理故障域。

完整项目预算还应计入：持续磁盘增长、独立备份、网络出口与域名、模型API、agent trace保存、监控retention、测试副本运行小时、升级与恢复的人工时间。不得用裸VM价格冒充总成本。本轮没有选云商地区，也没有真实流量/资源测量，因此不报固定总月费。

## 仍需用户决定的上游问题

真正愿意长期使用哪类服务；目标是 Agent 应用工程还是 SRE/平台工程；是否必须 Kubernetes；可接受月费与每周维护时间；能否接受个人服务短暂停机，以及数据丢失窗口。它们决定最终作品规模、服务器隔离与 Agent 权限，而不是先定技术栈后找使用理由。
