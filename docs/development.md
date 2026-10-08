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
- `make red-proof`：把本分支已提交的新增/改动测试放到与 `origin/main` 的 merge-base 代码上重放，至少一个失败才算有红证明；只收集失败（新模块 ImportError）标为弱红证明；只改了 `tests/` 下支持模块时报「需人工确认」。本地退出码 0 有红证明或不适用、1 无红证明或需人工确认、2 内部错误；CI 的 `red-proof` 工作流试行期用 `--report-only`，退出码恒 0、只报告，只钉住已有行为的 PR 加 label `no-red-proof`。集成测试需按下文自行准备数据库并设置 opt-in 变量，否则记为跳过。
- `python3 scripts/doctor.py --lab`：额外检查 Docker/Compose、daemon、kubectl 和 Helm；不会启动、安装或连接 Kubernetes 集群。

基础诊断失败返回 1；基础可用而请求的实验设施缺失返回 2；所请求的诊断均满足前提返回 0。
锁文件存在、锁与声明一致、虚拟环境已同步是不同结论。doctor 不证明完整依赖匹配锁文件；setup 成功负责该次同步，后续人工改变环境须重新同步。
pytest 没有收集到测试返回 5，不算通过；make 可能将子命令错误映射为自己的非零退出码，原始错误仍在输出中。

## 数据库迁移（Alembic）

2026-10-05 起 schema 由 Alembic 版本化（[ADR-0007](adr/0007-data-access-raw-sql-with-standard-tools.md)，[任务记录](tasks/2026-10-05-m1-prep-schema-migrations.md)）。迁移文件在 `opspilot/migrations/versions/`，内容全部是 `op.execute` 的 SQL，不用 ORM。基线 `0001_baseline` 是 commit 3d6c94f 四个 `install()` DDL 的逐字搬入，没有 downgrade（没有更早的 schema 可回退，而删表会毁掉业务记录恢复权威）；**之后的每一条迁移都必须带能撤销 `upgrade()` 的 `downgrade()`**，`tests/test_schema_baseline.py` 会拒绝 `pass`/`raise` 的 downgrade。

- `OPSPILOT_DSN=<owner 连接> make migrate`（可加 `MIGRATE_FLAGS=--accept-column-order`，见下）：在启动 web/worker **之前**，用拥有 DDL 权限的连接执行。空库直接升到 head；已有版本表的库增量升级；由旧版 `install()` 建成、没有版本表的库先接管：比对该库与同服务器上一个新建空库升到 head 后的 `pg_dump --schema-only`（只含 `opspilot_*` 对象，去掉注释与会话设置），逐字一致才 `alembic stamp`，否则退出 1 并打印 diff、不改任何东西。接管需要与服务端同大版本的 `pg_dump`（不在 PATH 时设 `OPSPILOT_PG_DUMP`）和该角色的 CREATEDB 权限。
- 接管被拒时（退出 1，stdout 末尾是 `--- fresh head` / `+++ existing database` 的 unified diff）：拒绝是默认且唯一的自动行为，工具不会 stamp、不会改表。操作者按 diff 分类处理：① 旧库缺表/缺列/缺索引（diff 只有 `-` 行）——说明旧版 `install()` 从未在该库跑到当前版本，先用**旧版代码**（基线所对应的 commit 3d6c94f 之前的 web/worker，或直接 `psql -f tests/integration/legacy_schema_2026-10-05.sql`，内容与旧版 `install()` 逐字相同、全部 `IF NOT EXISTS`）把库补到旧版头，再重跑 `make migrate`；② 列顺序不同、类型/默认值/约束相同（同一列一行 `-` 一行 `+`，出现在不同位置；拒绝信息会提示 `--accept-column-order`）——这是经历过历史 `ADD COLUMN` 的库的正常形态（已知两例：`opspilot_incidents.lifecycle`、`opspilot_steps.sequence`，见任务记录），用户 2026-10-05 决定（方案 C）：重跑 `make migrate MIGRATE_FLAGS=--accept-column-order`，它只把每个 `CREATE TABLE` 块内的列行排序后再比对，类型、默认值、NOT NULL、约束、索引、identity、序列仍逐字比对，只有列顺序之差时 stamp 并打印/记录被接受的 diff；把这段 diff 原样写进任务记录或 PR。产品 SQL 不依赖列顺序由 `tests/test_sql_column_order_independence.py` 静态把守（INSERT 必列列名、行按名读取）；③ 多出的列/表/索引或默认值不同——该库被手工改过或不是本产品的库，不要 stamp，查清来源。任何手工 `alembic stamp` 都是绕过比对，须写进任务记录并附 diff。
- 部署步骤：`make migrate` 之后，任何**非 owner** 的运行时角色（如 M1-02 的 Observer）除业务表权限外还要 `GRANT SELECT ON alembic_version TO <运行时角色>`；否则 `install()` 抛 `PersistenceError("SCHEMA_VERSION_UNREADABLE")`（cause 里带这条 GRANT），不是 `SCHEMA_NOT_MIGRATED`，也不是一般的 `STORAGE_UNAVAILABLE`。
- `OPSPILOT_DSN=... .venv/bin/python -m opspilot.schema check`：只读校验当前版本是否为 head，退出 0/1。
- 运行时 `DurableStore.install()` 及三个 web 模块的 `install()` 签名不变，但只做上面这项校验，不是 head 就抛 `PersistenceError("SCHEMA_NOT_MIGRATED")` 拒绝启动；因此 M1-02 的受限 Observer 角色不需要 DDL 权限。调用 `install()` 的脚本（`scripts/m1_live_runner.py` 等）同样要先 `make migrate`。
- `0002_state_checks` 给 `opspilot_runs.state`、`opspilot_incidents.lifecycle` 加 CHECK，取值等于 `opspilot/domain` 的 `RunExecution`、`IncidentLifecycle`（`tests/test_schema_state_checks.py` 比对）。升级前先数已有非法值，有则抛 `IllegalStateValues`（表/列/值/行数）、退出 1、不改数据，库停在上一版本；修好数据后重跑 `make migrate`。旧库接管现在是：与新建库升到 `0001_baseline` 的 dump 比对 → stamp 0001 → 升到 head，输出 `schema stamped: <head>`。其余枚举型列为何未加约束见任务记录 PR-c 段。
- `0003_observation_store`（M1-02 第 2 步，#84）：建 `opspilot_health_profiles`（按 `health_profile_revision` 去重保存 profile 的规范化 JSON 文本与 sha256，CHECK revision = `<profile_id>@<sha256 前 12 位>`；授权会话时传入内容并校验）、`opspilot_observation_sessions`（观察会话，含授权时固定的期限/次数/间隔/持续窗口、已采纳水位和唯一的活动采样任务）、`opspilot_observation_samples`（每次提交的采样与判定依据）、`opspilot_observation_signal_readings`（逐信号读数），给 `opspilot_incidents` 加 `observation_generation` 列；状态列 CHECK 等于 `ObservationPurpose`/`ObservationSessionState`/`SampleOutcome`/`SampleReason`/`IncidentLifecycle`（`tests/test_schema_observation_store.py` 比对）。同一迁移建 `NOLOGIN` 角色 `opspilot_observer`（集群级，已存在则跳过）并只授予采样所需的最小权限：`alembic_version`、目标/挂起表只读；`opspilot_incidents` 只读身份与生命周期列、只能更新 `lifecycle`；会话表只能更新水位、任务槽与状态列；采样表与读数表只能插入。触发器 `opspilot_incidents_observer_lifecycle_guard` 再把 Observer 角色（`pg_has_role` 成员，含嵌套；superuser 与表 owner 除外）对 `lifecycle` 的改动限制在 `observing_recovery → resolved | open` 两条边，其他角色不受影响。约束触发器 `opspilot_incidents_observer_lifecycle_evidence`（DEFERRABLE INITIALLY DEFERRED）在提交时要求该改动在同一事务内有 `opspilot_observation_endings`（append-only，Observer 只能插入）里本事故会话的结束记录且 transition 与边一致，`resolved` 还须指向同事务写入的确认采样行。两个触发器的边界：它们只防 Observer 代码或 SQL 的错误（裸 `UPDATE`、复用旧行、写错边），不防被攻陷的 Observer 凭据——Observer 凭据本身就是观察判定的权威，可以伪造自己的采样行与结束记录；要防这一层须把写 `resolved` 拆到独立角色，不在本切片。另：暂停生效期间授权的会话，恢复后首次被领取或提交即以 `scope_suspended` 结束，需第 3 步重新授权。读数表的 `raw bytea`（≤128 KiB，须带 `raw_sha256`）保存原始返回供重放核对哈希。Observer 进程的登录角色在仓库外建（`CREATE ROLE <login> LOGIN IN ROLE opspilot_observer`，凭据不入库；建议不给它数据库级 TEMP 权限——`REVOKE TEMP ON DATABASE <db> FROM <login>`——迁移不改 PUBLIC 的 TEMP 默认，以免影响其他角色）。领取采样任务在事故行锁下重读控制范围，与暂停路径串行。downgrade 删触发器、三张表与列、`DROP OWNED BY` 收回本库授权，只有集群里再无数据库引用该角色时才 `DROP ROLE`。
- `0004_target_identity`（M1-02 第 3 步，#85，用户决定 2026-10-07）：给 `opspilot_targets` 补 `integration_id`、`cluster_uid`、`namespace` 三列（**可空**，有值须非空 CHECK），与已有 `resource_uid` 共同构成不可变目标身份；`revision` 不属于身份，只在授权观察时快照到会话行。事故接收保持 M1-01 合同：仍只按 `resource_uid` 自动登记目标（`DurableStore.register_target(resource_uid)` 不变），迁移不需要任何输入，已有行三列留空。完整身份只在「登记处置 / 授权观察」时要求：`ObservationStore.register_remediation` 在同一事务里从工作台传入的配置身份（`OPSPILOT_TARGET_IDENTITIES` JSON 文件，格式 `{"<target_id>": {"integration_id": "...", "cluster_uid": "...", "namespace": "...", "workload": "<Deployment/服务名>", "health_profile_id": "<profile_id>"}}`，五个字段都必填且为非空字符串，由 `opspilot.schema.load_target_identities` 严格校验——缺字段、空值、未知键、`resource_uid` 与键不符都在加载时拒绝并报出条目的 target_id（一个之后永远登记不了的条目不该等到登记时才失败）；`health_profile_id` 声明该目标适用的 HealthProfile，与工作台加载的 profile 的 `profile_id` 不同或缺失 → `HEALTH_PROFILE_TARGET_MISMATCH`；`workload` 随身份一起写入登记行，授权时存储层在事故行锁下把 profile 的 `subject.kubernetes_namespace` / `subject.service` 与登记行的 `namespace` / `workload` 比对，不等 → `HEALTH_PROFILE_TARGET_MISMATCH`，同样不写任何东西）首次补齐该行，之后不可改——登记行已完整而文件给的不同 → `TARGET_MISMATCH`；行仍为空且没有配置身份（文件未设或未列该 id）→ `TARGET_IDENTITY_MISSING`，两者都不写任何东西。downgrade 只删三列。本机 55431 lab 库合并后升级不再需要身份文件：`OPSPILOT_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab" OPSPILOT_PG_DUMP=/opt/homebrew/opt/postgresql@17/bin/pg_dump make migrate`。
- `0005_incident_mode`（M1-02 第 3b 步，#121）：给 `opspilot_incidents` 加 `mode text NOT NULL DEFAULT 'automatic'`，CHECK 取值等于领域 `ObservationMode`（`automatic`/`human_owned`，`tests/test_schema_incident_mode.py` 比对）；已有行为 `automatic`，downgrade 删列。工作台动作 `takeover` 在同一事务递增代际、把 `mode` 置为 `human_owned`、撤销观察授权、把当前 Run 交给人（`waiting_human`）；之后 worker `claim` 为 `CONTROL_DENIED`，`new_run` 与自动续开被拒，带内容的追问/纠正只记录为输入（不建/不领 Run），`resume` 只解除事故级暂停（mode 不变、Run 仍停在 `waiting_human`、不恢复自动调查；全局/目标挂起优先，挂起期间 resume 后镜像仍 `paused`），再次 `takeover` 撤销 human_owned 下单独登记的新观察，`register_remediation` 仍可单独授权观察（C3 §10）。领域模型没有回到 `automatic` 的边，产品也不提供（#124）。`downgrade` 在存在 `human_owned` 事故时拒绝（`HumanOwnershipWouldBeLost`，报出数量、不改数据）：删列再升级会把人工接管变回 automatic。
- 新增迁移：`.venv/bin/alembic revision -m "<说明>"`（配置在 `pyproject.toml` 的 `[tool.alembic]`，DSN 来自 `OPSPILOT_DSN`），文件名改成 `000N_<slug>.py`、`revision` 同名，SQL 写进 `op.execute`，补 `downgrade()`。改表结构属用户门 PR。
- 测试：`tests/integration/conftest.py` 在 `M1_DURABLE_POSTGRES=1`/`M0_B_POSTGRES=1` 下先对 lab 库跑一次 `migrate`（lab 用户是 owner），现有测试里的 `install()` 调用不动；设 `OPSPILOT_LAB_DSN`（库名仍须是 `m0_budget`；由 `scripts/m0/postgres_lab.py` 读取，子进程也继承）可把整套集成测试指向另一端口的临时实例，而不动 55431 的 lab 与其数据；lab 库是旧版建的时会走接管，所以本机也要有 PG 17 的 `pg_dump`（Homebrew：`/opt/homebrew/opt/postgresql@17/bin`）。两条 CI 路径（空库→head；旧 DDL 建库→接管→head，并比对两边 dump）在 `m0-postgres` 作业里。

## 失败与恢复

先根据诊断定位缺少的解释器、工具或 daemon。离线锁检查因本地材料缺失失败时，记录环境前提缺失，不冒充代码失败或通过。
setup 涉及安装；check/test 可能写工具缓存和测试临时文件，但不安装依赖。doctor 的子命令均为只读查询并设 5 秒超时，不输出环境变量或原始错误中可能出现的凭据。
Docker 不可用不影响默认基础检查；等对应实验获得准备授权后再处理设施。不要擅自删除环境或设置来排除错误。

## 验证证据

在对应任务记录中保存实际命令、工具版本、结果和重要失败。未提交状态下的结果标为 dirty 工作区验证，并保留本次受检脚本、配置、测试及锁文件的哈希或快照，不能只用 HEAD 标识版本。
临时日志的唯一副本不留在即将删除的 worktree 中。当前没有通用日志 runner、自动结果目录或产品服务；基础 CI 见下节。

## GitHub CI

任何 PR、push main 或手动触发 `.github/workflows/ci.yml`；Ubuntu 24.04、uv 0.10.8 与 Python 3.12.13，执行相同 make setup/check。CI 无业务 Secrets、dataset eval 或部署。setup 尊重 UV_PYTHON，避免安装与后续锁检查选择不同解释器。

PR 须通过最新 checks，按 AGENTS.md「PR 与合并」的就绪顺序处置审查，再按预授权类自动合并或交用户合并；平台保护与审查机器人接入状态见[交付与资源规划](plans/delivery-and-resources-2026-09-08.md)。

## 项目 LangChain 文档 MCP

从有本批变更的任务分支执行 `.venv/bin/python scripts/setup_docs_mcp.py`；默认配置当前目录，可用 `--project /绝对/项目目录` 安装到本仓库另一 worktree。脚本只修改三个项目文件：`.codex/config.toml`、`.mcp.json`、`.claude/settings.local.json`，无用户级服务器注册、模型调用、业务 key 或全量工具自动授权。未知同名配置冲突时拒绝覆盖，保留其他服务器和设置；可重复执行。

版本管理保存安装脚本，生成配置被忽略。原因是 Codex 运行入口为小写 `.codex/`，而仓库历史上曾保留大写 `.Codex/`（2026-09-21 已删除）：macOS 大小写不敏感时两者落入同一目录，Linux 则不同；安装时按宿主正确路径生成，避免 Git 同时保存仅目录大小写不同的树。Claude 同步生成本项目 `.mcp.json` 和两个具名服务器的本地启用设置；不修改共享 AGENTS 规则或复制 skills。

Codex 仅启用文档搜索/虚拟文档读取、API 搜索/符号读取四项工具；不设 required，断连时回退官方网页及源码。Claude 明确 deny 已知 `submit_feedback`，其余工具仍受宿主权限机制；不是对未来未知工具的完整白名单保证。文档服务不需要 DeepSeek/LangSmith key，不读取 `.env`；虚拟文档文件系统在远端官方资料内，不是本机文件访问。

用户授权本项目接入已落实到本地配置；新克隆/新 worktree 仍遵守宿主项目信任机制，脚本不自动信任所有目录。当前已打开的会话未必热加载，需重开/刷新 MCP 后验证。检查命令：`codex mcp get langchain-docs --json`、`codex mcp get langchain-reference --json`、`claude mcp get langchain-docs`、`claude mcp get langchain-reference`。配置读取、Connected 和实际查询是不同证据，见[任务记录](tasks/2026-09-08-agent-capabilities.md)。

停用时在项目 Codex 两个 server 表设置 `enabled = false`，并按 Claude 的项目 MCP 管理停用对应名字；只改这两项，保留其他服务器。重新启用先核对修改后的配置，不用安装脚本覆盖用户后续定制。Git 忽略不等于可删除，清理工作区前按原 worktree 约定保留需要的本地配置与私有资料。

## 本地运行演示：网页提交 → 常驻 worker → 真实调查

三个进程：本 worktree 的 lab PostgreSQL（55431，数据留在 `tmp/m0-b/postgres`）、工作台 `python -m opspilot.web serve`（只收事故、记录调查输入、投影进度，不跑模型）、常驻 worker `python -m opspilot.worker_main`（轮询可领取的 Run → 清扫过期 Run → 经 `InvestigationRunner` 真实调查，工具用 `opspilot.tools.fixture` 的固定 Prometheus 回放，无真实 OTel）。两者必须用同一套 `versions` 与工具面（都来自 `opspilot.tools.fixture`），否则 Run 会在 claim 时被记为 `blocked`。真实模型调用按 AGENTS.md「费用与真实调用」常设授权，凭据只从环境或私有 env 文件读取，不打印。

```sh
.venv/bin/python -m scripts.m0.postgres_lab start
export OPSPILOT_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=m0_lab"
make migrate   # owner 连接把 schema 升到 head；web/worker 启动时只校验版本
# 工作台登录：哈希从 stdin 读密码，只打印哈希
.venv/bin/python -m opspilot.web hash-password
# 目标身份文件（可选，只在「登记处置」时读）：操作者 target_id → 不可变身份；不设或未列出的 target_id 登记处置被拒（TARGET_IDENTITY_MISSING），事故接收不受影响。
# fixture profile 的示例值（合成演示，不是任何真实集群的身份）：
echo '{"checkout-prod": {"integration_id": "fixture", "cluster_uid": "fixture-cluster", "namespace": "otel-demo", "workload": "checkout", "health_profile_id": "otel-demo-checkout"}}' > /tmp/opspilot-targets.json
OPSPILOT_TARGET_IDENTITIES=/tmp/opspilot-targets.json \
OPSPILOT_UI_USERS="demo=<上面打印的哈希>" .venv/bin/python -m opspilot.web serve
# 「登记处置」用的 HealthProfile 默认是随产品发布的 otel-demo-checkout.json；OPSPILOT_HEALTH_PROFILE=<路径> 可换
# 另一个终端：worker（SIGTERM/Ctrl-C 停止领取，等在跑的尝试最多 OPSPILOT_WORKER_GRACE_SECONDS 秒）
OPSPILOT_ENV_FILE=/绝对路径/.env .venv/bin/python -m opspilot.worker_main
# 提交事故（目标固定为 fixture 的 checkout-prod；Origin 必须是绑定地址）
curl -u demo:<密码> -H 'Origin: http://127.0.0.1:8080' \
  -d target_id=checkout-prod -d idempotency_key=demo-1 \
  --data-urlencode 'question=Checkout appears to show elevated HTTP errors. Query the authorized metrics and return a json investigation report for the authorized window.' \
  http://127.0.0.1:8080/intake/ui
# 观察：浏览器打开 http://127.0.0.1:8080/incidents/<incident_id>，或订阅事件流
curl -N -u demo:<密码> 'http://127.0.0.1:8080/incidents/<incident_id>/events?cursor=0'
# 追问（交接或发布后；发布后会被 ILLEGAL_TRANSITION 拒绝，见 ADR-0005）
curl -u demo:<密码> -H 'Origin: http://127.0.0.1:8080' \
  -d action=follow_up -d expected_generation=<页面上的代际> -d idempotency_key=f-1 \
  --data-urlencode 'text=Also compare against the previous hour.' \
  http://127.0.0.1:8080/incidents/<incident_id>/control
# 接管（M1-02 第 3b 步）：人接管事故，同一事务撤销观察授权、停下自动调查（Run 交给人）；之后仍可 register_remediation 单独授权观察，但不恢复自动调查
curl -u demo:<密码> -H 'Origin: http://127.0.0.1:8080' \
  -d action=takeover -d expected_generation=<页面上的代际> -d idempotency_key=t-1 \
  http://127.0.0.1:8080/incidents/<incident_id>/control
# 登记处置（M1-02 第 3 步）：人工在系统外处置后登记，事故进入 observing_recovery，授权一个按 HealthProfile 定界的观察会话；
# revision 是处置后的目标版本（审计快照，不是身份）；同一 idempotency_key 重放不产生第二个会话；暂停/取消/续开/new_run 在同一事务撤销授权
curl -u demo:<密码> -H 'Origin: http://127.0.0.1:8080' \
  -d action=register_remediation -d expected_generation=<页面上的代际> -d idempotency_key=rem-1 \
  -d revision=checkout:v2.0.3 http://127.0.0.1:8080/incidents/<incident_id>/control
.venv/bin/python -m scripts.m0.postgres_lab stop
```

worker 会领取、清扫或按版本不符记为 `blocked` 同一数据库里的**每一个** Run，包括测试套件和 `scripts/m1_live_runner.py`（`tool_schema_revision: live-runner-1`）留下的行：只对没有其他套件或脚本在用的数据库运行它。worker 可同时跑多个实例（租约栅栏保证一个 Run 只有一个执行者）；被杀的 worker 留下的租约最多 `LEASE_SECONDS`（420 秒）后过期，下一次 claim 从已提交的行继续。`OPSPILOT_WORKER_POLL_SECONDS`（默认 2）、`OPSPILOT_WORKER_BATCH`（默认 20）可调。`DurableStore.transaction()` 的连接来自进程内的 `psycopg_pool` 池（ADR-0007，#77）：`OPSPILOT_POOL_MIN_SIZE`（默认 1，空闲时保留的连接数）、`OPSPILOT_POOL_MAX_SIZE`（默认 4，单进程并发事务上限，超出的请求排队）、`OPSPILOT_POOL_TIMEOUT_SECONDS`（默认 5，排队等待上限，超时按 `TIMEOUT` 报给调用方）、`OPSPILOT_POOL_MAX_IDLE_SECONDS`（默认 60，多余空闲连接的回收时间）；同一进程里相同 DSN 与配置的 `DurableStore` 共用一个池，池在第一个事务时才建、每个事务仍是独立的 commit/rollback 与 `SET LOCAL` 超时，数据库侧 `max_connections` 要容得下 web 与 worker 各自的 `MAX_SIZE` 加运维连接（lab 实例是 12）。工具次数与秒数经 `DurableToolLedger` 落在 Run 行（`tool_operations_used` / `tool_seconds_used`），重启不归零。这是开发入口，启动时只校验 schema 版本（迁移见上文「数据库迁移」），不是部署工件。

## 真实 OTel Demo 工具 profile（`OPSPILOT_TOOL_PROFILE=otel-demo`）

worker 与工作台各有一个工具 profile 开关 `OPSPILOT_TOOL_PROFILE`（`opspilot/tools/profiles.py`）：默认 `fixture`（上面的固定回放，CI 与测试用），`otel-demo` 让 worker 通过 HTTP 只读查询固定的 OTel Demo 2.0.2 实验环境的 Prometheus（`metrics_range_query`，范围查询）与 Jaeger（`traces_search`，按服务搜 trace 并投影为 M0 trace view v3 的采样记录）。**两个进程必须设同一个值**：`versions` 里的 `tool_schema_revision` 不同，Run 在 claim 时会被记为 `blocked`。观测窗是提交时刻往前 300 秒，由工作台写进 Run 的输入快照，worker 从快照读回（超时后续开的新 Run 沿用前一 Run 的历史窗口，不重新取窗）；目标固定为 `m0-otel-20260909`（提交事故时 `target_id` 必须是它）。后端地址用 `OPSPILOT_OTEL_PROMETHEUS_URL`（默认 `http://127.0.0.1:19090`）、`OPSPILOT_OTEL_JAEGER_URL`（默认 `http://127.0.0.1:16686/jaeger/ui`）。M1-02 第 4 步起实验环境 Prometheus 开启 basic auth：调查侧用自己的只读账号 `OPSPILOT_OTEL_PROMETHEUS_USERNAME` / `OPSPILOT_OTEL_PROMETHEUS_PASSWORD`（`kind_lab.py up` 写到 `tmp/m1-kind-lab/prometheus-investigator.env`，`set -a; . <file>; set +a` 后启动 web 与 worker；只对 Prometheus 请求发送，Jaeger 仍匿名）；`OPSPILOT_OTEL_TOKEN` 是 bearer 后端用的另一种凭据。凭据只进请求头，不进注册、证据或日志；调查侧不读 Observer 的 `OPSPILOT_OBSERVER_*`，反之亦然（C3 §3、D3，`tests/test_m1_prometheus_credentials.py`）。

实验环境属工程操作，不是产品或 Agent 能力：镜像与配置沿用 M0 冻结的 `compose-pinned.json`（默认在 `production-ops-agent-m0-environment/tmp/m0-environment`，可用 `OPSPILOT_OTEL_LAB` 指向别处；重建见 [环境复现](evidence/m0-real-environment/reproduce.md) 与 `scripts/m0_environment/prepare.py` / `freeze_images.py`）。

```sh
python3 scripts/otel_demo_lab.py up        # colima start m0-otel（4 CPU/6 GiB）+ compose up，等两个后端就绪
python3 scripts/otel_demo_lab.py health    # Prometheus 有 span metrics、Jaeger 列出 checkout 才算就绪
python3 scripts/otel_demo_lab.py fault inject --experiment-id <id>   # 开发故障：paymentFailure flag 100%（M0 development_fault.py）
python3 scripts/otel_demo_lab.py fault restore --experiment-id <id>
python3 scripts/otel_demo_lab.py stop      # compose stop + colima stop，不删容器、卷或 profile
python3 scripts/otel_demo_observe.py <start_iso> <end_iso> <out.json> [--expect fault|normal]   # 独立观察：直接读 Prometheus/Jaeger，输出控制窗前提与故障确认（父子关系谓词）
```

故障钩子只改实验目录里的 flagd 文件并保存前后字节与 SHA256；调查者与模型没有它的入口。

### kind 实验环境（M1-02 第 0 步起）

Compose 环境目录已不存在；M1-02 起实验环境是 colima profile `m1-kind`（4 CPU / 8 GiB）上的 kind 集群 `opspilot-m1`，装锁定版本的 OTel Demo Helm chart（0.37.8，appVersion 2.0.2，按 `scripts/kind_lab/values.yaml` 精简）和 kube-state-metrics 8.6.0；Prometheus / Jaeger / frontend 经 NodePort 发布到与 Compose 相同的 127.0.0.1:19090 / 16686 / 18080，产品 profile 的默认地址不变。证据与实测见 [docs/evidence/m1-02-lab/run.md](evidence/m1-02-lab/run.md)。

```sh
.venv/bin/python scripts/kind_lab.py up        # 查宿主可回收内存 ≥ 3 GiB → colima start → kind create → helm upgrade --install ×2 → 等 Deployment 就绪（首次拉镜像约 20 分钟）
.venv/bin/python scripts/kind_lab.py health    # span metrics、kube-state-metrics series、Jaeger 列出 checkout、Deployment 全就绪
.venv/bin/python scripts/kind_lab.py fault inject --experiment-id <id>    # patch flagd-config ConfigMap：paymentFailure → 100%（指标上约 3 分钟后可见）
.venv/bin/python scripts/kind_lab.py fault restore --experiment-id <id>
.venv/bin/python scripts/kind_lab.py stop      # 只 colima stop；集群与 release 保留，再 up 约 90 s
```

Prometheus 基本认证（M1-02 第 4 步，D3）：`up` 首次生成 `tmp/m1-kind-lab/prometheus-auth.json`（600，四个账号 observer / investigator / collector / lab 的口令），把 bcrypt 哈希渲染成 Secret `prometheus-web-config`（Prometheus `--web.config.file`）、把 collector 推送用的口令渲染成 Secret `prometheus-web-auth`（collector `basicauth` 扩展），并写 `prometheus-observer.env` / `prometheus-investigator.env`（600，各只含该角色自己的变量名）；`kind_lab.py env-file <role>` 可重写。口令不进仓库、values、日志与证据。限制：Prometheus 自带 basic auth 对所有账号权限相同，合同要求的是独立凭据而非不同权限；Prometheus 探针改为 TCP、configmap-reload 侧车关闭（二者否则需要凭据或会 401）。工程脚本（`kind_lab.py health`、`scripts/otel_demo_observe.py`、`docs/evidence/m1-02-health-profile/collect_baseline.py`）经 `kind_lab.lab_authorization()` 用 `lab` 账号：优先 `OPSPILOT_LAB_PROMETHEUS_USERNAME/_PASSWORD`（`kind_lab.py env-file lab`），否则读 auth 文件；都没有时请求匿名、401 如实报出。

脚本在宿主上只写 git 忽略的 `tmp/m1-kind-lab/`：故障历史（`engineer-only/<experiment-id>/`，越出该目录的 id 或符号链接被拒绝）、实验环境专用 kubeconfig（kubectl/helm/kind 都显式用它，不碰 `~/.kube/config` 的当前上下文）和 Helm 的仓库配置与 chart 缓存（`HELM_CONFIG_HOME`/`HELM_CACHE_HOME`/`HELM_DATA_HOME`）。`fault` 在 patch 后重新读取 ConfigMap 比对 SHA-256，不一致则非零退出。
刚启动的环境要过几分钟才有足够样本（`increase(...[5m])` 需要两个以上采样点）。然后按上一节的三进程流程运行，只把 web 与 worker 都加上 `OPSPILOT_TOOL_PROFILE=otel-demo`，提交时 `target_id=m0-otel-20260909`。

### 独立 Observer 进程（M1-02 第 4 步起）

`python -m opspilot.observer` 是确定性的恢复观察进程（C3 §10，F6）：轮询 → `sweep_expired_sessions` → `claim_due_samples` → 对每个租约按会话绑定的 HealthProfile revision（内容取自 `opspilot_health_profiles`，须能重算出同一 revision）向 Prometheus 发即时查询（每个信号三条：`query`、`coverage_query`、`freshness_query`，都在窗口末尾求值；**每条请求前**重新校验租约的 scope generation，已变化则不再发请求、提交部分读数由存储按 `suspended` 结束会话）→ 查询返回后再取 `sample_time` → `evaluate_readings` → 原子 `submit_sample`。不调用模型，不 import 调查 worker、工具网关与 web（`tests/test_m1_observer.py` 以子进程校验）。

只读自己的环境变量，不回退到调查侧的 `OPSPILOT_DSN` / `OPSPILOT_OTEL_*` / `DEEPSEEK_API_KEY`：

```sh
export OPSPILOT_OBSERVER_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=<Observer 登录角色>"   # CREATE ROLE <login> LOGIN IN ROLE opspilot_observer（仓库外）
export OPSPILOT_OBSERVER_PROMETHEUS_URL="http://127.0.0.1:19090"                                      # Observer 自己的只读端点
export OPSPILOT_OBSERVER_ENV_FILE="$PWD/tmp/m1-kind-lab/prometheus-observer.env"                     # 含 OPSPILOT_OBSERVER_PROMETHEUS_USERNAME / _PASSWORD（Observer 自己的 basic-auth 账号）
# 可选：OPSPILOT_OBSERVER_PROMETHEUS_TOKEN（bearer 后端）、OPSPILOT_OBSERVER_POLL_SECONDS（5）、OPSPILOT_OBSERVER_BATCH（20）、OPSPILOT_OBSERVER_GRACE_SECONDS（30）；凭据变量可直接设在环境或放在 ENV_FILE 里
.venv/bin/python -m opspilot.observer
```

启动时同样校验 schema 在 Alembic head（角色由 0003 授予 `alembic_version` 只读）。授权会话（人工登记处置）属第 3 步 #85；在其合并前，工程侧可用 `docs/evidence/m1-02-observer/lab_run.py prepare` 以 owner DSN 调用存储原语创建会话。

### 离线重放（M1-02 第 5 步，#87，F6 第 5 步）

`python -m opspilot.observer.replay` 只读已存行重算恢复判定：会话行、`opspilot_health_profiles` 里冻结的 profile 内容（须能重算出会话绑定的 revision）、每条采样行及其判定条件、每条读数行的原始捆绑。三层各自重算并与已存比对：捆绑重新哈希并重建读数（`sampler.replay_sample`）→ 按 profile 阈值/新鲜度/流量门在捆绑记录的 `sample_time` 重判 outcome（`evaluate_readings`）→ 用重算 outcome 走存储层同一折叠（`observation.store.fold_history`：采纳、健康连续期、持续窗口、次数、结束记录、生命周期）。任何一层不一致（捆绑哈希不符、读数/outcome/判定重算不同、profile 不可读或 revision 不符、会话终态或结束记录不符）都报 `STORED_OBSERVATION_INTEGRITY_MISMATCH`，`recovery_verdict` 为 `unknown`，不复述已存判定；捆绑实际重算出的结论另列为 `recomputed_verdict`。不查遥测、不调模型、不看墙钟。

```sh
export OPSPILOT_OBSERVER_DSN="host=127.0.0.1 port=55431 dbname=m0_budget user=<Observer 登录角色>"   # 或 --dsn；角色对所需表只读
.venv/bin/python -m opspilot.observer.replay --session <session_id>      # 可重复；或 --incident <incident_id> 重放该事故全部会话
```

重放核对的范围：会话参数须等于冻结 profile 的 `session` 值（次数、持续窗口、间隔；期限按行创建时间与 `deadline_seconds` 核对）；采样行的判定条件（期限按会话冻结的 `deadline_at` 与采样窗尾重算，挂起按采样行记录的控制代际与授权代际重算，不信已存标志）、会话行水位（已采纳序号/窗尾/次数/健康连续期起点须等于重算值）、结束记录（恰好一条且须由重算结局支撑：次数耗尽须重算已采纳次数 ≥ 冻结 `max_samples`，无采样的期限结束须记录在冻结期限之后）、每条读数的 query / coverage / freshness 表达式与窗口须等于冻结 profile 与采样窗口。Observer 在采样时取不到或无法验证 profile 行的采样，以一条 `health_profile` 哨兵读数（哈希捆绑记录 `HEALTH_PROFILE_UNAVAILABLE` / `HEALTH_PROFILE_INVALID` 与 revision）持久记录原因，重放只在验证该记录（含其 outcome 必为 failed、窗口绑定采样与捆绑）后才豁免读数覆盖检查；没有记录的空读数即完整性不一致。**边界**：重放只能检出与已存原始数据、哈希、判定或会话状态互相不一致的篡改；拥有数据库写权限者把原始数据、哈希、判定与会话一致地改写无法被检出，这不是本步目标（业务记录权威与写权限边界见 ADR-0003 与 PRODUCT-CONSTRAINTS）。

stdout 为 JSON 列表（每会话一项：`consistent`、`integrity`、`reasons`、`recovery_verdict`、`recomputed_verdict`、`healthy_window_seconds`、`expected_lifecycle`/`recorded_lifecycle`、逐采样的 stored/replayed outcome 与判定、逐读数的 stored/replayed 状态、值、点数、查询、窗口、来源、sha256；不含原始字节）。退出码：0 全部一致，1 有不一致，2 用法或存储错误。纯函数入口 `opspilot.observer.replay.replay_history(session_history)` 与 F6 验收形态的 `replay(profile, handled_at, samples, session=..., endings=..., allow_telemetry=False, allow_model=False)`（任一 allow 为 True 直接拒绝 `REPLAY_IS_OFFLINE`）供测试与验收夹具使用。事故页「Recovery verdict」区块显示同一结果与逐采样依据，与「Investigation report」分开，页面不增加写动作。

### F6 验收投影（#140）

`opspilot.acceptance.recovery_outcome(scenario, RecoveryRecords(...)) -> RecoveryOutcome` 是恢复观察的外部验收入口（AGENTS.md「验收入口是外部 IncidentScenario -> IncidentOutcome」），与调查 Run 的 `IncidentOutcome` 并列而非扩展：Run 的 `final_state` 不是事故生命周期，F6 字段来自另一组已提交记录。输入由调用方一次快照读出：`ObservationStore.incident_records(incident_id)`（事故行、每个会话的 `session_history`、`opspilot_controls` 审计行）和 `ObservationStore.table_privileges()`（在 Observer 自己的连接上实测）。判定、原因、交接、健康窗口全部取 `opspilot.observer.replay.replay_history` 的 `SessionReplay`（`recovery_verdict / recovery_reasons / handoff / handoff_reasons / observation_ended / ended_reason`，在线判定与重放共用同一逻辑；完整性不一致 → `unknown` + `STORED_OBSERVATION_INTEGRITY_MISMATCH`），投影只做复制与格式转换，不解析 profile、不另算原因；目标取会话绑定的不可变 `target`；读数的 `evaluated_at` / `sample_time` 取捆绑记录的时刻（捆绑不验证则为 None），不用窗尾冒充。

`actions` 按记录顺序投影：控制审计行 `register_remediation` → `record_handling`，再加 `advance_incident_lifecycle` 仅当生命周期确实改变（第 k 次登记对应第 k 个会话；首个会话前事故为 `open`，之后以上一个会话最后一条结束记录的 `lifecycle_after` 为准，没有记录则不声称）；`takeover` → `human_takeover`；其他 → `human_control:<action>`；每条读数捆绑里实际发出的即时查询各记一次 `read_only_query`（detail 为 `LEASE_BUDGET` 的本地填充、哨兵读数、捆绑不验证都不计）；每采样行 `persist_observation`；每结束记录：生命周期变化 → `advance_incident_lifecycle`，期限/次数结束 → `human_handoff`。

`table_privileges()` 的测量范围：当前库所有非系统 schema 的普通表与分区表（整表权限记 `"*"`，SELECT/INSERT/UPDATE 另按 `has_column_privilege` 逐列记录精确列集合）、序列（USAGE/SELECT/UPDATE）、schema（CREATE/USAGE）、数据库（CREATE/TEMP）、当前角色可 EXECUTE 的 volatile 非触发器 SECURITY DEFINER 函数。`permissions_from_grants` 把测量结果与 0003 迁移授予 Observer 的精确列集合（`OBSERVER_GRANTS`，单测与迁移源逐列核对）比较：能读全部所依据记录 → `read_only`（否则 `unreadable:<表>`），有控制审计行 → `human_control`，多出的能力一律列出：`record_rewrite:<表>(列…)`、`investigation_write:<表>(列…)`、`record_delete:<表>`、`foreign_write:<schema.表>(列…)`、`sequence_write:<schema.序列>`、`schema_create:<schema>`、`database_create` / `database_temp`、`security_definer_execute:<函数>`；空测量拒绝。**限制**：SECURITY DEFINER 函数的函数体是否写库无法可靠判定，凡当前角色可执行的 volatile 定义者函数都报出；PUBLIC 默认的数据库 TEMP 权限会如实出现为 `database_temp`（部署建议 `REVOKE TEMP ON DATABASE … FROM <login>`，见 0003 说明）；Observer 对实验环境的「只读」来自其进程只持有 Prometheus 只读凭据（D3），数据库测不到，投影不臆造。

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

本节不带批准文件的命令：offline成功退出0、异常退出1；check-config退出2表示仅核查配置而未批准执行；live缺少批准文件时退出3（LIVE_NOT_ENABLED）。当前live并非无条件禁用：完整配置和独立有效批准合同满足条件时可执行，见下方“受批准合同约束的真实入口”。预算数字本身不能打开入口。2026-09-21 起真实调用为常设授权：批准文件由执行 Agent 按 SPEC「Operating constraints」自行填写，不再等待用户逐次批准。

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

[具体方案](evidence/m0-01-live/plan.md)和[当前任务](tasks/2026-09-08-m0-01-preflight.md)是范围及授权依据。`live` 默认仍退出3；只有显式 `--env-file /absolute/private.env --approval-file /absolute/private-approval.json` 才进入批准合同校验。缺字段、未批准、错误hash/身份、非正预算、过期均在外部请求前拒绝。批准文件是执行 Agent 在常设授权下填写的0600本地技术记录（2026-09-21 起不再逐次请求用户批准），不是授予模型的权限，不承诺抵御可修改本机代码/数据库的恶意操作者。

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

模型授权：当前live仅接受m0-normal-1-v2合同，必须显式model_profile（request_model/accepted_response_model/version_scope/thinking/reasoning_effort），且逐响应核验报告模型。当前仅支持version_scope=floating_alias、accepted_response_model=deepseek-flash（2026-09-15 用户决定；旧名 deepseek-v4-flash 对应的模型已由供应商退役。`deepseek-flash` 是浮动别名，名称校验只能证明「是当前 Flash」，不能证明代次未变；与冻结校准比较前须另经 `/models` 元数据核对。批准合同必须声明 `models_metadata_sha256`，即所批准的官方 `/models` 快照摘要，缺失或非 64 位小写十六进制一律拒绝，并由 claim() 写入可读列 m0_live_once.models_metadata_sha256；缺该列的旧 lab schema 同样拒绝，须重建），拒绝无法证明的fixed_weights批准。**既有批准合同文件须重发**：`live.py` 对 `contract["model_profile"]` 做整字典相等比较，任何仍写旧 `request_model`/`accepted_response_model` 的合同会直接得到 `LIVE_MODEL_PROFILE_MISMATCH`；旧v1文件不可自动迁移，原实验不重跑。参见[审查修复](evidence/m0-01-live/pr-review-closure.md)。
批准合同v2还需runtime={python,implementation}，明确完整Python版本（包括patch）及CPython实现。入口在claim前核对实际运行环境与锁定依赖闭包（含当前平台marker和binary extra）；旧/缺失依赖或Python错配固定拒绝。此核对不自动安装环境，也不承诺检测伪造distribution元数据或被篡改的二进制。
撰写 prompt 与 tool description：分层、revision 生成与 bump 条件见 [C3 第 5 节「指令分层与版本」](design/technical-proposal-2026-09-07.md)，模型可见的工具描述必填项与禁止项见同文件第 8 节；按当前模型官方特性的撰写规则（排列顺序、措辞、`cannot_prove` 写法、实现前检查清单）见 [DeepSeek V4.1 Flash 设计参考](design/deepseek-flash-prompt-tool-reference.md)。改动送模文字前先读这两处；参考文档为日期绑定，供应商变更时重核。

错误审计：显式本地setup还会创建m0_live_diagnostics，与m0_live_once通过experiment_id关联；业务/outbox与business_code同事务，trace状态与trace_code同事务。原实验行不改写，无诊断行代表历史未记录；连接不可用时不能声称错误码已落盘，需保留CLI固定分类。本次不向生产数据库安装或迁移。

最终内容诊断：`LIVE_FINAL_JSON_INVALID` 表示最终文本不是可解析 JSON；`LIVE_FINAL_SCHEMA_MISMATCH` 表示非对象或字段集合不符；`LIVE_FINAL_TARGET_MISMATCH` / `LIVE_FINAL_EVIDENCE_MISMATCH` 区分对应值错配。仅保存固定代码，不导出正文；原历史 LIVE_PROTOCOL_FAILED 不追溯重分类。

### 本轮真实软件环境与上游基线

2026-09-09已固定并实际部署OTel Demo 2.0.2，HolmesGPT固定上游源码/独立锁依赖运行；[环境复现与保留路径](evidence/m0-real-environment/reproduce.md)、[当前总任务](tasks/2026-09-09-m0-real-investigation.md)。专属Colima profile和运行数据属于production-ops-agent-m0-environment原工作区；合入代码不自动搬移其tmp/VM/数据库，不从其他worktree盲目启停同名服务。原PG仍属于production-ops-agent-m0-01。真实模型/故障仅按当前实验合同执行，产品Agent仍只读；安装/开发检查不证明调查正确。
