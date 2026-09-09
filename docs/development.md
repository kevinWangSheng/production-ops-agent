# 开发工具与检查

当前工具覆盖Python开发检查、环境诊断与有界M0实验入口；原Pro单次实验及后续trace只读核验已有记录，默认现为Flash。Flash 单次已真实运行，但最终 JSON 合同失败、未上传 trace，见 [结果](evidence/m0-01-live/flash-execution.md)。完整链路、M0 与产品验收仍未完成。
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

PR须通过最新checks、等待已触发的review返回并处置发现，再按用户授权合并；平台保护与审查机器人接入状态见[交付与资源规划](plans/delivery-and-resources-2026-09-08.md)。

## 项目 LangChain 文档 MCP

从有本批变更的任务分支执行 `.venv/bin/python scripts/setup_docs_mcp.py`；默认配置当前目录，可用 `--project /绝对/项目目录` 安装到本仓库另一 worktree。脚本只修改三个项目文件：`.codex/config.toml`、`.mcp.json`、`.claude/settings.local.json`，无用户级服务器注册、模型调用、业务 key 或全量工具自动授权。未知同名配置冲突时拒绝覆盖，保留其他服务器和设置；可重复执行。

版本管理保存安装脚本，生成配置被忽略。原因是本仓库保留 `.Codex/` 旧资料，而 Codex 运行入口为小写 `.codex/`：macOS 大小写不敏感时两者落入同一目录，Linux 则不同；安装时按宿主正确路径生成，避免 Git 同时保存仅目录大小写不同的树。Claude 同步生成本项目 `.mcp.json` 和两个具名服务器的本地启用设置；不修改共享 AGENTS 规则或复制 skills。

Codex 仅启用文档搜索/虚拟文档读取、API 搜索/符号读取四项工具；不设 required，断连时回退官方网页及源码。Claude 明确 deny 已知 `submit_feedback`，其余工具仍受宿主权限机制；不是对未来未知工具的完整白名单保证。文档服务不需要 DeepSeek/LangSmith key，不读取 `.env`；虚拟文档文件系统在远端官方资料内，不是本机文件访问。

用户授权本项目接入已落实到本地配置；新克隆/新 worktree 仍遵守宿主项目信任机制，脚本不自动信任所有目录。当前已打开的会话未必热加载，需重开/刷新 MCP 后验证。检查命令：`codex mcp get langchain-docs --json`、`codex mcp get langchain-reference --json`、`claude mcp get langchain-docs`、`claude mcp get langchain-reference`。配置读取、Connected 和实际查询是不同证据，见[任务记录](tasks/2026-09-08-agent-capabilities.md)。

停用时在项目 Codex 两个 server 表设置 `enabled = false`，并按 Claude 的项目 MCP 管理停用对应名字；只改这两项，保留其他服务器。重新启用先核对修改后的配置，不用安装脚本覆盖用户后续定制。Git 忽略不等于可删除，清理工作区前按原 worktree 约定保留需要的本地配置与私有资料。

## M0-01 离线协议入口

`make setup` 现在同时同步 `dev` 和 `m0` 依赖组；`m0` 固定 OpenAI 3.10.0、LangSmith 0.12.2、HTTPX2 2.12.0，传递依赖及发行物哈希见 uv.lock。产品 dependencies 仍为空。pytest明确禁用LangSmith自动插件；CI做开发检查与合成PostgreSQL集成，不执行付费模型/trace。

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

本节不带批准文件的命令：offline成功退出0、异常退出1；check-config退出2表示仅核查配置而未批准执行；live缺少批准文件时退出3（LIVE_NOT_ENABLED）。当前live并非无条件禁用：完整配置和独立有效批准合同满足条件时可执行，见下方“受批准合同约束的真实入口”。预算数字本身不能打开入口。

仅offline/check-config不启动服务或数据库，CLI客户端随上下文关闭；本地真实实验的专属PostgreSQL由其所属worktree显式启停。stdout 可保存到任务专用 `tmp/m0-01/`；审核后无秘密证据写入 `docs/evidence/m0-01/`。清理遵循 AGENTS，不自动删除证据或其他任务卷。真实实验的用例合同、版本来源、资源限制和缺项见 [M0-01](tasks/2026-09-08-m0-01-preflight.md)。


## M0 公开合同、预算与安全检查

A/B/C批次是合成机制证据；后续M0-01已有受批准合同约束的live入口及原单次实验证据，不能复用已消耗的批准或据此开放产品门槛。任务及PR依赖见[批次索引](tasks/2026-09-08-m0-batch.md)。`make check`包括A/C及预算输入检查；实际数据库测试必须显式 opt-in，默认skip单列，不能算实际集成通过。

- 数据库由创建该专属实例的任务worktree中的 `.venv/bin/python -m scripts.m0.postgres_lab start/stop` 控制；先核对[资源与归属](evidence/m0-b/results.md)。本机实验使用PostgreSQL17.9，专属55431及各自worktree的tmp/m0-b/postgres，数据始终保留；不要在另一个worktree同时创建同端口实例。原生配置不是容器硬资源限额，不证明产品数据库身份隔离。
- 数据库由所属worktree启动并核对归属后，执行 `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`；数据库重启测试只从实例所属工作区额外设置 `M0_B_RESTART=1`。任务完成后由该实例所属脚本停止，不删除数据。
- 秘密扫描：`python3 scripts/install_gitleaks.py --directory tmp/gitleaks` 下载固定8.30.1官方发行包并校验固定SHA256；已存在目标拒绝覆盖，可直接复用已核查binary。支持macOS arm64/Linux x86_64。
- `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks`先运行实际合成泄漏/干净样本自检，再分别扫描 Git 索引暂存 blob、已跟踪路径的当前工作区快照，以及全部本地 Git refs 历史。索引内容按列举时固定的 blob ID 读取，工作区后续清理或删除不会掩盖已暂存内容；未暂存更改仍单独检查。未合并的索引、symlink/submodule 与误暂存私有配置均拒绝；不宣称检查与后续 commit 对并发 git add 原子绑定。使用默认规则并禁用仓库抑制/inline allow；仅对指定证据manifest的两个已核实源码SHA256设规则+路径+值AND例外，并实测同路径canary/同值不同路径仍拒绝；输出只含固定结果，发现/扫描错误非零退出。Git未跟踪/ignored私有文件不读取；误跟踪.env直接拒绝，不读内容。扫描当前新增文件前需按任务范围`git add`，不能把漏扫未跟踪源码误当安全证明。

CI 的checks增加同一扫描与自检；m0-postgres使用官方17.9固定digest、1CPU/512MiB的临时合成服务，执行真实数据库集成（不执行原生实例restart）。无业务Secrets、模型/trace/部署。该服务账本没有真实额度权威；新建CI库不授权付费。维护首次源代码状态/结果见[汇合任务](tasks/2026-09-08-m0-integration.md)。

### 受批准合同约束的真实入口（M0-01 normal-1）

2026-09-09原Pro单次实验已获批并执行，原退出1/trace unknown及后续只读确认均保留在[执行记录](evidence/m0-01-live/execution.md)。默认已改Flash；此变更不是新的付费执行授权，原已使用v1批准文件不迁移。以下描述当前v2入口使用条件。

[具体方案](evidence/m0-01-live/plan.md)和[当前任务](tasks/2026-09-08-m0-01-preflight.md)是范围及授权依据。`live` 默认仍退出3；只有显式 `--env-file /absolute/private.env --approval-file /absolute/private-approval.json` 才进入批准合同校验。缺字段、未批准、错误hash/身份、非正预算、过期均在外部请求前拒绝。批准文件是工程操作者根据真实人工授权填写的0600本地记录，不是授予模型的权限，不承诺抵御可修改本机代码/数据库的恶意操作者。

助手在获批后完成私有记录，不要求用户手写JSON。记录字段以 `live.validate` 为准；审批引用与experiment/run UUID不可重复，绑定代码/lock/fixture digest、两key的SHA256、区域/现有workspace/project UUID、2.00元及绝对deadline。`approved` 和 `billing_checked` 仅在对应依据齐全后填写true；不能将草案预算当授权。修改源码后重新核对digest与审查范围。

本地工程准备（不调用模型或平台）：

```sh
.venv/bin/python -m scripts.m0.postgres_lab start
.venv/bin/python -c 'from scripts.m0.live import LiveLedger; from scripts.m0.postgres_lab import DSN, verify_server; verify_server(); LiveLedger(DSN).install_live()'
M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration/test_m0_live_postgres.py -q
.venv/bin/python -m scripts.m0.postgres_lab stop
```

该脚本检查端口及本worktree数据目录归属；数据留在 `tmp/m0-b/postgres`（沿用既有lab布局），不复用另一worktree的数据库、不删除卷。live执行本身只验证本地服务归属并使用已安装表，不自动安装schema。独立live表记录本实验真实调用授权/未知账单，旧synthetic表不变；测试只用随机合成审批身份。

批准后运行形式为 `.venv/bin/python -m scripts.m0 live --env-file /absolute/private.env --approval-file /absolute/private-approval.json`。成功退出0；默认或前提拒绝退出3；协议/trace不完整退出1。输出只含受控状态、模型请求尝试数与未核账占用，实际费用unknown不自动退还额度。PostgreSQL的m0_live_once记录固定身份、每类HTTP尝试、业务结果与白名单outbox；trace失败不能抹掉业务结果。重启/并发再次启动一律拒绝，不自动续传或重发；后续trace恢复须另行限定，不能靠新UUID绕过同一授权。

本轮正常模型请求为固定官方Chat Completions JSON，经锁定HTTPX2直接受限发送；LangSmith通过锁定SDK序列化到内存并验证白名单后发送。没有OpenAI SDK自动重试或自动trace wrapper。单次请求/响应限制16KiB/128KiB，HTTP层无环境代理、重定向或重试；完整body读取受timeout约束，DB提交后再次检查截止与取消。420秒是有效HTTP运行期限，数据库失败保存/客户端关闭另受既有有界连接/语句超时约束，不保证进程精确420秒退出。原Pro协议及LangSmith既有trace回读已有指定范围证据，不能据此证明新Flash配置、恢复流程或全部后端行为；Flash 本轮结果及最终内容诊断缺口见上述执行记录；完整成功链路仍需新的有界执行，不复用已消耗批准。

回读诊断：CLI的trace_code为固定分类；trace_readback_code严格核对业务DTO，仅允许平台返回的根层级metadata.ls_run_depth=int0，出站extra规则仍不变。2xx null明确失败而非按404重试。初次失败可继续只读核对已有Run，无需重跑模型或重新上传；参考[真实误判修复](evidence/m0-01-live/trace-diagnosis.md)。

模型授权：当前live仅接受m0-normal-1-v2合同，必须显式model_profile（request_model/accepted_response_model/version_scope/thinking/reasoning_effort），且逐响应核验报告模型。当前仅支持reported_alias=deepseek-v4-flash，拒绝无法证明的fixed_weights批准；旧v1文件不可自动迁移，原实验不重跑。参见[审查修复](evidence/m0-01-live/pr-review-closure.md)。
批准合同v2还需runtime={python,implementation}，明确完整Python版本（包括patch）及CPython实现。入口在claim前核对实际运行环境与锁定依赖闭包（含当前平台marker和binary extra）；旧/缺失依赖或Python错配固定拒绝。此核对不自动安装环境，也不承诺检测伪造distribution元数据或被篡改的二进制。
错误审计：显式本地setup还会创建m0_live_diagnostics，与m0_live_once通过experiment_id关联；业务/outbox与business_code同事务，trace状态与trace_code同事务。原实验行不改写，无诊断行代表历史未记录；连接不可用时不能声称错误码已落盘，需保留CLI固定分类。本次不向生产数据库安装或迁移。

最终内容诊断：`LIVE_FINAL_JSON_INVALID` 表示最终文本不是可解析 JSON；`LIVE_FINAL_SCHEMA_MISMATCH` 表示非对象或字段集合不符；`LIVE_FINAL_TARGET_MISMATCH` / `LIVE_FINAL_EVIDENCE_MISMATCH` 区分对应值错配。仅保存固定代码，不导出正文；原历史 LIVE_PROTOCOL_FAILED 不追溯重分类。

### 本轮真实软件环境与上游基线

2026-09-09已固定并实际部署OTel Demo 2.0.2，HolmesGPT固定上游源码/独立锁依赖运行；[环境复现与保留路径](evidence/m0-real-environment/reproduce.md)、[当前总任务](tasks/2026-09-09-m0-real-investigation.md)。专属Colima profile和运行数据属于production-ops-agent-m0-environment原工作区；合入代码不自动搬移其tmp/VM/数据库，不从其他worktree盲目启停同名服务。原PG仍属于production-ops-agent-m0-01。真实模型/故障仅按当前实验合同执行，产品Agent仍只读；安装/开发检查不证明调查正确。
