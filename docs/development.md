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
