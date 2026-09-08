# 开发交付与资源安排

日期：2026-09-08。用户授权本轮基础 CI、PR 约定和基础配置；产品/C3/M0 合同不变。将后续事项保存在仓库，避免仅依赖会话记忆；机器内存容量同时作为实验资源前提核查。

## 本轮：基础 CI 与 PR

- 使用 GitHub Actions，PR 到 main、push main 和手动触发；Ubuntu 24.04，Python 3.12.13，uv 0.10.8，依赖使用 uv.lock。
- CI 仅运行 make setup 和 make check：环境诊断、离线锁检查、Ruff、pytest。无 dataset eval、模型调用、镜像发布或部署。
- Actions 固定完整提交 SHA；contents: read、checkout 不保留 Git 凭据、15 分钟超时、同 PR 取消旧运行、暂不共享缓存。不使用 pull_request_target、不注入业务凭据。
- PR 是交付入口：范围/依据、实际验证、风险和后续；最新提交 checks 成功并完成适用的独立审查后，依据授权合并。
- 默认 main、私有仓库、合并后删除远程任务分支、Actions 默认只读且不可批准 PR 已配置。
- GitHub API 对当前私有仓库的 rulesets 和 branch protection 均返回 403，提示升级 Pro 或公开仓库。保持私有，不升级套餐；PR/检查规则目前是操作约定，尚无服务端强制保证。解锁后配置禁止直接推 main/force-push/删除，要求 checks 和解决审查讨论；不设置作者本人无法满足的强制自我审批。

## 审查机器人

优先使用已有 Codex GitHub code review 集成，不在 CI 中新增模型 API key 或另购审查 SaaS。AGENTS.md 的 Code Review Rules 提供项目边界，机器人意见不能代替检查或用户决定。

启用需 Codex 设置中连接本仓库并开启 code review；自动审查还需相应开关。必须在真实 PR 上观察机器人反应/审查才算接入验证成功。GitHub CLI 当前 OAuth 无权列出 GitHub App installations，不能把查询失败解释成未安装。当前 Codex 设置页面明确显示 GitHub 账号未连接；需要用户完成连接授权，再针对本仓库开启 review 并验证。此项尚未完成，保留独立本地 Agent 审查，不宣称机器人已启用。

## 资源与凭据：按阶段提供

- 基础 CI：GitHub 托管 runner；只使用 GitHub 自动提供的只读 GITHUB_TOKEN，不需要用户提供模型 key。私有仓库 Actions 用量受账号额度限制，无法运行时记录具体账单/配额错误，不自动升级付费额度。
- 模型协议实验：需要 DeepSeek 账号的有效 key、实际可用 profile 与已授权调用预算。建议本地被忽略的 .env.local 存 DEEPSEEK_API_KEY；示例配置只写变量名和空值。未来实验入口需显式加载指定文件或进程环境；当前代码尚无 dotenv 自动加载能力。
- Trace/eval 接入：需要 LangSmith 项目/凭据与授权数据出口。LANGSMITH_API_KEY 同样仅在本地私有配置或任务特定的 GitHub Secrets；LANGSMITH_PROJECT 等非秘密配置可版本管理。只导出 C3 白名单，不上传完整 Agent state。
- 实验环境：先测可用内存、CPU、磁盘和容器 VM 配额；Docker daemon 当前不可达，kubectl/Helm 尚未就绪。实测后选择本地或隔离 Linux 资源，不把候选容量当已可用资源，不采购常驻双 VM/HA。
- PostgreSQL：开发用数据库与目标系统隔离，使用任务专用库/身份；连接凭据保持本地配置，生产/目标查询身份与工程管理身份分离，不进入模型或 trace。
- Dataset/eval：单独 workflow 或本地实验入口，初期仅人工触发；固定数据、模型/prompt/tool/adapter/evaluator 版本、预算和超时，隔离开发与保留数据，保留失败与独立评分。需完成具体合同后才接入，不在每个 PR 自动运行。
- 镜像与部署：有可运行产品后选择镜像仓库（优先评估 GHCR）、固定 digest、测试环境、部署身份、升级/回滚/在途任务兼容。若目标支持，优先短期身份/OIDC；否则任务限定 Secrets。GitHub 环境审批和保护也需核对私有仓库套餐能力，不能假设可用。
- UI：已有 Jinja/SSE 技术选择，先用本地运行页面、浏览器自动化与截图验收。Figma 仅在需要设计稿交接、多人协作或复用资产时接入，不是前置依赖；浏览器工具实际可用性随首个 UI 任务验证。
- 无需本轮增加：第三方审查订阅、向量 Wiki、额外 MCP、通用执行编排或自托管 runner。

用户不在聊天、文档、源码或 PR 中提供 key；需要时由用户在相应私有配置界面录入，Agent 只验证存在与最小连通性，不打印值。基础 PR CI 永不获得业务或部署 Secrets。当前记录的是配置约定，不代表这些服务已经连接。

## 后续完成条件

- [ ] 解锁并实际验证 main 服务端保护（套餐/可见性决定由用户作出）。
- [ ] 验证审查机器人对本仓库真实 PR 的响应及复审方式。
- [ ] 测量机器内存和实验资源，准备首个有界 M0 环境及凭据。
- [ ] 完成真实模型/上下文/恢复/权限实验与 eval 校准，再接入人工触发的评测。
- [ ] 有运行产品后接入镜像构建、测试环境部署与 smoke 验证。
- [ ] CD 上线、独立上线检查、升级/恢复与 72 小时 soak；仍不构成真实生产证明。

## 依据

- [M0 执行计划](m0-validation-plan-2026-09-07.md)与[C3 技术方案](../design/technical-proposal-2026-09-07.md)。
- [uv 与 GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)。
- [GitHub 分支保护适用套餐](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)。
- [Codex GitHub review 配置](https://learn.chatgpt.com/docs/third-party/github)。
