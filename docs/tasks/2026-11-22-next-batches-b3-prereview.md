# 批次 3 部署健壮性：开工前合同预审（2026-11-22）

- 预审者：独立 Agent，未参与实现，只读，没有改仓库、没开 PR、没评论 issue。对照 `origin/main` `7ebb04fa`（`git fetch` 后核实）。
- 读过：共用任务书 `opspilot-next-batches-brief-2026-11-22.md`、AGENTS.md、PRODUCT-CONSTRAINTS.md、`docs/tasks/README.md`「合同预审」、C3 §4（`technical-proposal-2026-09-07.md:106`、`:205`）、§12（`:338`）、§13（`:491-493`）、F8 合同 `docs/tasks/2026-10-27-m3-f8-operability-contract.md:24`（F8-R4）；九个 issue 的正文与评论（`gh issue view`）。代码：`opspilot/persistence/{budgets,runs,outbox,grants}.py`、`opspilot/investigation/{store,client}.py`、`opspilot/observer/{loop,__main__}.py`、`opspilot/observation/{store,release_policy}.py`、`opspilot/health.py`、`opspilot/tools/http_deadline.py`、`opspilot/web/{__main__,service}.py`、`opspilot/web/templates/incident.html`、`opspilot/release_intake.py`、`deploy/compose/{compose.yaml,install.sh}`、`deploy/docker-entrypoint.sh`、`scripts/soak/compose.soak.yaml`、`scripts/kind_lab.py:68-95`、`docs/development.md:163,199-200`、`docs/portfolio/limitations.md`；测试 `tests/acceptance/test_413_health_contract.py`、`tests/acceptance/test_392_{compose,install}_contract.py`。
- 标注：【核实】读过代码或跑过；【推断】由代码推出，没有在实例上验证；【未确认】没核对。
- 全量探针：**没跑**。本机 swap 两次读数 16.37/17.41 GB、16.05/17.41 GB 已用（`sysctl vm.swapusage`），超过任务书 10 GB 门限，按任务书不跑 pytest。改为 grep 钉值文件加一次共享函数实测（#443，见 B3-R11）。钉值预期：
  - `tests/acceptance/test_413_health_contract.py:100-186` 用关键字参数调 `poll_readiness`；新参数带默认值时不变【推断】。
  - `tests/acceptance/test_392_compose_contract.py:145-172` 只要求 web 至少 3 个 secret，且每个 secret 的文件路径含 `OPSPILOT_SECRETS_DIR`，第 4 个 secret 照此写法能过【核实：读断言】。
  - `tests/acceptance/test_392_install_contract.py:146-175` 要求重跑 install 时所有 secret 文件字节不变、权限 0600；`write_once` 满足这两条【核实：读 `install.sh:289-299`】。
  - 无测试断言 `logging:`、预留 `state`、`budget_reserved` 的具体值；`model_requests_used` 是三列之和（`budgets.py:301-303`），#527 的改动不改变它【核实：grep `tests/`】。
  - 预期探针 0 失败【推断】。开工后用 Draft PR 交 CI 跑 `tests/acceptance`。
- **issue 前提已过时的两处**：
  1. #683 说要放宽 `tests/acceptance/test_393_compose_contract.py:53`，这个文件已在 #714（`4f8bf5c0`）删除，现行断言只要求「≥3」，所以不需要改验收断言【核实】。它仍属凭据变更，见 B3-U2。
  2. #443 说 outbox「记原值」，实测非法 revision 在 outbox 里记的是 `UNCLASSIFIED`，不是原值；真正的不一致在 B3-R11【核实】。

## 需用户裁决（3 项）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| B3-U1（#522） | **Observer 正在采样时 `/readyz` 算不算就绪**。现状：Observer 只在一整轮跑完后才记一次「成功轮询」（`observer/loop.py:289-296`、`observer/__main__.py:179-189`），超过 3×5 秒没有新记录就返回 503 `POLL_STALE`（`health.py:98-116`，合同 F8-R4「上次成功轮询 ≤ 3×poll 间隔」）。一次采样要发 24 个 Prometheus 请求，约 20–25 秒，所以采样期间 `/readyz` 会掉成 503，soak 里观察到 1.4%–7.9% 的 tick。Compose healthcheck 要连续 12 次失败（60 秒）才判 unhealthy（`compose.yaml:184-189`），所以目前只是指标和 soak 可用率不好看，不会触发重启 | **a. 采样进行中也算活着，以这次采样的租约为上限**：领到一个采样任务时记下开始时刻，采样期间 `/readyz` 返回 200，只要离开始不超过租约 120 秒（`observation/store.py:76`，采样器保证每次采样在租约内结束，`observer/loop.py:68-70`）；超过 120 秒还没结束，或者两次采样之间主循环卡住超过 15 秒，照旧 503。代价：F8-R4 改一行字；`poll_readiness` 加一个带默认值的参数。**b. 把 Observer 的过期窗口直接放宽到 120 秒**：改动最小，但主循环在采样之外卡住时也要 120 秒才报出来（现在 15 秒）。**c. 不改**，在运维文档写明「采样期间 readyz 会短暂 503」 | **a**。F8-R4 后面加一句：「Observer 领到采样任务后到提交之前也算存活，以该任务的租约（120 秒）为上限。」`/metrics` 的 `last_poll_age` 含义不变（仍是上次整轮的年龄） | F8 合同记录一行；`health.py`、`observer/{loop,__main__}.py`；worker 不变（worker 的轮询线程和执行槽本来就分开，`development.md:163`） | |
| B3-U2（#683） | **安装时要不要生成一份固定的 web 会话密钥**。现状：Compose 部署没设 `OPSPILOT_SESSION_SECRET`，web 每次启动随机生成一个（`web/__main__.py:143-155`），所以每次 web 重启或 `install.sh upgrade` 后，所有浏览器都要重新登录。这是新增一份凭据文件（AGENTS 把凭据变更列为用户门） | **a. 生成固定密钥**：`install.sh` 用 `write_once` 生成 `web_session_secret`（64 位十六进制随机数，0600，与其他 secret 在同一目录），Compose 只挂给 web，经 entrypoint 的 `_FILE` 机制读入（`docker-entrypoint.sh:9-26`，加一个变量名）。**不放进「已有数据库时必须存在」的清单**（`install.sh:326-341`），像 `target_identities.json` 那样（`install.sh:358-361`）在老安装下次 upgrade 时补生成，否则老安装 upgrade 会被拒。效果：web 重启和升级后登录保持；第一次升级到这个版本时还会退出一次；要轮换时删掉这个文件再 upgrade，所有人退出。密钥只在 secrets 目录和 web 容器里，不进日志、证据或模型输入。**b. 不改**，在 README 写明重启会退出登录，需要的人自己设环境变量 | **a**。验收断言不用改（见上文「前提过时」第 1 条） | `install.sh`、`compose.yaml`、`docker-entrypoint.sh`、UPGRADE.md 一段；PR 属凭据类用户门 | |
| B3-U3（#476） | **`compose.yaml` 要不要自带日志轮转**。现状：产品 `compose.yaml` 不设 `logging:`，容器用 Docker 守护进程的默认值；没配 `daemon.json` 的主机默认是 json-file、不限大小，日志会一直涨到占满磁盘（soak 是用 `scripts/soak/compose.soak.yaml` 给每个服务单独加的 100m×10，产品文件没变，`compose.soak.yaml:22-42`）。`install.sh` 只认 `compose.yaml`，不接受额外的 override 文件（`install.sh:58`、`:281`） | **a. 默认带轮转**：一个 `x-logging` 锚点给全部 7 个服务，`json-file`，`max-size: 50m`、`max-file: 5`（每个容器最多约 250 MB，全部约 1.75 GB）。代价：会覆盖操作者在守护进程上配的别的日志驱动（例如 journald、Loki 插件）；这种情况要改 `compose.yaml`。**b. 不带**，在 README/UPGRADE.md 写一段 `daemon.json` 示例，让操作者自己配。磁盘风险留给操作者。**c. 驱动和大小做成 install.env 里可改的设置**：灵活，但 `max-size` 这类选项只对 json-file/local 有效，换成 journald 时选项非法会导致容器起不来，要多一层校验 | **a**，README 写一句「要用自己的日志驱动就删掉 `x-logging`」。大小属可逆默认，按 soak 经验可调 | `compose.yaml`、README 一句；soak 覆盖文件不动 | |

## 必答类别（docs/tasks/README.md 合同预审）

| 类 | 状态 |
|---|---|
| 1 时间 | #527：结算在 `claim()` 的同一事务里用数据库时钟（`runs.py:185`），不新增时间点。#522：只用 Observer 进程的 `time.monotonic()`（`observer/__main__.py:189`），上限取租约 120 秒，见 B3-U1。#444：DNS 等待以本次请求剩余期限为界（`http_deadline.py:39-66`、`client.py:260-261`），不新增容差。其余项不涉及时间 |
| 2 计数与口径 | #527：模型请求总数 = `budget_reserved + budget_spent + budget_unknown`（`budgets.py:301-303`），把遗留预留转成 `unknown` 只是从第一列挪到第三列，总数和「已用秒数」（`COALESCE(seconds,reserved_seconds)`，`:293`）都不变；C3 §13「未知费用保持占用」（`technical-proposal:491`）照旧成立。事故页「x spent / y reserved / z unknown」（`incident.html:254`）会显示为 reserved 减、unknown 增，总数不变。其余项不涉及计数 |
| 3 状态转移 | #527：只在 `claim()` 真正换新 epoch 的分支（`runs.py:221-235`）结算本 Run 所有 `state='reserved'` 的行。此时旧租约已过期，旧 attempt 的 `settle_budget` 一定被拦（`budgets.py:119-128`），所以这些行一定是遗留的【推断，由栅栏条件推出】。版本不符改 `blocked` 的分支、人工取消、超时、终态都不动（B3-R2）。#522：状态只有「整轮完成」与「采样中」两种，见 B3-U1 |
| 4 主体与权限 | #527：`claim()` 由 worker 角色执行；worker 对预留表已有 `UPDATE(seconds,state)`（`grants.py:292-296`），对 Run 表已有 `UPDATE(budget_reserved,budget_spent,budget_unknown,…)`（`grants.py:397`），**不扩权、不需要迁移**【核实】。#683：新密钥只挂给 web 容器（B3-U2）。其余项不涉及权限 |
| 5 数据合同 | 不改表、不做迁移（#527 用现有列；其他项不碰库）。部署合同变化：#683 新增一个 secret 文件、一个 `_FILE` 变量；#476 新增 `logging:`。部署顺序：同一个镜像加同一个 `compose.yaml`，`install.sh upgrade` 一次完成 |
| 6 旧数据兼容 | #527：已有的遗留行（如 soak 数据库里那一行）不回填；只有该 Run 再被认领时才结算，已结束的 Run 保持原样（B3-R3）。已归档证据（soak 报告里「readyz 采样时下降」的描述、F8 证据）不改，照实保留。#683：老安装 upgrade 时补生成密钥，不拒绝（B3-U2）。#443：已写入的 outbox 行不改 |
| 7 并发与水位 | #527：`claim()` 已按事故→Run 的顺序加行锁（`runs.py:178-182`），结算放在这把锁里，与 `settle_budget`、`reserve_budget` 同序，不新增死锁路径【推断】。#444：DNS 并发上限是固定槽位数（B3-R8）。#522：只有采样线程写、健康线程读两个浮点数，沿用现有的无锁读法（`observer/__main__.py:170-189`） |
| 8 外部调用 | #444：DNS 卡住时，请求在剩余期限内以 `TimeoutError` 结束，走现有的 `OSError` 分支变成 `MODEL_UNAVAILABLE`（`client.py:320-329`），预留按 `unknown` 结算，重试规则不变。#522：Prometheus 请求次数不变。其余项没有外部调用 |
| 9 验收投影 | F6、F8、F9、F12 的 `passes` 都已经是 true（`feature_list.json`），本批不翻任何 `passes`，不改验收步骤【核实】。F8 第 4 步（72h soak）证据照旧有效，不重跑 |

## 可逆默认（实现者自定，17 项）

| 编号 | 问题 | 默认 | 依据 |
|---|---|---|---|
| B3-R1（#527） | 在哪里结算遗留预留 | `claim()` 换新 epoch 的分支里、同一事务内执行：`UPDATE opspilot_budget_reservations SET state='unknown', seconds=COALESCE(seconds,reserved_seconds) WHERE run_id=%s AND state='reserved' RETURNING amount`，按返回的金额总和把 `budget_reserved` 挪到 `budget_unknown`。同时更新 `settle_budget` 文档注释里「a revoked attempt leaves its reservation as reserved」（`budgets.py:97-98`） | issue 正文「Expected」；`unknown` 的含义与客户端调用失败且无用量时一致（`budgets.py:91-94`） |
| B3-R2（#527） | 范围 | 只处理接管这一条路径。人工取消、超时、版本不符改 `blocked`、Run 终态留下的预留不处理：它们不影响后续 Run 的预算（预算按 Run 算），只是账面上还挂着 reserved | 主路径优先（M3 后方向）；issue 只描述了接管 |
| B3-R3（#527） | 旧行回填 | 不做迁移回填 | 不影响结果和计数（B3 类别 2）；回填需要迁移，代价大于收益 |
| B3-R4（#527） | 测试 | 一个 PG 集成测试：预留后让租约过期，再 `claim()`，断言旧行变成 `unknown`、`seconds = reserved_seconds`、三列之和不变、旧租约的 `settle_budget` 返回 `CONTROL_DENIED`。由未参与实现的新 Opus Agent 编写 | AGENTS「验收与合同测试由独立 Agent 写」；测试少而有用 |
| B3-R5（#527） | 真实运行证据 | 触及恢复路径，按 AGENTS 附一次有界真实 Run：本地 Compose 加真实 DeepSeek，在模型调用进行中 `docker kill` worker 容器（复现 soak 的 A1 故障），等接管后 Run 跑完；附 LangSmith trace、`summary.json`，再用 psql 查出该 Run 所有预留行的状态（不留 `reserved`） | AGENTS「验证与汇报」；ADR-0006 |
| B3-R6（#522） | 实现方式 | `ObserverLoop` 在领到任务后、`sample()` 前后各调一个可选回调（与 `on_poll` 同款，失败只记日志）；`PollRecord` 记 `sampling_since`；`poll_readiness` 加带默认值 `None` 的 `in_progress_since` 与 `in_progress_bound` 参数 | 不改现有测试的调用方式（`test_413_health_contract.py:100-186`） |
| B3-R7（#522） | 证据 | 定向测试：用一个每次请求都慢的假 Prometheus，让一次采样耗时超过 15 秒，断言采样中 `/readyz` 为 200、超过租约上限为 503。不需要 kind 实验。Observer 不属于调查循环、报告校验或恢复路径，不要求真实 Run trace | AGENTS 真实 Run 要求的适用范围；kind 实验会和并行实施线抢内存 |
| B3-R8（#444） | 实现方式 | 模型客户端的主机名解析走 `http_deadline.resolve` 同款有界解析（信号量计在途、剩余期限内等槽），但**用自己的 2 个槽，不和工具查询共用**：否则某个数据源主机名卡住时，模型调用也跟着排队，Run 以 `MODEL_UNAVAILABLE` 失败。做法：`resolve` 加一个槽位参数；`_RecordingHTTPSHandler._connect` 给连接设 `_create_connection`，期限由 `_post` 传入。每请求一个 `ThreadPoolExecutor` 的写法保留：DNS 有界之后，传输线程在期限内一定会结束 | issue「建议」；#290 Q9 的做法（`http_deadline.py:20-66`） |
| B3-R9（#444） | 测试 | 一个定向测试：让 `getaddrinfo` 一直阻塞，连发若干次请求，断言每次都在预算内返回 `MODEL_UNAVAILABLE`，且存活的解析线程不超过 2 个 | issue「实施时补一个 DNS 卡住的定向测试」 |
| B3-R10（#444） | 真实运行证据 | 模型客户端属于调查循环的传输层，附一次有界真实 Run（正常网络即可）的 trace 与 `summary.json`，证明正常路径没被破坏 | AGENTS；ADR-0006 |
| B3-R11（#443） | 三处 revision 怎么对齐 | 实测（`OPSPILOT_REVISION` 取四个值调 `health._revision()` 与 `build_payload(image_fields())`）：`abc123`→两边都是 `abc123`；`a b`→health 记 `unknown`、outbox 记 `UNCLASSIFIED`；`-lead`→health 原样保留 `-lead`、outbox 记 `UNCLASSIFIED`；空→两边都是 `unknown`【核实】。原因：health 的规则 `[0-9A-Za-z._-]{1,64}`（`health.py:84`）比 outbox 的 `IDENTIFIER`（首字符必须是字母或数字，`outbox.py:112`，迁移 0024 冻结了一份副本 `0024_trace_outbox.py:78`）宽。默认：`image_fields()` 改用 `health._revision()`，**同时把 health 的规则收紧到与 `IDENTIFIER` 一致**（不能动 outbox 那边，迁移里冻结着）。之后三处对任何值都给出同一结果；以标点开头的 revision 在 `/metrics` 和日志里改记 `unknown`。补一个参数化单测 | issue「建议」；合法值不受影响 |
| B3-R12（#683） | 细节 | 文件名 `web_session_secret`，变量 `OPSPILOT_SESSION_SECRET_FILE`，加进 entrypoint 的 `_FILE` 循环（`docker-entrypoint.sh:9-11`）；内容是 `random_hex` 生成的 64 个字符（≥16 字节的下限满足，`web/__main__.py:153`）；开发入口（`development.md:127`）不变 | 沿用现有 `OPSPILOT_UI_USERS_FILE` 写法 |
| B3-R13（#683） | 证据 | 本地 `install.sh install` 后登录，`docker compose restart web`，同一个 cookie 仍然有效（curl 返回 200，不跳到登录页）；再用一份没有该文件的老 secrets 目录跑 `upgrade`，确认文件被补生成且 upgrade 成功。不涉及调查循环，不要求真实 Run | 部署类变更的实际验证 |
| B3-R14（#476） | 大小与范围 | 50m×5，7 个服务全部加上（含 `migrate`、`init-roles` 这类一次性服务，保持一致）；`docker compose config` 校验；soak 覆盖文件不动（它后合并，100m×10 仍然生效） | B3-U3 a；soak 的 100m×10 在 72h 内足够（#476 评论） |
| B3-R15（#391） | 文档写什么 | `docs/portfolio/limitations.md` 加一行：发布观察的校准清单把目标钉在开发 kind 集群的 `cluster_uid`（`release-calibrations.json:12`，路径写死在 `release_policy.py:45-47`），换任何新集群（租机、staging）都不会自动授权发布观察，需要按 #391 另做合同预审。#391 保持 OPEN。佐证：租机安装脚本写入的目标身份 `cluster_uid` 是 `opspilot-m1`（`development.md:200`），与清单不一致；拒绝时的具体错误码【未确认，没跑】 | 照实披露；只记录现状，不改行为 |
| B3-R16（#373） | C3 怎么改 | §4 `technical-proposal:106` 的「拆分尚未实现（#373）」改为「拆分不做，作为已知偏离（用户 2026-11-22 决定，#373）；改目标与合并保留」；§表 `:205` 那一行的「拆分」同样加注。内容已经裁决，只是落字 | 任务书「已裁决」；#373 评论 |
| B3-R17（#485） | 文档写什么 | `docs/development.md` kind 实验小节（`:199-200` 附近）加一句：「同机多个 lab（`OPSPILOT_LAB_INSTANCE`，`kind_lab.py:70-95`）时，每个实例的控制侧要用 install.sh 设独立的 `OPSPILOT_PROJECT` 和 `OPSPILOT_WEB_HOST_PORT`（`install.sh:23-25`），再用 `connect-control --project <该项目>` 以该实例的别名接入。」 | issue 原文提案 |

## PR 拆分、顺序与合并类别

一个 PR 对应一个 issue，都以 `main` 为目标，不做 stacked PR；改同一文件的（P5、P6 都改 `compose.yaml`）按顺序先合前一个，后一个 `merge origin/main`。

| 顺序 | PR | 内容 | 需要的证据 | 类别 |
|---|---|---|---|---|
| 1 | `fix/443-outbox-revision` | B3-R11 | 单测 | 缺陷修复，不改合同，预授权 |
| 2 | `fix/444-model-client-dns` | B3-R8/R9 | 定向测试 + 一次有界真实 Run 的 trace 与 `summary.json` | 缺陷修复，触及调查循环传输层；不扩权、不改合同，按「功能 PR 审查干净由 Agent 合并」处理，合并前简报 |
| 3 | `fix/527-orphan-reservations` | B3-R1..R5 | PG 集成测试 + kill worker 的真实 Run（trace、`summary.json`、预留行查询结果） | AGENTS 把「状态恢复」列为用户门；不扩权、不迁移。建议审查干净后由 Agent 合并并简报 |
| 4 | `fix/522-observer-readyz` | B3-U1 + B3-R6/R7，F8 合同记录加一行 | 定向测试 | 合同措辞（F8-R4）变更，用户门；B3-U1 裁决后合同已定，可由 Agent 合并 |
| 5 | `feat/683-session-secret` | B3-U2 + B3-R12/R13 | 本地 install/restart/upgrade 实测 | 凭据类，用户门 |
| 6 | `chore/476-compose-logging` | B3-U3 + B3-R14 | `docker compose config` + 本地 install 后 `docker inspect` 看到 LogConfig | 部署默认值；按 B3-U3 裁决后预授权 |
| 7 | `docs/391-485-limitations-runbook` | B3-R15、B3-R17 | 无 | 文档，预授权，不派审查 |
| 8 | `docs/373-c3-split-deviation` | B3-R16 | 无 | 改 C3，按 AGENTS 属用户门；内容已裁决，建议简报后由 Agent 合并 |

每个代码 PR 完成前由未参与实现的新 Opus Agent 做一轮独立审查；P2、P3、P4 的合同测试由另一个新 Opus Agent 按本表与公开接口编写，不给实现代码。

## 建议移出或去掉

- 不需要移到其他批次的项。
- #683 的「验收断言要放宽」这个前提已经不存在，PR 正文要写明这一点，避免审查者去找已删除的 `test_393`。
- #443 的 issue 描述（「outbox 记原值」）与实测不符，实现 PR 里照实更正。
- #391 本批只做文档披露；真正的校准与集群绑定方式（数据合同变更）不在本批，issue 保持 OPEN。
