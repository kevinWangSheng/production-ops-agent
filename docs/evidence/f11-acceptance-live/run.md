# F11 PR-5c kind 验收窗口：三个发布全走产品路径（批次 `f11-5c-live`）

- 日期：2026-10-26（UTC 04:53–06:00），kind-opspilot-m1 / otel-demo；代码 `e028064`（`feature/F11-acceptance` = main `a558f6f` + F11 四步独立验收测试 + 发布页时间按 UTC 显示的修复 `be0c789`），运行期间工作区没有改产品代码
- 合同：[PR-5 合同](../../tasks/2026-10-24-f11-pr5-contract.md) §5「kind 真实运行最小做法（U-4=A）」与文末裁决；[F11 任务记录](../../tasks/2026-10-12-f11-post-release.md) PR-5 r1（U-1..U-5）；[M2 授权](../../tasks/2026-10-24-m2-remaining.md) Q6
- 冻结摘要：每个发布一份，[A](release-a-paused-unknown/summary.json)、[C](release-c-pause-resume-healthy/summary.json)、[B](release-b-regression-degraded/summary.json)；三个产品进程的环境变量名 [process-env-names.json](process-env-names.json)；页面 HTML 的哈希与链接 [pages.json](pages.json)。原始 ledger 在主仓库 `tmp/lab-ledgers/f11-5c-{a,c,b}/` 与 `tmp/lab-ledgers/f11-5c-live/`（快照、页面控制记录、页面 HTML、silence），不入库，各文件 sha256 见摘要 `raw_ledger`
- 脚本（本次新增）：[`scripts/f11_pr5c_lab.py`](../../../scripts/f11_pr5c_lab.py)（23 类策略面快照与比对、经页面路由的 pause/resume、等待、宿主读数、环境变量名记录、余额）、[`scripts/f11_pr5c_collect.py`](../../../scripts/f11_pr5c_collect.py)（从页面 JSON、harness ledger、只读 SQL 与 `replay_session` 冻结摘要）。快照的 23 类与字段和 PR-3／PR-4b 的 `k8s_snap.py` 相同。窗口内运行的版本 sha256 为 `f0b3a33b…f6f8`（lab）与 `52613367…c739`（collect）；入库前只做了 ruff 格式化与删除一个未用变量，行为不变
- 性质：有界真实运行。发布是 F11 harness 的真实 `kubectl patch` 滚动（只改 `OPSPILOT_U2_RELEASE_ID` 环境变量）；登记走真实 `POST /intake/version-changes`（release-deployer token）；**授权由 web 进程的 `release_authorize_pass` 以 `system` 自动完成（PR-5b），没有任何直接的存储原语调用**；暂停／恢复经工作台页面路由 `POST /version-changes/{id}/control`（Basic Auth、Origin、`expected_control_generation`、幂等键）；Observer、web、worker 都从本 worktree 代码启动；故障是 flagd 真实注入；模型是真实 DeepSeek。不是生产证明；本次不改 `feature_list.json`
- 执行者：未参与 F11 实现的 Agent，只跑窗口、写证据，没有改产品代码
- **结论：三个发布的结果都与预期一致（A unknown、C healthy、B degraded），没有失败轮，没有用到 Q6 重跑。** 发布 A 的第一次 webhook 因为本次 web 启动时漏了 release-deployer 的 token 哈希被拒（401，环境前提），修正 web 环境后对同一轮次重发，见时间线

## 系统做了什么

每个发布同一条链：harness patch checkout 触发滚动（R = patch 回执）→ watch 补齐新旧 RS/Pod 身份 → 取策略面基线 → harness 以 release-deployer token 调 `POST /intake/version-changes`（201 created，状态 pending）→ web 后台线程每 30 s 一轮的 `release_authorize_pass` 在清单 `otel-demo-checkout-release@e2ed7b69b96b` 恰好匹配时以 `system` 授权（建会话，首个采样在授权后 60 s 到期）→ Observer 按冻结 policy 采样判定 → 操作员经页面暂停、恢复 → 异常时 web 的 `release_anomaly_pass` 新建事故与 Run，worker 用 v6 输入调查。

## 环境

- PostgreSQL 17.9：本 worktree 新建一次性实例（initdb，端口 55811，trust，`log_connections`、`log_line_prefix` 带 SQLSTATE），`python -m opspilot.schema migrate` 升到 head `0021_release_incident_links`；`postgres_lab runtime-roles` 建 web、worker 登录角色，另建 `opspilot_observer_lab`（IN ROLE `opspilot_observer`，收回 TEMP）。之前的 F11 会话（PR-3、PR-4b）同样各用一次性实例，PR-4b 的数据目录随其 worktree 已不存在。窗口结束后 `pg_ctl stop`，数据留在本 worktree `tmp/f11-5c-live/pg`。55561、55611 未碰
- web：`python -m opspilot.web serve`，`127.0.0.1:8080`，`env -i` 最小环境：DSN（`opspilot_web_lab`）、`OPSPILOT_TOOL_PROFILE=otel-demo`、`OPSPILOT_TARGET_IDENTITIES`（lead 核实的 `m0-otel-20260909`）、`OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=30`（产品默认值，显式写出）、`OPSPILOT_EVENT_TOKENS`（只有哈希，来自 `workbench-alertmanager.env` 与 `workbench-release-deployer.env`）、`OPSPILOT_UI_USERS`（实验账号 `f115clab`，随机口令只在仓库外私有文件）。没有 Prometheus 凭据
- worker：`python -m opspilot.worker_main`，`OPSPILOT_TRACE=lab`，Prometheus 用 investigator 账号；版本 `prompt-replay-candidate-bef70649a3a8` / `ctx-ctx-policy-v1-a30e65ea52f4` / `otel-demo-68ed2ca8f5d6`；防循环上限为产品默认
- Observer：`python -m opspilot.observer`，`env -i`，只带自己的 DSN、Prometheus URL 与 `prometheus-observer.env`
- 实验目录：worktree 的 `tmp/m1-kind-lab` 以符号链接指向主仓库同名目录，凭据没有复制进 worktree
- 端口转发：flagd 18013、Alertmanager 19093，只在发布 B 前启动，结束后停止
- trace：LangSmith 项目 `opspilot-lab-f11-acceptance-live-20261026`，运行前建立，回读 `trace_tier=longlived`
- 占用：`occupation show` 为 null 后 claim（holder `f11-5c`，batch `pr5c`，04:54:47Z），06:00Z release，回读 null
- 宿主读数：开跑前 04:53Z checkout PlaceOrder p95 95 ms、frontend span 速率 2.07/s（> 0，未重启 collector）、swap 余量 1162 MiB（≥ 512）、`ALERTS{CheckoutPlaceOrderErrorRatioHigh}` 为空、flagd 11 个 flag 全 off（ConfigMap sha256 `1ee5c025`）、无 `fault-active.json`；05:20Z p95 94 ms、swap 余量 1380 MiB；结束 06:00Z p95 91 ms、span 2.26/s、swap 余量 1668 MiB。窗口内没有跑 make check、全量 pytest 或 Codex

## 时间线（UTC）

| 时间 | 动作 | 结果 |
|---|---|---|
| 04:55:33–38 | 启动 Observer、worker；web 第一次启动（只 source 了 `workbench-alertmanager.env`） | — |
| 04:55:56 | 快照 s0（窗口前） | 308 个对象 |
| 04:56:09 | **A** 发布 R（`f11-5c-a-20261026T045608Z`）、watch；04:56:23 基线 a1 | — |
| 04:56:24 | A webhook | **401 `INVALID_CREDENTIALS`**：web 的 `OPSPILOT_EVENT_TOKENS` 缺 release-deployer 哈希（`workbench-alertmanager.env` 写于该 actor 出现之前）。产品没有登记任何东西。按 PID 停掉本次 web，加 source `workbench-release-deployer.env` 后 04:56:49 重启 |
| 04:56:57 | A webhook 重发（同一轮次、同一载荷） | 201 created，观察 `3ac2493b` |
| 04:57:19.41 | 自动授权 pass | A `authorize/applied`，actor 与 principal 都是 `system`，会话 `1c2f2094` |
| 04:57:19.77 | 页面 pause A（gen 0→1） | 200 applied，会话 `authority_revoked`，0 个采样；04:57:26 快照 a2 与 a1 无差异 |
| 04:58:13 | **C** 发布 R，watch；04:58:16 基线 c1；04:58:17 webhook | 201，观察 `d8d5cf8d` |
| 04:58:19 | 自动授权 pass | C `authorize/applied`（`system`），会话 `bcbddfdd` |
| 05:00:18、05:02:44 | C 采样 1、2 | no_data、failed（p95 查询 `NON_FINITE`：5 分钟窗内无请求，低流量） |
| 05:02:47 | 快照 c2 → 页面 pause C（gen 0→1）→ 快照 c3 | 200 applied，会话 `authority_revoked`；c2/c3 无差异 |
| 05:02:47–05:04:58 | 保持暂停 | 零采样（采样只属于已撤销的会话 1 与之后的会话 2） |
| 05:04:57–59 | 快照 c4 → 页面 resume C（gen 1→2）→ 快照 c5 | 200 applied，新会话 `35cf58cc`（authorized_by `f115clab`），deadline 不变 05:58:13；c3/c4、c4/c5 无差异 |
| 05:07–05:31 | C 会话 2 的 12 个采样 | 2 no_data，之后 10 个 healthy；跟踪 05:05:44–05:31:50（1566 s ≥ M=1500） |
| 05:33:08 | C 观察结束 | **healthy**（`healthy_confirmed`） |
| 05:33:50 | 建 Alertmanager silence（`alertname=CheckoutPlaceOrderErrorRatioHigh`，2 h，U-3） | active |
| 05:33:57 | **B** 发布 R（最后一次滚动），watch；05:34:00 基线 b1；harness 排 paymentFailure R+300 维持 900 s | — |
| 05:34:09 | B webhook | 201，观察 `c01d4872` |
| 05:34:20 | 自动授权 pass | B `authorize/applied`（`system`），会话 `399779ed` |
| 05:39:58 | paymentFailure 生效（flagd 确认 100%） | — |
| 05:41:00、05:43:19 | B 采样 3、4 | healthy |
| 05:45:39 | B 采样 5 | degraded → **anomalous**（页面投影 `degraded`） |
| 05:45:54 | 告警 firing | Alertmanager `suppressed`（silencedBy 本次 silence），告警投递 0 |
| 05:47:02 | before/after 对比 | `complete`（1 次尝试） |
| 05:47:21 | 消费 pass | link `created`：新事故 `61fd54cc` + Run `41498afd`；05:47:28 快照 b2 |
| 05:54:36 | Run 发布 | `completed`，27 次模型请求、43 次工具调用；随后 #184 pass 以 `system` 为该事故自动授权一次恢复观察 |
| 05:55:11 | 故障恢复（harness `finally`，flagd 确认 off） | ConfigMap 回到 `1ee5c025` |
| 05:56:39 | Observer 到期清扫（A 的 deadline 05:56:09） | A **unknown**（`deadline_expired`），0 个采样 |
| 05:57:58 | 快照 z（窗口结束） | 308 个对象 |
| 05:59–06:00 | 删除 silence（expired）；按核实过的 PID 与 cwd SIGTERM 停 web、worker、Observer、两个端口转发；`pg_ctl stop`；occupation release | 8080 不再监听，回读 null，无 `.active-*`／`fault-active.json` |

## 每个发布的结果

| 发布 | 预期（合同 §5） | 实际 | 判定 |
|---|---|---|---|
| A | webhook → 自动授权 → 首个采样前页面 pause → 保持暂停 → deadline 清扫 → unknown；被后续滚动取代属合法 | webhook 201（第二次）→ 授权 04:57:19.41（`system`，登记后 22 s）→ 0.36 s 后页面 pause → 全程 0 个采样 → 05:56:39 清扫为 `unknown`／`deadline_expired`。页面无控制按钮（终态）；详情页显示 unknown 与 deadline_expired | 符合 |
| C | webhook → 自动授权 → 中途页面 pause、resume（新会话、deadline 不变）→ healthy；unknown 也合法 | 授权 04:58:19（`system`，登记后 2 s）→ 2 个采样后 pause → 2 分 11 秒暂停期间零采样 → resume 建新会话 `35cf58cc`，`tracking_since` 05:05:44 在 resume 之后（健康与跟踪窗口不继承），deadline 仍为 R+3600 → `healthy_confirmed` 05:33:08，共 14 个样本（≤ 40） | 符合 |
| B | webhook → 自动授权 → R+300 注入、告警静默 → degraded → 一个新事故、一个真实模型 Run（trace 与冻结摘要）→ 详情页与事故页互链；B 是最后一次滚动 | 授权 05:34:20（`system`）→ 第 5 个样本 degraded → anomalous／页面 `degraded` → 对比 `complete`：before 窗 05:28:57–05:33:57、旧实例 `checkout-7bcc99987c-9cx98`、新实例 `checkout-7765446c4b-qvd72`，before_service/before_old error_ratio 0，after_service/after_new 1.0 → link `created`，恰 1 个事故（`intake:release-anomaly:<observation>`，优先级 1，automatic）、1 个 Run（输入 `opspilot-investigation-input-v6`，`release_observation` 事实含前后 RS 与 hash、ending、对比）。报告 `partial`，引用两个 Pod 名、两个 pod-template-hash、同一 checkout 镜像 digest、payment 的 `Invalid token` 与 flagd `paymentFailure`。发布详情页 HTML 链到 `/incidents/61fd54cc…`，事故页 HTML 链回 `/version-changes/c01d4872…`。全库事故 1、Run 1、告警投递 0 | 符合 |

三个发布的详情页 JSON 中 `source_release_id`、`released_at`、前后版本（镜像 digest 与 pod-template digest）与 harness 载荷逐项相等（摘要 `page_matches_payload` 全 true）；每个发布恰有 1 条 `authorize/applied` 审计，actor 与 principal_kind 都是 `system`。

## 零发布门（策略面快照）

同一 23 类、308 个对象，比对 spec 哈希、labels/annotations 哈希、generation 与非 status 的 managedFields。基线都在 harness 滚动与 watch 之后取。

| 区间 | 中间发生的产品动作 | 排除 Deployment/RS/flagd 后的变化 |
|---|---|---|
| a1 → a2 | A webhook（401 + 201）、自动授权、页面 pause | 0（整体也无任何变化） |
| c1 → c2 | C webhook、自动授权、2 个采样 | 0（整体无变化） |
| c2 → c3 | 页面 pause | 0（整体无变化） |
| c3 → c4 | 暂停期 | 0（整体无变化） |
| c4 → c5 | 页面 resume | 0（整体无变化） |
| b1 → b2 | B webhook、授权、采样、异常、对比、消费建事故与 Run | 0；整体只有 `flagd-config`（managedFields `kubectl-patch` 05:38:58Z，harness 注入） |
| b1 → z | 以上加 Run、故障恢复、A 清扫、#184 恢复观察授权 | 0；整体只有 `flagd-config` 的 managedFields（`kubectl-patch` 05:54:59Z，harness 恢复；内容哈希回到基线） |
| s0 → z（整窗） | 全部 | 0；整体变化只有 checkout Deployment（`kubectl-patch` 04:56:10Z、04:58:14Z、05:33:57Z，即三次 harness R）、checkout RS（新增 3 个、按历史上限移除 3 个，`kube-controller-manager`）与 `flagd-config` |

harness 的写入以 managedFields actor 区分：Deployment 与 ConfigMap 上新增的条目只有 `kubectl-patch`，时间与 harness 记录的 R 和注入／恢复回执吻合；RS 变化只有 `kube-controller-manager`。kind apiserver 没有开启审计日志，actor 证据只来自 managedFields 与 generation（与 PR-3、PR-4b 相同的限制）。

**CI 层：不适用。** 按 U-5 裁决（A，2026-10-25），实验环境没有目标应用的 CI 门禁，真实前后核对只覆盖集群内的门禁与流量对象；A11「不改变 CI 检查」由代码面测试（产品不含 CI／GitHub／Kubernetes 客户端与写接口）与下面的凭据缺失证明承担。本证据不声称核对过任何 CI 检查不变。

## 三个产品进程的环境变量名（不含值）

由 `scripts.f11_pr5c_lab exec-env` 在启动时记下 `sorted(os.environ)` 后原地 exec 产品入口（同一 PID、同一环境），见 [process-env-names.json](process-env-names.json)：

- web：`HOME, LC_CTYPE, OPSPILOT_EVENT_TOKENS, OPSPILOT_SELF_HEAL_INTERVAL_SECONDS, OPSPILOT_TARGET_IDENTITIES, OPSPILOT_TOOL_PROFILE, OPSPILOT_UI_USERS, OPSPILOT_WEB_DSN, PATH, PWD, SHLVL, __CF_USER_TEXT_ENCODING`
- Observer：`HOME, LC_CTYPE, OPSPILOT_OBSERVER_DSN, OPSPILOT_OBSERVER_ENV_FILE, OPSPILOT_OBSERVER_PROMETHEUS_URL, PATH, __CF_USER_TEXT_ENCODING`
- worker：`HOME, LANGSMITH_PROJECT, LC_CTYPE, OPSPILOT_ENV_FILE, OPSPILOT_LAB_ROUND, OPSPILOT_OTEL_PROMETHEUS_PASSWORD, OPSPILOT_OTEL_PROMETHEUS_USERNAME, OPSPILOT_TOOL_PROFILE, OPSPILOT_TRACE, OPSPILOT_WORKER_DSN, PATH, PWD, SHLVL, __CF_USER_TEXT_ENCODING`

三者都没有 `KUBECONFIG`、Kubernetes service account token 或 CI/GitHub token 类变量。worker 启动后从私有 `OPSPILOT_ENV_FILE` 读 DeepSeek 与 LangSmith key，Observer 从自己的 env 文件读 Prometheus 账号；这两个文件不含 Kubernetes 或 CI 凭据。web 的 Prometheus 账号本次没有提供。

## 结束核对

- `replay_session`（web 登录角色）：A 1 个会话、C 2 个、B 1 个，全部 `consistent=true`、`integrity=[]`，退出码 0；重放 verdict 与存储一致（A unknown、C healthy、B degraded）
- PG 日志：0 条 `sqlstate=42501`；ERROR 共 2 条，都是执行者自己用 owner 角色写错列名的只读查询（`42703`），无 FATAL
- web 日志（两次启动合计）：`release authorize pass failed` 0 次、`release anomaly pass failed` 0 次、`automatic observation pass failed`（self-heal）0 次；无 Traceback、WARNING、ERROR。self-heal 在 B 的 Run 发布后成功为该事故授权恢复观察，说明 #342 修复后在发布观察过的目标上不再报错
- worker、Observer 日志：无 ERROR、WARNING、Traceback

## trace 与费用

- [B 的 Run 根 run](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/bf7b4352-361d-47da-98a7-32b337b563b0/r/00000000-0000-0000-3723-0f0f84360596?poll=true)（项目 `opspilot-lab-f11-acceptance-live-20261026`，OTel `cfdbe0d09ce32a288c6e14446312b6c5`，status `published`）：27 个 llm、43 个 tool 子 run，与库中 27 次模型请求、43 次工具调用一致；2339862 输入 / 66766 输出 token
- DeepSeek 余额 91.33 → 90.78 CNY（04:53:59Z → 06:00:07Z），差 0.55 CNY；同一账户期间可能有其他会话调用，只是上界。本次只有 B 一个 Run 调模型，复盘未生成（事故仍在恢复观察中，worker 已停）

## 与 PR-4b 证据的复用关系（U-4）

- 复用 PR-4b（代码 `5943b0f`，[run.md](../f11-pr4b-live/run.md)）：S1 健康发布、S2 健康发布 + 无关 adFailure 证第 2 步（审定流量策略下的前后与新旧实例对比、健康发布与无关并发故障不关联）；S1、S2 结束后的零事故与 S3 的单事故单 Run 证第 3 步（健康终态不被重查、延迟回归被定时跟进发现）；S4 证告警先开时的追加路径。**这四个场景的授权是执行者以 web 登录角色直接调用 `authorize_release` 存储原语，不是产品路径**；产品授权路径（第 1 步）与页面投影、页面控制（第 4 步）由本窗口三个发布证明
- U-4 的复用前提检查（`git diff --stat 5943b0f..e028064 -- opspilot/observer opspilot/observation/release_samples.py opspilot/observation/release_evaluation.py opspilot/observation/release_comparison.py opspilot/investigation`）**结果不为空**：唯一改动是 `opspilot/investigation/inputs.py`（+28/−4），来自 F2 的 #346（`0d3fad9`，告警后继 Run 在附加 episode 后重取框），不是 PR-5 的 PR；改动只在 `AlertAnchor.frame_end` 与 `continuation_input(alert=…)` 新增的可选参数上，默认值保持原行为。PR-5a/5b/5c 对上述路径零改动。另外，复用的 S4 涉及告警关联，而 #343 修复（#345，`2cc2fcd`）在 `5943b0f` 之后改了 `opspilot/persistence/alerts.py`、`correlation.py`（不在 U-4 列出的路径内）。是否仍按 U-4 复用 S1–S4（尤其 S4），由 lead／用户判断；本窗口的 B 在当前代码上重新走了一遍发布异常 → 新事故 → v6 Run 的路径

## 观察到但不属本项判定

- 发布详情页 JSON 的 `ended_at` 带数据库会话时区偏移（如 `2026-10-25T22:56:39.505783-07:00`），`released_at`／`received_at`／`deadline_at` 是 UTC。时刻本身正确；`be0c789` 修的是页面显示，这里只是记录 JSON 字段格式不统一
- C 会话 1 的第 2 个样本 `failed`：p95 查询返回 NaN（`NON_FINITE`，窗口内无请求），是实验流量低造成的，与 05:02Z 宿主读数 p95 NaN 一致
- Observer 每个发布样本约 49 次 Prometheus 查询，实际间隔约 2 分 20 秒（profile 是 60 s）；C 在 resume 后 28 分钟内得到 healthy，离 deadline 还有 25 分钟
- A 在暂停中、被 C 与 B 的滚动取代，页面 `authorization_hold` 为空（该字段只用于 pending 发布）

## 凭据

- 证据里没有 token、口令或 API key，也没有本机绝对路径；UI 口令、事件 token 只以哈希进入 web 环境；页面控制请求在摘要里只写 `idempotency_label`
