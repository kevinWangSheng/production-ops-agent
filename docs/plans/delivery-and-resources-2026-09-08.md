# 开发交付与资源安排

日期：2026-09-08。基础 CI/PR 已完成；本文件维护当前资源安排。用户已授权更新开工上下文，并在本项目安装 LangChain 文档 MCP、规划 coding agent 扩展能力；本轮用户授权本地可逆 M0 A/B/C、汇合与秘密扫描及任务分支PR；不授权业务模型调用、trace 上传、采购或部署。产品范围和实施门槛不变。

## 本轮：基础 CI 与 PR

- 使用 GitHub Actions，PR 到 main、push main 和手动触发；Ubuntu 24.04，Python 3.12.13，uv 0.10.8，依赖使用 uv.lock。
- 基础已合并CI运行make setup/check；本批集成PR增加固定Gitleaks正负自检/扫描和固定PostgreSQL17.9镜像合成集成测试。最新CI见批次索引，不含dataset eval、模型/trace调用、镜像发布或部署。
- Actions 固定完整提交 SHA；contents: read、checkout 不保留 Git 凭据、15 分钟超时、同 PR 取消旧运行、暂不共享缓存。不使用 pull_request_target、不注入业务凭据。
- PR 是交付入口：范围/依据、实际验证、风险和后续；最新提交 checks 成功并完成适用的独立审查后，依据授权合并。
- 默认 main、公开仓库（用户于 2026-09-08 授权变更）、合并后删除远程任务分支、Actions 默认只读且不可批准 PR 已配置。
- 初始私有仓库的保护 API 曾返回套餐限制 403；用户授权公开后，main 保护 PUT 成功。当前要求 GitHub Actions（app_id 15368）的 checks 成功并与 main 同步、解决讨论、管理员也受约束，禁止 force-push/删除。PR review 流程启用但审批人数为 0，不强制单人仓库自我审批；独立审查仍按 AGENTS 执行。

## 审查机器人

优先使用已有 Codex GitHub code review 集成，不在 CI 中新增模型 API key 或另购审查 SaaS。AGENTS.md 的 Code Review Rules 提供项目边界，机器人意见不能代替检查或用户决定。

启用需 Codex 设置中连接本仓库并开启 code review；自动审查还需相应开关。必须在真实 PR 上观察机器人反应/审查才算接入验证成功。GitHub CLI 当前 OAuth 无权列出 GitHub App installations，不能把查询失败解释成未安装。初始 GitHub 未连接问题已由用户完成配置。PR #1 的手动 Code Review / Security Review 已完成，唯一 P2 文档发现已修正并解决讨论，安全审查无发现。PR #1/#2 已合并，main CI 成功、本地同步和任务 worktree 清理完成；证据见[任务记录](../tasks/2026-09-08-github-ci.md)。每次推送自动复审仍未证明，应在后续真实 PR 核实；不能把手动成功称为自动启用。

## 资源与凭据：按阶段提供

- 基础 CI：GitHub 托管 runner；只使用 GitHub 自动提供的只读 GITHUB_TOKEN，不需要用户提供模型 key。私有仓库 Actions 用量受账号额度限制，无法运行时记录具体账单/配额错误，不自动升级付费额度。
- 模型协议实验：需要 DeepSeek 账号的有效 key、实际可用 profile 与已授权调用预算。用户已在主工作区 `.env` 填写 DEEPSEEK_API_KEY 与 LANGSMITH_API_KEY（仅验证存在，未验证有效性），权限已为 0600；后续显式读取该文件，不复制秘密到 worktree。[配置模板](../../.env.example)的秘密值为空，非秘密模型配置沿用 C3。M0-01 已实现显式文件/进程环境离线校验；无自动 dotenv 发现，live 无条件拒绝。
- Trace/eval 接入：需要 LangSmith 项目/凭据与授权数据出口。LANGSMITH_API_KEY 同样仅在本地私有配置或任务特定的 GitHub Secrets；LANGSMITH_PROJECT 等非秘密配置可版本管理。只导出 C3 白名单，不上传完整 Agent state。
- 实验环境：先测可用内存、CPU、磁盘和容器 VM 配额；Docker daemon 当前不可达，kubectl/Helm 尚未就绪。实测后选择本地或隔离 Linux 资源，不把候选容量当已可用资源，不采购常驻双 VM/HA。
- PostgreSQL：B已使用现有原生17.9建立专属55431/库/数据目录，合成累计预算及进程/数据库重启由独立agent实测。任务结束停止，数据保留；原生内存配置和静态RSS不是硬资源限制或产品权限隔离。只使用无秘密本地合成trust，未连接目标数据库。具体资源/启停见[B证据](../evidence/m0-b/results.md)。
- Dataset/eval：单独 workflow 或本地实验入口，初期仅人工触发；固定数据、模型/prompt/tool/adapter/evaluator 版本、预算和超时，隔离开发与保留数据，保留失败与独立评分。需完成具体合同后才接入，不在每个 PR 自动运行。
- 镜像与部署：有可运行产品后选择镜像仓库（优先评估 GHCR）、固定 digest、测试环境、部署身份、升级/回滚/在途任务兼容。若目标支持，优先短期身份/OIDC；否则任务限定 Secrets。GitHub 环境审批和保护也需核对私有仓库套餐能力，不能假设可用。
- UI：已有 Jinja/SSE 技术选择，先用本地运行页面、浏览器自动化与截图验收。Figma 仅在需要设计稿交接、多人协作或复用资产时接入，不是前置依赖；浏览器工具实际可用性随首个 UI 任务验证。
- 无需本轮增加：第三方审查订阅、向量 Wiki、除本批文档检索外的额外 MCP、通用执行编排或自托管 runner。

用户不在聊天、文档、源码或 PR 中提供 key；需要时由用户在相应私有配置界面录入，Agent 只验证存在与最小连通性，不打印值。基础 PR CI 永不获得业务或部署 Secrets。当前记录的是配置约定，不代表这些服务已经连接。

## 后续完成条件

- [x] 用户授权仓库公开后配置 main 服务端保护；API 回读确认必需 checks、strict、管理员约束和禁止强推/删除。尚未通过故意违规 push 做破坏性探测。
- [x] 审查机器人已在真实 PR #1 响应手动请求。
- [x] 手动机器人审查结果已处理；PR #1/#2 合并、main CI、本地同步与任务 worktree 清理已完成。
- [ ] 在后续真实 PR 验证自动复审方式，未验证时继续遵守独立审查约定。
- [-] 已复测本机内存/CPU/磁盘并准备 M0-01 离线环境；容器 daemon 仍不可达，本地专属PostgreSQL预算已验证；真实凭据有效性/区域/账号/费用期限授权及完整实验环境仍待补齐，见 M0-01 任务。
- [ ] 完成真实模型/上下文/恢复/权限实验与 eval 校准，再接入人工触发的评测。
- [ ] 有运行产品后接入镜像构建、测试环境部署与 smoke 验证。
- [ ] CD 上线、独立上线检查、升级/恢复与 72 小时 soak；仍不构成真实生产证明。

## 首次配置：谁提供什么

首个任务见 [M0-01 资源与入口准备](../tasks/2026-09-08-m0-01-preflight.md)。`.env.example` 是配置合同模板，M0-01 已有显式离线加载器；填值不会触发调用，也不代表权限、预算或连通性已通过。入口必须显式指定私有文件，按数据解析，不用 shell source/执行配置内容；进程环境与文件冲突时拒绝启动，避免误用其他项目账号。

- 用户已提供两个 API key（仅存在性确认）；仍需明确 LangSmith 所属区域 endpoint 和项目归属（key 授权多个 workspace 时提供 workspace ID）；首轮模型/trace 实验的实际费用上限和期限。私有值仅录入主工作区 `.env`、任务明确指定的私有文件或服务私有配置界面，不发到聊天。基础 CI 不接收这些值。
- 模型当前决定（用户于2026-09-09更新）：DeepSeek 官方 `deepseek-v4-flash`、thinking enabled、high，直接请求Flash；由助手验证实际可用性、锁版本、记录调用日期及返回版本。换模型或降低质量不得静默处理。尚未选择独立 judge profile，M0 校准时提出方案；本轮不需购买另一模型。
- 助手负责：实验脚本、依赖锁、PostgreSQL 本地实验身份、OTel Demo 版本及遥测映射、端口/卷/命名空间、只读权限与注入身份隔离、查询配置、证据留存和清理。用户无需逐项指定数据库口令或 Kubernetes 标签。现有私有资源仅在用户决定复用时提供精确访问范围，不能默认连接生产。
- 资源选择：2026-09-08 只读盘点为 16 GiB 物理内存、10 逻辑 CPU、约 44 GiB 可用磁盘；不是可分配容量。Docker/Compose 客户端可用但 daemon 不可达，kubectl/Helm 缺失。助手先准备本地机制实验的容量方案；完整实验需要云资源时，再给出供应商、容量、期限、总价上限和退出方案，用户只做采购/复用决定。无需现在交付云主账号或服务器。
- 后续才需要：部署地址/TLS、镜像与部署身份、72 小时运行窗口和预算。UI 首次运行时验证浏览器能力；基础 GitHub 接入已完成。

预算模板默认 0，含义是尚未获准付费调用；填非零值本身不是授权证据。实验任务记录保存授权依据、额度、期限及既有消耗；后续真实入口必须在调用前校验并执行累计预算，不能只设置单次 max_tokens。每个 worktree 的私有文件使用任务专用路径，不能假设会随 Git 自动复制。

## Coding agent 能力规划（2026-09-08）

目标是补齐模型无法直接取得的事实、执行能力与可靠验证，而非统一采用 MCP。CLI/API 负责实际读写和执行，skill 负责可复用的方法与判断，项目测试/工件负责可重现结果；文档或模型自述不能替代后两者。以下均为开发者能力，不向 OpsPilot 产品授予 shell、数据库写或外部通知权限。本次只安装 LangChain 文档 MCP，其余按列明阶段实施。

### 现在：资料、源码与上下文

- **官方资料检索：已配置。** 项目 LangChain docs/reference 两个 MCP 分别负责指南与 API 签名；只发送公开库名、符号和抽象问题，不发送源码私有片段、业务证据、key 或原始 trace。无需业务 key。Web/官方 Markdown/固定源码版本仍是回退路径；文档版本与锁定依赖不一致时以对应安装版本签名/源码和最小调用验证，不能自动升级依赖迎合最新示例。安装及实测见[任务](../tasks/2026-09-08-agent-capabilities.md)。
- **源码定位：复用 rg、Git、shell 和现有 IDE。** 用固定上游 commit、调用点、实际实现弥补模型记忆不准；可定位目标接口并解释实调用链才算可用。Context7、语义/LSP/Symbol 检索暂按需：反复跨库查询、遗漏引用或定位耗时后，再用同一真实任务比较结果正确性、耗时和上下文消耗。编辑器有 LSP 不等于 coding agent 已能调用；没有净收益不新增外部索引、也不上传全仓。
- **跨会话与复用方法：复用任务记录、ROADMAP、PR 模板及现有共享 skills。** 调试、源码研究、代码审查、前端设计等已有技能入口，按任务匹配，不在每次启动全量加载。复杂状态/时间线可复用可视化 skill；其产物只解释合同，不证明行为。重复任务中形成稳定且经验证的操作后，才考虑 CLI+skill 封装；跨项目技能按中央 skills-maintenance 流程维护。本批不新增 skill、记忆服务或通用 orchestrator。

### M0：执行与运行证据（最高优先级）

- **受控实验 CLI：离线部分已实现，真实部分待前提明确。** M0-01 已有显式配置读取、版本锁定、固定次数/超时的离线 SDK 排演、退出码和脱敏工件；live 始终拒绝。A已扩展可注入合成协议，B已验证持久累计预留/结算/unknown与期限，C已提供公开开发合同；真实费用授权/账号校验和联网清理仍待实现与独立验证。解决模型会写代码但无法仅凭阅读证明协议可用的问题；以真实多轮工具调用、受控错误/中断与可重复输出验证。先做离线入口，不以缺 key 阻塞所有本地工作；实际调用前确定额度与期限。
- **运行观察：替身上传/回读已验证，真实接通待授权，首条真实链路必需。** LangSmith SDK/API 白名单导出及结果回读，PostgreSQL 保存业务真相；按需读取本地结构化日志、trace、SQL 记录和版本。验证正确关联、出口过滤、平台故障及重试，不能把 trace 成功算调查成功。初期不加 LangSmith MCP 或第二套观测 SaaS；若人工反复找 run、跨页面排障造成明显摩擦，再评估只读专用查询入口。费用包含模型调用与平台留存，按实验记录约束。
- **真实环境与故障：待准备。** 复用 Docker/Compose、PostgreSQL 客户端；真实 OTel 工作负载和权限验证需要时再准备 kubectl/Helm。工程身份与产品只读身份隔离，任务专用端口/卷/命名空间。实际正常遥测、拒绝证据、故障注入/恢复/重置才是能力证明；daemon/CLI 版本存在只是前提。控制 CPU/内存/磁盘、启停和证据留存，不先购买常驻集群或云管理 MCP。
- **评测与独立验证：C公开开发schema/fixtures及确定性断言已独立审查，完整eval按M0–M3推进。** 项目 Scenario/Outcome harness、确定性 oracle、人工校准的 judge、冻结基线/候选/预算及失败分母；独立 agent 按 AGENTS 审查合同和原始证据。共享目录的新 agent 不算保留集隔离，正式盲测前需实际访问拒绝。优先完成既定 LangSmith + 项目脚本组合，不增加另一评测平台；以冻结结果、非退化判据及可重现失败衡量收益。

### 首次实现 API/UI 与凭据路径：补可重复检查

- **API：待加入 HTTPX/FastAPI TestClient 等锁定测试依赖。** 在首次 API 实现时测试认证、输入、幂等、expected_version 冲突、取消、CSRF/Origin、SSE 游标及重连。pytest 用实际响应与持久状态断言；本地 HTTP CLI 用于排障。不需要 Postman/Apifox 前置订阅，除非出现跨团队接口交付需求。
- **浏览器：已有宿主工具入口，项目 UI 尚未实测。** 首个 Jinja/SSE 页面用现有浏览器能力核查真实操作、控制台、网络和截图；关键用户流程再用固定版本 pytest-playwright 保存并进入 CI。API 通过不证明 UI 可用，截图也不证明控制状态正确。Figma、浏览器云服务只在设计交接或跨浏览器容量出现具体需要时引入。
- **类型与安全静态检查：已有 Ruff/pytest；本批集成新增固定Gitleaks并验证真实合成泄漏拒绝。** 第一批状态/DTO/异步代码选定一种类型检查器；Gitleaks8.30.1只扫描Git历史/已跟踪文件，输出固定状态；精确源码hash误报按规则/路径/值AND例外并独立复验，CI状态见集成任务。扫描通过不能代替运行时出口测试。应用依赖加入后评估 pip-audit，镜像出现后再评估 Trivy；分别固定版本、明确扫描范围/离线数据库新鲜度/误报处理，避免多套重复 SAST。普通测试不读取真实 `.env` 或启动付费调用。

### M3：交付与持续运行

复用 Git/gh/Actions、镜像构建与 Compose/Helm；实现部署 smoke、升级/在途恢复、独立健康检测和 72 小时 soak。能力证明是固定镜像/配置在干净环境复现、失败可见、状态保留与实际资源/费用记录。镜像仓库、部署身份和运行窗口按阶段准备；不默认给 coding agent 云主账号，不增加自托管 runner、远程常驻 shell 或自动通知工具。

### 接入与保留条件

每项新增能力须有具体工作项、现有方法的缺口、最小权限/数据出口、版本/调用预算、一次实际成功与必要失败证据。状态区分“提供入口、已配置、已实际验证”；新增 CLI/MCP/skill 不自动成为产品依赖。实际耗时、错误率、上下文和维护成本没有净收益时停用或回退；外部服务失效不能妨碍已有本地检查。图像生成、OCR、音视频等在当前文本遥测与工作台范围尚无明确缺口，出现附件/视觉输入任务时再评估。通用 shell/数据库 MCP、全仓外部索引、第二套知识/评测/编排平台、自动宣布验收完成的 hook 暂不引入。

## 状态维护与 hook 选择

本次已存在维护要求，漏项表现为完成结果追加到了任务尾部和 ignored 进度，引用计划的当前待办没有同步；无法仅由工件证明是 context rot。先使用 AGENTS 的收尾核对及 PR 模板提示，不新增宿主 hook。语义 hook 难以判断实验/验收是否真的完成，Git hook 也无法在提交前证明合并后的 CI/清理结果。

若后续真实任务再次出现同类遗漏，先固定可机械判定的失败案例，再将小检查接入现有 make check/CI；需要宿主提醒时再验证对应宿主的触发时机和支持情况。检查只报告不一致，不自动勾选 passes、不改历史、不自动提交。此决定不声称规则已能消除长上下文遗漏。

## 依据

- [LangChain 文档/API MCP](https://docs.langchain.com/use-these-docs)、[Context7](https://github.com/upstash/context7)、[FastAPI 测试](https://fastapi.tiangolo.com/tutorial/testing/)、[Playwright Python](https://playwright.dev/python/docs/intro)、[Gitleaks](https://github.com/gitleaks/gitleaks)：2026-09-08 已核查官方资料；候选工具的具体版本与适用性在实施工作项验证，不表示已安装。

- [LangSmith key/endpoint/workspace 配置](https://docs.langchain.com/langsmith/create-account-api-key)与[区域识别](https://docs.langchain.com/langsmith/regions-faq)，2026-09-08 核查；本项目按 C3 禁用全量自动 tracing，使用白名单出口。

- [M0 执行计划](m0-validation-plan-2026-09-07.md)与[C3 技术方案](../design/technical-proposal-2026-09-07.md)。
- [uv 与 GitHub Actions](https://docs.astral.sh/uv/guides/integration/github/)。
- [GitHub 分支保护适用套餐](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches)。
- [Codex GitHub review 配置](https://learn.chatgpt.com/docs/third-party/github)。

## 本批交付状态

2026-09-08：[批次索引](../tasks/2026-09-08-m0-batch.md)汇总PR #4–#8。A/B/C及汇合均已完成本地检查、独立审查与发现修复；PR #8首次固定PostgreSQL服务和Gitleaks CI成功。全部PR未合并，真实模型/trace未运行，账号范围/区域/实际费用期限仍未授权；产品门槛不变。本地专属PG已停止，独有数据/日志与worktree保留。
