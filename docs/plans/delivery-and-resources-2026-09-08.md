# 开发交付与资源安排

日期：2026-09-08。基础 CI/PR 已完成；本文件维护当前资源安排。用户本轮授权更新开工上下文与配置模板；不授权模型调用、采购或部署。产品范围和实施门槛不变。

## 本轮：基础 CI 与 PR

- 使用 GitHub Actions，PR 到 main、push main 和手动触发；Ubuntu 24.04，Python 3.12.13，uv 0.10.8，依赖使用 uv.lock。
- CI 仅运行 make setup 和 make check：环境诊断、离线锁检查、Ruff、pytest。无 dataset eval、模型调用、镜像发布或部署。
- Actions 固定完整提交 SHA；contents: read、checkout 不保留 Git 凭据、15 分钟超时、同 PR 取消旧运行、暂不共享缓存。不使用 pull_request_target、不注入业务凭据。
- PR 是交付入口：范围/依据、实际验证、风险和后续；最新提交 checks 成功并完成适用的独立审查后，依据授权合并。
- 默认 main、公开仓库（用户于 2026-09-08 授权变更）、合并后删除远程任务分支、Actions 默认只读且不可批准 PR 已配置。
- 初始私有仓库的保护 API 曾返回套餐限制 403；用户授权公开后，main 保护 PUT 成功。当前要求 GitHub Actions（app_id 15368）的 checks 成功并与 main 同步、解决讨论、管理员也受约束，禁止 force-push/删除。PR review 流程启用但审批人数为 0，不强制单人仓库自我审批；独立审查仍按 AGENTS 执行。

## 审查机器人

优先使用已有 Codex GitHub code review 集成，不在 CI 中新增模型 API key 或另购审查 SaaS。AGENTS.md 的 Code Review Rules 提供项目边界，机器人意见不能代替检查或用户决定。

启用需 Codex 设置中连接本仓库并开启 code review；自动审查还需相应开关。必须在真实 PR 上观察机器人反应/审查才算接入验证成功。GitHub CLI 当前 OAuth 无权列出 GitHub App installations，不能把查询失败解释成未安装。初始 GitHub 未连接问题已由用户完成配置。PR #1 的手动 Code Review / Security Review 已完成，唯一 P2 文档发现已修正并解决讨论，安全审查无发现。PR #1/#2 已合并，main CI 成功、本地同步和任务 worktree 清理完成；证据见[任务记录](../tasks/2026-09-08-github-ci.md)。每次推送自动复审仍未证明，应在后续真实 PR 核实；不能把手动成功称为自动启用。

## 资源与凭据：按阶段提供

- 基础 CI：GitHub 托管 runner；只使用 GitHub 自动提供的只读 GITHUB_TOKEN，不需要用户提供模型 key。私有仓库 Actions 用量受账号额度限制，无法运行时记录具体账单/配额错误，不自动升级付费额度。
- 模型协议实验：需要 DeepSeek 账号的有效 key、实际可用 profile 与已授权调用预算。建议本地被忽略的 .env.local 存 DEEPSEEK_API_KEY；[配置模板](../../.env.example)的秘密值为空，非秘密模型配置沿用 C3。未来实验入口需显式加载指定文件或进程环境；当前代码尚无 dotenv 自动加载能力。
- Trace/eval 接入：需要 LangSmith 项目/凭据与授权数据出口。LANGSMITH_API_KEY 同样仅在本地私有配置或任务特定的 GitHub Secrets；LANGSMITH_PROJECT 等非秘密配置可版本管理。只导出 C3 白名单，不上传完整 Agent state。
- 实验环境：先测可用内存、CPU、磁盘和容器 VM 配额；Docker daemon 当前不可达，kubectl/Helm 尚未就绪。实测后选择本地或隔离 Linux 资源，不把候选容量当已可用资源，不采购常驻双 VM/HA。
- PostgreSQL：开发用数据库与目标系统隔离，使用任务专用库/身份；连接凭据保持本地配置，生产/目标查询身份与工程管理身份分离，不进入模型或 trace。
- Dataset/eval：单独 workflow 或本地实验入口，初期仅人工触发；固定数据、模型/prompt/tool/adapter/evaluator 版本、预算和超时，隔离开发与保留数据，保留失败与独立评分。需完成具体合同后才接入，不在每个 PR 自动运行。
- 镜像与部署：有可运行产品后选择镜像仓库（优先评估 GHCR）、固定 digest、测试环境、部署身份、升级/回滚/在途任务兼容。若目标支持，优先短期身份/OIDC；否则任务限定 Secrets。GitHub 环境审批和保护也需核对私有仓库套餐能力，不能假设可用。
- UI：已有 Jinja/SSE 技术选择，先用本地运行页面、浏览器自动化与截图验收。Figma 仅在需要设计稿交接、多人协作或复用资产时接入，不是前置依赖；浏览器工具实际可用性随首个 UI 任务验证。
- 无需本轮增加：第三方审查订阅、向量 Wiki、额外 MCP、通用执行编排或自托管 runner。

用户不在聊天、文档、源码或 PR 中提供 key；需要时由用户在相应私有配置界面录入，Agent 只验证存在与最小连通性，不打印值。基础 PR CI 永不获得业务或部署 Secrets。当前记录的是配置约定，不代表这些服务已经连接。

## 后续完成条件

- [x] 用户授权仓库公开后配置 main 服务端保护；API 回读确认必需 checks、strict、管理员约束和禁止强推/删除。尚未通过故意违规 push 做破坏性探测。
- [x] 审查机器人已在真实 PR #1 响应手动请求。
- [x] 手动机器人审查结果已处理；PR #1/#2 合并、main CI、本地同步与任务 worktree 清理已完成。
- [ ] 在后续真实 PR 验证自动复审方式，未验证时继续遵守独立审查约定。
- [ ] 测量机器内存和实验资源，准备首个有界 M0 环境及凭据。
- [ ] 完成真实模型/上下文/恢复/权限实验与 eval 校准，再接入人工触发的评测。
- [ ] 有运行产品后接入镜像构建、测试环境部署与 smoke 验证。
- [ ] CD 上线、独立上线检查、升级/恢复与 72 小时 soak；仍不构成真实生产证明。

## 首次配置：谁提供什么

首个任务见 [M0-01 资源与入口准备](../tasks/2026-09-08-m0-01-preflight.md)。`.env.example` 是配置合同模板，当前无加载器；填值不会触发调用，也不代表权限、预算或连通性已通过。未来入口必须显式指定私有文件，按数据解析，不用 shell source/执行配置内容；进程环境与文件冲突时拒绝启动，避免误用其他项目账号。

- 用户需要提供：DeepSeek API key；LangSmith API key、所属区域 endpoint 和项目归属（key 授权多个 workspace 时提供 workspace ID）；首轮模型/trace 实验的实际费用上限和期限。私有值仅录入任务使用的 `.env.local` 或服务私有配置界面，不发到聊天。基础 CI 不接收这些值。
- 模型已有决定：DeepSeek 官方 `deepseek-v4-pro`、thinking enabled、high；由助手验证实际可用性、锁版本、记录调用日期及返回版本。换模型或降低质量不得静默处理。尚未选择独立 judge profile，M0 校准时提出方案；本轮不需购买另一模型。
- 助手负责：实验脚本、依赖锁、PostgreSQL 本地实验身份、OTel Demo 版本及遥测映射、端口/卷/命名空间、只读权限与注入身份隔离、查询配置、证据留存和清理。用户无需逐项指定数据库口令或 Kubernetes 标签。现有私有资源仅在用户决定复用时提供精确访问范围，不能默认连接生产。
- 资源选择：2026-09-08 只读盘点为 16 GiB 物理内存、10 逻辑 CPU、约 44 GiB 可用磁盘；不是可分配容量。Docker/Compose 客户端可用但 daemon 不可达，kubectl/Helm 缺失。助手先准备本地机制实验的容量方案；完整实验需要云资源时，再给出供应商、容量、期限、总价上限和退出方案，用户只做采购/复用决定。无需现在交付云主账号或服务器。
- 后续才需要：部署地址/TLS、镜像与部署身份、72 小时运行窗口和预算。UI 首次运行时验证浏览器能力；基础 GitHub 接入已完成。

预算模板默认 0，含义是尚未获准付费调用；填非零值本身不是授权证据。实验任务记录保存授权依据、额度、期限及既有消耗；后续入口必须在调用前校验并执行累计预算，不能只设置单次 max_tokens。每个 worktree 的私有文件使用任务专用路径，不能假设会随 Git 自动复制。

## 状态维护与 hook 选择

本次已存在维护要求，漏项表现为完成结果追加到了任务尾部和 ignored 进度，引用计划的当前待办没有同步；无法仅由工件证明是 context rot。先使用 AGENTS 的收尾核对及 PR 模板提示，不新增宿主 hook。语义 hook 难以判断实验/验收是否真的完成，Git hook 也无法在提交前证明合并后的 CI/清理结果。

若后续真实任务再次出现同类遗漏，先固定可机械判定的失败案例，再将小检查接入现有 make check/CI；需要宿主提醒时再验证对应宿主的触发时机和支持情况。检查只报告不一致，不自动勾选 passes、不改历史、不自动提交。此决定不声称规则已能消除长上下文遗漏。

## 依据

- [LangSmith key/endpoint/workspace 配置](https://docs.langchain.com/langsmith/create-account-api-key)与[区域识别](https://docs.langchain.com/langsmith/regions-faq)，2026-09-08 核查；本项目按 C3 禁用全量自动 tracing，使用白名单出口。

- [M0 执行计划](m0-validation-plan-2026-09-07.md)与[C3 技术方案](../design/technical-proposal-2026-09-07.md)。
- [uv 与 GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)。
- [GitHub 分支保护适用套餐](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)。
- [Codex GitHub review 配置](https://learn.chatgpt.com/docs/third-party/github)。
