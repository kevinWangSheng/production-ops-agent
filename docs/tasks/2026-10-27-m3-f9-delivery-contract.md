# F9 第 1 实施步骤合同预审（镜像 + 控制侧 Compose 一键安装 + 版本与 tag + GHCR 发布 workflow）

- 预审者：未参与实现的只读 Agent，2026-10-28；基线 origin/main `db5360c3`（本地 `git fetch` 因网络 SSL 失败，用本地已有的 origin/main，与 main 同一提交）
- 依据：AGENTS.md「合同预审」；docs/tasks/README.md:21-40；M3 开放决策 Q3/Q4/Q10（docs/tasks/2026-10-27-m2-exit-m3-opening-decision.md:39-45,66-68）；F9 任务记录（docs/tasks/2026-10-27-m3-f9-delivery.md:10-23）；feature_list.json F9（:93 起，5 步，`passes:false`）；C3 §2 :33、§3 :50-62、§11 :417、§12 :433,445；SPEC.md:30,87
- 已查过 `~/.agent/handoffs/`：没有 F9 的既有裁决
- 裁决（r1，用户 2026-10-28）：A3 按推荐公开（决策稿第二轮 Q2）、A5 按推荐（Q5）、A2 按推荐（Q2）；A1、A4、A6 属可逆默认，按推荐由 Agent 定（A4 的工作台宿主端口在 da-server 上改为 `127.0.0.1:18080`，8080 已被占用，见 Q13）；B1–B13 按推荐。见[决策稿第二轮](2026-10-27-m2-exit-m3-opening-decision.md#第二轮裁决2026-10-28)。

## 已定事项（不再问，引出处）

| 项 | 已定内容 | 出处 |
|---|---|---|
| 交付形态 | 单一产品镜像，按 digest 固定，web/worker/Observer 共用；控制侧 Compose；产品不做 Helm | 决策稿 Q4 :45；C3 :33、:433；SPEC.md:87 |
| 干净机器 | 全新 Linux 主机；macOS + colima 只是开发环境 | Q4 :45；任务记录 :10 |
| 发布通道 | 新增 workflow，只在打 tag 时运行，只有它拿 `packages: write`，用 `GITHUB_TOKEN`，不新增凭据；现有 CI 保持 `contents: read`（ci.yml:9-10，red-proof.yml:18-19） | Q4 :45；SPEC.md:30 |
| 首次对外发布 | 首次推送镜像前必须先告诉用户（用户门） | Q4 :45；Q10 :68；任务记录 :10 |
| 云主机 | 下单前给出报价，用户批准后才买；干净机器取证要等批准。镜像和 Compose 可以先在本机验证 | Q3 :40-41；任务记录 :14 |
| 迁移顺序 | 所有运行时停止后才执行迁移；不支持滚动升级；运行时启动时 `verify_head` | C3 :417；Makefile:20-25；web/__main__.py:202-203；worker_main.py:540-541；observer/__main__.py:155 |
| 合并授权 | F9 实施 PR 在 CI 绿、`CLEAN`、独立审查已处置、真实运行证据通过时由 Agent 合并；花钱、扩权限或数据出口、改 SPEC/C3、首次对外发布仍归用户门 | Q10 :68；SPEC.md:30 |
| 不在本步范围 | soak、staging 升级演练、Helm；F9 第 2–5 步 | 任务记录 :10；收口记录 2026-10-27-m3-closure.md:14 |

## 9 类必答逐项

1. 时间：产品代码只用数据库时钟（pyproject.toml:41-45 用 ruff 禁掉 `datetime.now`），容器时区不影响判定。本步不新增时间语义，**已定**。镜像构建时间与 tag 时间只作溯源元数据，见 B9。
2. 计数与口径：一个镜像对应多个角色；每个服务的副本数见 B3；版本号口径见 A2；连接池与 PG 连接数见 B6。
3. 状态转移：首次安装、重复执行安装、升级、卸载的状态与顺序见 A6、B4、B5。
4. 主体与权限：四个运行时 login（web/worker/observer/retention）都由安装器创建（迁移只建 NOLOGIN 组，0011:16-18、0003:35-36、0023:29；实验库脚本只建 web/worker 两个、trust 免密，scripts/m0/postgres_lab.py:143-150）。工作台端口暴露范围见 A4，镜像拉取凭据见 A3，发布权限见 A5，PG 角色见 B2。已知偏离：Tool Gateway 没有独立进程，worker 进程内持有目标只读凭据（C3 :56,60 对照 0011:28-30、#197），本步照现状打包、不扩权，也不在本步修。
5. 数据合同：本步不改 schema。PG 镜像固定为 CI 已用的 `postgres@sha256:2a0d0f…`（17.9，ci.yml:77-78），数据放命名卷。迁移用一次性 `migrate` 服务（owner DSN）。全新库走 `upgraded` 路径，用不到 pg_dump（schema.py:525-532），所以镜像不需要带 pg_dump。见 B2、B4。
6. 旧数据与已归档证据：安装的是全新库，不接管本机 55431 实验库，已归档证据不动。版本号从 `0.0.0` 改到新值会同时改 uv.lock:625（`make check` 里的 `uv lock --check` 会检查）。仓库里没有测试钉住 `opspilot` 的版本号（`0.0.0` 的 5 处命中都是无关 IP 字面量，或在 mock 第三方包版本：tests/test_m0_live.py:391）。**无待决**，探针佐证。
7. 并发与水位：worker 容量默认 2，要求 `OPSPILOT_POOL_MAX_SIZE` ≥ 容量 + 2（worker_main.py:38-39；默认 max_size=4，persistence/base.py:43-44）。web 自带自动观察线程（web/__main__.py:27-45,250-257），开多个副本会让三个 pass 并跑。见 B3、B6。
8. 外部调用：worker 调 DeepSeek（运行时已常设授权）；trace 默认 off，lab 判定要求 Prometheus/Jaeger 地址是回环或 `*.svc`（tracing.py:141-169,185-188）。所以两台主机分开部署时 trace 会按设计 fail-closed，这归 F8/#243，本步不改。构建与安装要从 Docker Hub、GHCR、PyPI 拉东西，本机网络今天就断过一次（fetch SSL 失败），见 B10。
9. 验收投影与 `passes`：本步不翻 F9 的 `passes`。第 1 步原文要求「从干净机器……搭建真实软件环境并接上 Agent」，需要云主机和跨主机接线，见 A1、A4。本步的证据和独立验收测试内容见 B11。

## 需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| A1 | 本步（第一个 PR）做到哪里算完？ | (a) 只做镜像 + Compose + 版本号 + workflow，在本机 Linux VM（colima/docker）上把 Compose 安装跑通并留开发证据；干净云主机取证和跨主机接上目标留到后续步骤，各自另做预审。代价：第 1 步验收要等 Q3 批准之后才能过。(b) 本步同时完成干净主机取证。代价：被 Q3 报价卡住，PR 会长时间开着。 | (a)。任务记录 :14 已允许先在本机开发验证；跨主机接线牵涉 A4 的网络边界，单独预审更稳 | 任务拆分；F9 第 1 步完成时间 | |
| A2 | 版本号方案与唯一来源 | (a) SemVer，首版 `0.1.0`，tag 写 `v0.1.0`；唯一来源是 pyproject `version`，同步改 uv.lock:625，加 `opspilot.__version__` 并用测试保证两者一致；workflow 校验 tag 等于 pyproject 版本，不一致就失败。(b) 用 CalVer（例如 `2026.10.0`）。(c) 用 git describe 动态生成。代价：项目是 `package=false`（pyproject.toml:32），运行时拿不到 `importlib.metadata` 版本，动态方案要额外打包。 | (a)。M3 前 `0.x`，作品集验收后再定 `1.0.0` | 对外可见的版本标识，发布后不宜改；溯源 | |
| A3 | 干净主机从哪里拿镜像？（决定要不要新增凭据，以及要不要不可逆地公开） | (a) GHCR 包设为 public，主机匿名拉取。代价：GitHub 文档原文是首次发布默认 private，包「inherits the access permissions (but not the visibility)」，而且「Once you make a package public, you cannot make it private again」，属于**不可逆**。(b) 包保持 private，在云主机上放一个只有 `read:packages` 的 PAT。代价：新增一个长期凭据，要管理和撤销。(c) 安装器在目标主机上从 tag 对应的源码本地 `docker build`，按本地 image ID / digest 固定；GHCR 只做发布展示。代价：构建可复现性要靠锁文件和基础镜像 digest 保证，安装时间更长，而且「拉到的镜像与 CI 发布的是同一个 digest」这一点不成立。 | (a)。仓库本身已经公开（AGENTS.md「仓库公开」），镜像里只有源码与依赖、不含凭据；求职展示需要可以匿名拉取。公开前先确认镜像里没有 `.env`、`tmp/` 或证据私有目录（见 B8 的 .dockerignore）。不同意公开就选 (c) | 对外发布、不可逆；凭据面 | |
| A4 | 工作台端口在控制主机上暴露到什么范围？（Alertmanager 在目标主机上，需要能回调） | 现状：web 是明文 HTTP，默认 `127.0.0.1:8080`，`proxy_headers=False`，文档写的是「behind a trusted proxy」（web/__main__.py:1,191,259）；lab 的 Alertmanager 写死 colima 地址 `192.168.5.2:8080`（scripts/kind_lab/alertmanager-values.yaml:43）；目标侧 Prometheus/Jaeger/frontend 只监听 127.0.0.1（kind-config.yaml:15-24）。(a) 本步 Compose 只发布 `127.0.0.1:8080`，跨主机通道（SSH 隧道、WireGuard、或带 TLS 的反向代理）在接线步骤另做预审。(b) 本步直接发布 `0.0.0.0:8080`。代价：bearer token 和登录口令经公网明文传输，属于网络边界变化。(c) 本步就加 TLS 反向代理服务。代价：多一个组件和证书管理。 | (a)。容器内 bind `0.0.0.0:8080`，宿主侧只发布 `127.0.0.1:8080`，同时必须显式设 `OPSPILOT_ALLOWED_ORIGINS`（默认值会变成 `http://0.0.0.0:8080`，web/__main__.py:195）；跨主机方案推荐「隧道/私网，不开公网端口」，到时再裁 | 网络边界、凭据传输；PRODUCT-CONSTRAINTS 第 13 行对网络边界的态度 | |
| A5 | 第一次之后的 tag 由谁推？ | 首次发布已定为用户门（Q4/Q10）。(a) 首次之后，M3 期间 Agent 可以在 main 绿灯时自行推版本 tag 触发发布，事后简报。(b) 每次推 tag 都先问用户。代价：用户多一个门，M3 升级演练需要多个版本时会被卡住。 | (a)，期限到 M3 宣告完成为止，与 Q10 授权期限一致；只能从 main 上的提交打 tag，不删除、不覆盖已发布的 tag | 对外发布授权；全局规则里 release 需要明确授权 | |
| A6 | 卸载或重装时 PG 数据卷怎么处理 | (a) 卸载默认保留命名卷和密钥目录；删除数据必须显式加 `--purge-data` 并二次确认。(b) 卸载即 `down -v`。代价：误删事故与证据，违背 C3 :445「不能擅自删除唯一数据」。 | (a) | 数据安全，不可逆 | |

## 可逆默认（Agent 自定，一行理由）

| 编号 | 问题 | 默认 | 理由 / 出处 | 影响面 |
|---|---|---|---|---|
| B1 | 镜像构建方式 | 多阶段构建：基础镜像 `python:3.12-slim` 按 digest 固定；用固定版本的 uv（与 CI 的 0.10.8 一致，ci.yml:30）执行 `uv sync --locked --no-dev`，只装 m0 + web 组；入口按角色分：`python -m opspilot.web serve` / `opspilot.worker_main` / `opspilot.observer` / `opspilot.schema migrate` / `opspilot.retention`；以非 root 用户运行 | 默认组含 dev（pyproject.toml:33），不能进镜像；运行时依赖都在 m0/web 组（:9-25） | 镜像大小、攻击面 |
| B2 | PG 角色与网络 | 容器集群的 superuser 只给 `migrate` 和 `init-roles` 一次性服务用（迁移要 CREATE ROLE，0011:296、0023:215）；四个 login 都是 `LOGIN INHERIT`、只属于各自的组、用 SCRAM 口令，沿用 postgres_lab.py:183 的冲突校验写法；按 0003 文档建议 REVOKE TEMP（docs/development.md:37）；PG 只在 Compose 内网，不发布端口 | 与现有角色合同一致，不扩权 | 权限隔离 |
| B3 | 副本数 | web 1、worker 1（capacity 2）、observer 1；文档写明 web 不要多副本 | 多个 web 副本会让自动观察 pass 并跑（web/__main__.py:250-257）；worker 和 observer 靠租约可以多开（worker_main.py:17-19），但本步不需要 | 并发 |
| B4 | 安装与升级顺序 | `install.sh` 依次执行：生成缺失的密钥 → 起 PG → `migrate`（一次性）→ `init-roles` → 起运行时。升级必须先 `stop web worker observer`，再迁移，再 `up`，不靠 `depends_on` 隐式排序 | 照搬 C3 :417。Compose 重建服务时，迁移容器会在旧运行时还没停的时候先跑 | 迁移安全 |
| B5 | 重复执行安装（幂等） | 已存在的口令和密钥文件不重新生成；迁移已在 head 就输出 `unchanged`（schema.py:529-530）；login 已存在且属性正确就复用，属性不对就拒绝，不去修改 | 重新生成口令会把已有 login 锁在外面 | 状态 |
| B6 | 连接池与 PG 连接数 | 显式设 `OPSPILOT_POOL_MAX_SIZE`（worker ≥ 4），PG `max_connections` 留够余量（官方镜像默认值足够），在 compose 里写出来 | worker_main.py:38-39,506；persistence/base.py:39-44 | 启动拒绝 |
| B7 | 密钥怎么交给进程 | Compose 用文件型 secrets，每个服务只挂自己的文件（worker：模型 key 和调查侧 Prom 凭据；observer：Observer 的 Prom 凭据；web：只有 DSN 口令）。DSN 里不写口令，改用 libpq `passfile=`；worker 的模型 key 和 LangSmith key 走现有 `OPSPILOT_ENV_FILE`（worker_main.py:393-401,603-608），Observer 走 `OPSPILOT_OBSERVER_ENV_FILE`（observer/__main__.py:67-73）；调查侧 `OPSPILOT_OTEL_PROMETHEUS_*` 现在只从环境变量读（otel_demo.py:1139-1147），由镜像入口脚本从 secret 文件导入环境 | 不改产品代码；凭据导入环境后，trace 的精确值擦除仍然覆盖它们（tracing.py:131-132,227）；`docker inspect` 看不到口令 | 凭据面（实现方式，不扩大授权） |
| B8 | 密钥目录与 .dockerignore | 默认密钥目录放在仓库之外（例如 `/etc/opspilot` 或 `~/.config/opspilot`，权限 600/700，属主对齐容器 uid）；`.dockerignore` 排除 `.env*`、`tmp/`、`docs/evidence/`、`.venv`、`.git`、`.playwright-mcp` | 现有 .gitignore 没有 secrets 目录（.gitignore:6-11）；仓库根目录确实有 `.env`、`.env.judge` | 防泄露 |
| B9 | 镜像标签与溯源 | 推送 `ghcr.io/<owner 小写>/opspilot:<version>`，不推 `latest`；写 OCI labels（source、revision、version）；Compose 和安装记录写 `image@sha256:<digest>`；发布后用一个文档 PR 把 digest 写进 `releases/<version>.json`（属于预授权的证据/文档类） | GHCR 名称必须全小写（仓库 owner 是 `kevinWangSheng`）；repo 里的 compose 不可能事先写上自身所在提交构建出的 digest | 溯源 |
| B10 | workflow 细节 | 只用 `on: push: tags: ['v*']`，不响应 `pull_request`；job 级 `permissions: contents: read, packages: write`；actions 按 SHA 固定（照 ci.yml:23,28）；构建前跑一遍 `make check`，并校验 tag 提交在 `origin/main` 上；不用 attest-build-provenance（它要 `id-token`/`attestations: write`，超出 Q4），用 build-push-action 自带的 min provenance；平台出 linux/amd64 + linux/arm64 | 守住 Q4 的权限边界；arm64 让 Mac 开发机也能拉取 | CI 权限 |
| B11 | 本步的证据与独立验收测试 | 不翻 `passes`。独立测试作者写静态合同测试：除发布 workflow 外，所有 workflow 都没有 `packages: write`；compose 里镜像引用都带 digest；compose/镜像里没有明文凭据；迁移排在运行时之前；tag 和 pyproject 版本一致；`.dockerignore` 覆盖敏感路径。真实证据：在本机 Linux VM 上 `install.sh` 从零跑通，三个运行时都过了 `verify_head`，fixture profile 跑通一次真实模型 Run 到终态并截取 web 页面。本步不碰调查 loop、报告或恢复路径，不需要 LangSmith trace | AGENTS.md「验证与汇报」；Q10「真实运行证据」 | 验收 |
| B12 | 发布校准清单写死了开发集群的 `cluster_uid` | 本步只记录、不改：`release-calibrations.json:12` 写死了 `c8001471-…`，而且路径打包在代码里（release_policy.py:45-47），新的 kind 集群会对不上，发布自动授权会全部被拒。开一个 issue 挂到 F9 接线/收口，到时再预审（属于数据合同变更，到时可能要用户裁决） | F9 第 2 步不含发布观察，但 F8/F9 收口时的升级演练可能用到 | 后续风险 |
| B13 | 过时文案 | `.env.example:1-21` 里的 M0 预算审批措辞、README「Implementation has not started」、web/worker docstring 里的「not a deployment artifact」（web/__main__.py:50，worker_main.py:55-56），随本步或 README 重写一起更新 | 不改行为 | 文档 |

## 全量探针

- worktree：`/Users/shenghuikevin/dev/AI/poa-prereview-f9`，基于 origin/main `db5360c3`，分支 `prereview/f9-probe`
- 草稿补丁：pyproject `version` 改为 `0.1.0`，uv.lock:625 同步，`opspilot/__init__.py` 加 `__version__ = "0.1.0"`；`uv lock --check --offline` 通过
- 命令：`/Users/shenghuikevin/dev/AI/production-ops-agent/.venv/bin/python -m pytest tests/acceptance -q -p no:cacheprovider`
- 结果：**探针 0 失败**（2791 passed, 299 skipped, 412.74s）。skipped 都是需要 PG opt-in 的用例（`M1_DURABLE_POSTGRES=1`），这次没有起 PG
- 当时宿主 swap 已用 8713/9216 MB（另有两条预审线在并行），这不影响结论
- 清理：`git status --ignored` 显示只有草稿补丁和 `__pycache__`；已 `git worktree remove --force` 并删除分支
