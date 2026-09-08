# 开发工具与检查

当前工具覆盖 Python 开发配置和环境诊断，不代表完整 M0 实验环境或产品验收。
项目使用 Python 3.12；系统 Python 可继续保留原版本。当前支持 macOS/Linux 的 `.venv/bin` 布局。

## 首次准备

在项目根目录执行 `make setup`。它运行 `uv sync --locked --python 3.12`，可能下载 Python 和依赖到 uv 缓存，并创建或同步项目 `.venv`；不修改系统 Python 或 shell 配置。
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
临时日志的唯一副本不留在即将删除的 worktree 中。本批没有通用日志 runner、自动结果目录、CI、产品服务或额外 MCP。
