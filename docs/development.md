# 开发工具与检查

当前工具覆盖 Python 开发配置和环境诊断，不代表完整 M0 实验环境或产品验收。
项目使用 Python 3.12；系统 Python 可继续保留原版本。当前支持 macOS/Linux 的 `.venv/bin` 布局。

## 首次准备

在项目根目录执行 `make setup`。它运行 `uv sync --locked`，解释器优先使用显式的 `UV_PYTHON`（CI 固定版本），未设置时使用 Python 3.12，可能下载 Python 和依赖到 uv 缓存，并创建或同步项目 `.venv`；不修改系统 Python 或 shell 配置。
已有 `.venv` 会按锁精确同步，不要将其他项目或有独有包的环境放在这里。setup 显式将 `UV_PROJECT_ENVIRONMENT` 固定为当前项目 `.venv`，避免继承其他工作区的目标路径。

`uv.lock` 是实际解析生成的版本清单。锁文件缺失或过期时 setup 失败；有意修改依赖后，显式运行 `uv lock --python 3.12`，审查锁文件差异，再运行 setup。普通检查不会更新锁文件。

## 日常命令

- `make doctor`：只读检查根目录、项目声明、锁文件存在、uv、项目 Python 和工具版本。无需 `.venv` 即可启动诊断。
- `make check`：诊断、离线锁检查、Ruff lint、Ruff 格式检查、pytest；任何一步失败即返回非零。不自动修复代码或安装依赖。
- `make test`：诊断、离线锁检查、pytest。
- `.venv/bin/python -m pytest tests/test_doctor.py`：定向测试，使用 pytest 原生参数。
- `python3 scripts/doctor.py --lab`：额外检查 Docker/Compose、daemon、kubectl 和 Helm；不会启动、安装或连接 Kubernetes 集群。

基础诊断失败返回 1；基础可用而请求的实验设施缺失返回 2；所请求的诊断均满足前提返回 0。
锁文件存在、锁与声明一致、虚拟环境已同步是不同结论。doctor 不证明完整依赖匹配锁文件；setup 成功负责该次同步，后续人工改变环境须重新同步。
pytest 没有收集到测试返回 5，不算通过；make 可能将子命令错误映射为自己的非零退出码，原始错误仍在输出中。

## 失败与恢复

先根据诊断定位缺少的解释器、工具或 daemon。离线锁检查因本地材料缺失失败时，记录环境前提缺失，不冒充代码失败或通过。
setup 涉及安装；check/test 可能写工具缓存和测试临时文件，但不安装依赖。doctor 的子命令均为只读查询并设 5 秒超时，不输出环境变量或原始错误中可能出现的凭据。
Docker 不可用不影响默认基础检查；等对应实验获得准备授权后再处理设施。不要擅自删除环境或设置来排除错误。

## 验证证据

在对应任务记录中保存实际命令、工具版本、结果和重要失败。未提交状态下的结果标为 dirty 工作区验证，并保留本次受检脚本、配置、测试及锁文件的哈希或快照，不能只用 HEAD 标识版本。
临时日志的唯一副本不留在即将删除的 worktree 中。当前没有通用日志 runner、自动结果目录或产品服务；基础 CI 见下节。

## GitHub CI

PR 到 main、push main 或手动触发 `.github/workflows/ci.yml`；Ubuntu 24.04、uv 0.10.8 与 Python 3.12.13，执行相同 make setup/check。CI 无业务 Secrets、dataset eval 或部署。setup 尊重 UV_PYTHON，避免安装与后续锁检查选择不同解释器。

PR 通过最新 checks 及适用独立审查后才按授权合并；平台保护与审查机器人接入状态见[交付与资源规划](plans/delivery-and-resources-2026-09-08.md)。

## 项目 LangChain 文档 MCP

从有本批变更的任务分支执行 `.venv/bin/python scripts/setup_docs_mcp.py`；默认配置当前目录，可用 `--project /绝对/项目目录` 安装到本仓库另一 worktree。脚本只修改三个项目文件：`.codex/config.toml`、`.mcp.json`、`.claude/settings.local.json`，无用户级服务器注册、模型调用、业务 key 或全量工具自动授权。未知同名配置冲突时拒绝覆盖，保留其他服务器和设置；可重复执行。

版本管理保存安装脚本，生成配置被忽略。原因是本仓库保留 `.Codex/` 旧资料，而 Codex 运行入口为小写 `.codex/`：macOS 大小写不敏感时两者落入同一目录，Linux 则不同；安装时按宿主正确路径生成，避免 Git 同时保存仅目录大小写不同的树。Claude 同步生成本项目 `.mcp.json` 和两个具名服务器的本地启用设置；不修改共享 AGENTS 规则或复制 skills。

Codex 仅启用文档搜索/虚拟文档读取、API 搜索/符号读取四项工具；不设 required，断连时回退官方网页及源码。Claude 明确 deny 已知 `submit_feedback`，其余工具仍受宿主权限机制；不是对未来未知工具的完整白名单保证。文档服务不需要 DeepSeek/LangSmith key，不读取 `.env`；虚拟文档文件系统在远端官方资料内，不是本机文件访问。

用户授权本项目接入已落实到本地配置；新克隆/新 worktree 仍遵守宿主项目信任机制，脚本不自动信任所有目录。当前已打开的会话未必热加载，需重开/刷新 MCP 后验证。检查命令：`codex mcp get langchain-docs --json`、`codex mcp get langchain-reference --json`、`claude mcp get langchain-docs`、`claude mcp get langchain-reference`。配置读取、Connected 和实际查询是不同证据，见[任务记录](tasks/2026-09-08-agent-capabilities.md)。

停用时在项目 Codex 两个 server 表设置 `enabled = false`，并按 Claude 的项目 MCP 管理停用对应名字；只改这两项，保留其他服务器。重新启用先核对修改后的配置，不用安装脚本覆盖用户后续定制。Git 忽略不等于可删除，清理工作区前按原 worktree 约定保留需要的本地配置与私有资料。

## M0-01 离线协议入口

`make setup` 现在同时同步 `dev` 和 `m0` 依赖组；`m0` 固定 OpenAI 3.10.0、LangSmith 0.12.2、HTTPX2 2.12.0，传递依赖及发行物哈希见 uv.lock。产品 dependencies 仍为空。pytest 明确禁用 LangSmith 自动插件，CI 仍只做离线开发检查。

在任务 worktree 根目录执行：

```sh
make setup
make check
.venv/bin/python -m scripts.m0 offline
.venv/bin/python -m scripts.m0 check-config --env-file /Users/shenghuikevin/dev/AI/production-ops-agent/.env
.venv/bin/python -m scripts.m0 live
```

`offline` 仅使用版本管理的合成 fixture，不接受私有配置参数；模型使用 HTTPX2 内存 transport，trace 使用 requests 捕获 session，并拦截 socket 网络调用。入口只输出白名单摘要、版本与输入文件哈希；两次替身请求、单次 SDK 超时 5 秒、模型阶段总超时 15 秒、SDK 重试 0。此次数/时间限制属于固定替身排演，不能复用于付费预算或产品运行时。

`check-config` 是纯本地校验，显式绝对路径或 `--process-env` 二选一；无默认 dotenv 发现。文件必须为当前用户持有、普通文件且无 group/other 权限，拒绝最终符号链接、重复/未知键与 shell 插值，不执行配置。只输出固定错误码或布尔状态；文件与同名环境变量不一致即拒绝，不回显键值。日期要求带时区且在未来。有效性/区域归属/多 workspace 权限仍需后续实际验证，字段存在不算通过。

退出码：0 仅表示离线排演成功；1 排演异常（不输出原始 SDK 异常）；2 配置拒绝或真实前提未完成；3 `LIVE_NOT_ENABLED`。本版本 `live` **始终拒绝**，即使 key、预算、日期全填也不创建客户端、不读文件。后续真实入口必须先实现授权记录、实际 endpoint/workspace 校验、持久累计费用预留（含未知费用）、并发/重启和总期限限制、受控 trace 上传/回读与退出清理，并完成独立审查；预算数字本身不能打开此入口。

本次没有服务、数据库或后台导出线程需要停止，CLI 客户端随上下文关闭。stdout 可保存到任务专用 `tmp/m0-01/`；审核后无秘密证据写入 `docs/evidence/m0-01/`。清理遵循 AGENTS，不自动删除证据或其他任务卷。真实实验的用例合同、版本来源、资源限制和缺项见 [M0-01](tasks/2026-09-08-m0-01-preflight.md)。


## 本批 M0 公开合同、预算与安全检查

本批仍为离线合成机制实现，live 无条件拒绝。任务及PR依赖见[批次索引](tasks/2026-09-08-m0-batch.md)。`make check`包括A/C及预算输入检查；实际数据库测试必须显式 opt-in，默认skip单列，不能算实际集成通过。

- 数据库仅由B worktree的 `.venv/bin/python -m scripts.m0.postgres_lab start/stop` 控制；先核对[资源与归属](evidence/m0-b/results.md)。本机现有PostgreSQL17.9，专属55431及tmp/m0-b/postgres，数据始终保留；不要在另一个worktree同时创建同端口实例。原生配置不是容器硬资源限额，不证明产品数据库身份隔离。
- 数据库已由B实例启动时，在集成worktree执行 `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`；数据库重启测试只从B工作区额外设置 `M0_B_RESTART=1`。任务完成后由B实例脚本停止，不删除数据。
- 秘密扫描：`python3 scripts/install_gitleaks.py --directory tmp/gitleaks` 下载固定8.30.1官方发行包并校验固定SHA256；已存在目标拒绝覆盖，可直接复用已核查binary。支持macOS arm64/Linux x86_64。
- `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks`先运行实际合成泄漏/干净样本自检，再扫描全部本地Git refs历史及已跟踪文件快照。使用默认规则并禁用仓库抑制/inline allow；输出只含固定结果，发现/扫描错误非零退出。Git未跟踪/ignored私有文件不读取；误跟踪.env直接拒绝，不读内容。扫描当前新增文件前需按任务范围`git add`，不能把漏扫未跟踪源码误当安全证明。

CI 的checks增加同一扫描与自检；m0-postgres使用官方17.9固定digest、1CPU/512MiB的临时合成服务，执行真实数据库集成（不执行原生实例restart）。无业务Secrets、模型/trace/部署。该服务账本没有真实额度权威；新建CI库不授权付费。维护首次源代码状态/结果见[汇合任务](tasks/2026-09-08-m0-integration.md)。
