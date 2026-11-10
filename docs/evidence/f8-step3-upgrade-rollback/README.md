# F8 第 3 步（#429）：带在途事故的升级、回滚与不兼容版本演练（2026-11-01）

类别：集成运行。本机 colima `m1-kind` VM，Compose 项目 `opspilot-393`，`fixture` 工具档，真实 DeepSeek 模型；Observer 与自监控用 kind lab 的 Prometheus。不是生产证明，不是 soak。冻结摘要见 [summary.json](summary.json)。合同：[F8 第 3 步合同 r3](../../tasks/2026-10-27-m3-f8-operability-contract.md)（Q24–Q26、F8S3-R1–R11），lead 修订 r4、r5（见「偏离」）。

原始记录不入库（ADR-0006），在仓库外 `~/.cache/opspilot-429-drill/`：`timeline.log`（每条命令与输出，宿主 UTC）、`builds.jsonl`（各阶段 build_info 与镜像 ID）、`snapshot-S0…S3*.json`（只读快照）、`alerts-poll.log`、`samples-b8414d23.txt`、`runs-in-window.txt`、`logs-final-*.log`。不含 key、口令或令牌值。worker_env 只按路径引用，没有读取（K2）。

## 两个版本

| | revision | 镜像 ID | schema head |
|---|---|---|---|
| 旧版 OLD | `e0c11f1e`（#436 在 main 上的父提交） | `sha256:d2e180d2…`，tag `opspilot:f8s3-e0c11f1ef938` | `0023_retention_raw_cleanup` |
| 新版 NEW | `a285cfc5`（本分支：`04eec52d` + downgrade 命令 + 40s grace + 文档，lead r4） | `sha256:7a7b9933…`，tag `opspilot:f8s3-a285cfc5ff09` | `0024_trace_outbox` |
| 不兼容 INCOMPAT | 未提交的工作树（`a285cfc5` 上只改 `TOOL_SCHEMA_REVISION` → `fixture-5-f8s3-incompat`），OCI label 与 NEW 相同 | `sha256:7a20ada3…`，tag `opspilot:f8s3-incompat` | `0024_trace_outbox` |

OLD、`04eec52d`、NEW 三者的 `versions` 元组相同（prompt `bef70649a3a8` / ctx `a30e65ea52f4` / `fixture-5`）。同为 0.1.0：每个阶段的 `opspilot_build_info` revision 与三个容器的镜像 ID 都记在 `builds.jsonl`，并复制进 summary。NEW 相对 `04eec52d` 改动的 10 个文件列在 summary `versions.new.diff_from_04eec52d`。

## 结论

| 判定项 | 升级段（OLD→NEW） | 回滚段（NEW→OLD） | 出处（summary） |
|---|---|---|---|
| ① 模型调用中被停的 Run | `6119c0e5`：round-1 工具结果提交后 SIGKILL，此时 round-2 模型调用已开始。17:08:39 被新 worker 领取（租约 17:08:37 到期，epoch 1→2），从 round-2 续跑到 `waiting_human`（execution completed）。`versions` 不变 | `1d06ab45`：同样做法。17:16:23 被旧 worker 领取（租约 17:16:22 到期），从 round-2 续跑到 `waiting_human`。`versions` 不变 | `segments.*.shape1` |
| ② `queued` 的 Run | `22f11471`：worker 已停时进入队列，17:01:50 被新 worker 领取 | `0f29fe00`：17:14:07 被旧 worker 领取 | `segments.*.shape2` |
| ③ 授权中的观察会话 | `b8414d23` 持续采样。Observer 只停了约 9s，短于 60s 间隔，样本时间上看不出缺口 | 停机前最后一个样本 17:08:32，下一次应在 17:09:32。Observer 停机 17:09:30–17:14:05，重启后**只采一次**（17:14:37），然后恢复约 90s 的提交间隔（见下「采样节奏」）。缺口可见，不补采 | `segments.*.shape3` |
| ④ `human_owned` / `waiting_human` | `df84a9af`（human_owned，gen 1）与 3 个 `waiting_human` 事故在 S0 与 S1 中完全一致，没有开 Run | S0 与 S2 完全一致 | `segments.*.shape4` |
| ⑤ 不兼容版本 | 先 OLD→NEW（无在途），再在 NEW 上 SIGKILL `ecde07b8`，然后升到 INCOMPAT。租约到期后 17:24:40 领取被拒：事件 `run_claim_refused {"code":"INCOMPATIBLE_STATE"}`，Run 变为 `blocked`，旧行（`fixture-5`、round-1 step）保留。人 `cancel`（gen 0→1）→ `new_run`（gen 1→2）开出 `e7ca88c8`（`fixture-5-f8s3-incompat`），在新版跑到 `waiting_human`。旧 Run 最后是 `cancelled` | — | `segments.incompatible` |
| 业务表只增不减 | S0→S1、S1→S2、S2→S3 每张 `opspilot_*` 表（不含 outbox）行数都不减 | | `business_tables_non_decreasing`、`row_counts` |
| outbox downgrade 计数（单列，Q18） | 回滚时 `pending=16, delivered=0, dropped=0`。另有开窗前那次：`pending=8` | | `outbox_downgrade_counts` |

回滚段的三步与 UPGRADE.md「Rolling back」逐条一致：

1. 从 NEW checkout `install.sh compose stop web worker observer`。
2. 用 NEW 镜像 `install.sh compose run --rm --no-deps migrate python -m opspilot.schema downgrade --to 0023_retention_raw_cleanup`。输出依次为 `schema current: 0024_trace_outbox`、`schema target: 0023_retention_raw_cleanup`、outbox 三类计数、`schema downgraded: 0023_retention_raw_cleanup`。
3. 从 OLD checkout `install.sh upgrade`：`schema unchanged: 0023_retention_raw_cleanup`，init-roles 四个登录都在 0023，三个运行时 `/readyz` 200，build_info 回到 `e0c11f1e`。

## 时间线（宿主 UTC；Run 与样本时间是 DB 时钟）

| 时刻 | 事件 |
|---|---|
| 15:32:21 | 拿锁 `~/.agent/locks/kind-lab`，swap 剩 1.34G；`kind_lab.py health` ok，span rate > 0 |
| 15:32:55 | 实测 `docker compose stop` 默认超时 |
| 15:33–15:34 | 开窗前发现库在 0024（#427 演练留下），OLD 的 migrate 拒绝（`Can't locate revision '0024_trace_outbox'`）；用 NEW 镜像 downgrade 到 0023（outbox `pending=8`），再装 OLD |
| 15:34–16:58 | 准备形状 ④（`df84a9af` takeover）与形状 ③；第一个会话 `af9783c6` 采到 17 个样本后在 16:01 以 `completed` 结束。我的后台等待丢了通知，这段时间空闲，暂停 |
| 16:59 | 新开事故 `7bbf9e64`，`register_remediation` 后授权会话 `b8414d23` |
| 17:01:28 | S0 |
| 17:01:36.7–17:01:53.9 | 升级段：kill worker → 新的 queued Run → `install.sh upgrade`（NEW）→ 三个 readyz 200；停机 17s |
| 17:09:05 | S1 |
| 17:09:22.3–17:14:41.99 | 回滚段：kill → 新的 queued Run → stop → downgrade → 操作者有意暂停约 4 分钟 → OLD `upgrade`；停机 5m20s |
| 17:11:29 / 17:12:14 | `OpsPilotAgentAbsent`（web/worker/observer，`opspilot_self="true"`）进入 pending / firing |
| 17:16:50 | S2 |
| 17:17:09–17:25:03 | 不兼容段 |
| 17:25:54 | 升回 NEW 后的 S3；停栈，恢复 `target_identities.json`；17:26:14 释放锁 |

## 未确认项（实测）

- `docker compose stop` 默认超时：10s 后 SIGKILL。用一个忽略 SIGTERM 的探针容器测得 10.19s、exit 137（Compose 5.1.0）。因此 40s 的 `stop_grace_period` 才让 30s 的排空在安装路径上真正生效。
- Observer 停机期间错过多次到期：重启后只采一次，不逐次补。下一次到期 = 本次样本提交时刻 + 间隔（`opspilot/observation/store.py:2079`，`next_due = now + sample_interval_seconds`）。

### 采样节奏（会话 `b8414d23`，原始 `samples-b8414d23.txt`）

配置间隔是 60s（会话行 `sample_interval_seconds=60`，HealthProfile 给的值），停机前后都没有变。实测的「提交到提交」约 90s，两侧一致：

- 停机前（样本 1–6）：提交时刻 17:01:04、02:33、04:04、05:33、07:02、08:32，间隔 89–91s；
- 停机后（样本 7–13）：17:14:37、16:07，间隔 90s；17:19:41 之后 17:21:10、22:40、24:10、25:42，间隔 89–92s。

90s = 60s 间隔 + 每个样本自身约 30s（从领取、窗口结束 `window_end` 到提交 `submitted_at`，例如样本 2 是 17:02:04.8 → 17:02:33.6）。下一次领取正好在上次提交后 60s（样本 2 的 `window_end` 17:02:04.8 = 样本 1 提交 17:01:04.1 + 60s）。所以节奏没有变化：我之前写的「60s」是配置间隔，「90s」是实测提交间隔，两者不矛盾。

不兼容段还有一个缺口：样本 8 在 17:16:07 提交后，下一次 17:17:07 到期，而样本 9 到 17:19:11 才被领取。这期间 Observer 被重建两次（17:17:1x 回到 NEW、17:17:4x 换成 INCOMPAT）。推断（未逐行核对日志）：17:17:07 前后已有一个 Observer 领取了该样本，停止时没有提交，于是新 Observer 等它的 120s 采样租约（`OBSERVER_LEASE_SECONDS`）过期才重新领取（17:17:07 + 约 124s ≈ 17:19:11）。这一缺口同样没有补采。

## 偏离与局限

- 合同修订（lead）：**r4** 新版 = 分支 HEAD `a285cfc5`；**r5** 形状 ① 用 SIGKILL 制造。理由见下两条。观察端的发现：Observer 停机后回来只采一次，不补采（见「未确认项（实测）」）。

- **r4（lead）**：Q25 写新版 = `04eec52d`，但它没有 downgrade 命令，Q26 的回滚无法用它跑。新版改用本分支 `a285cfc5`（`04eec52d` + 本步改动），schema、版本号与 `versions` 不变。
- **r5（lead）**：形状 ① 用 `install.sh compose kill worker`（SIGKILL）在模型调用进行中制造。worker 收到 SIGTERM 后会把整个在途 attempt 跑完（最多 30s），而 R3 的前提「模型调用常超过 30s」不成立：本次单轮模型调用约 1–12s，#414 drill2 为 5–8s。这类 Run 在 40s grace 内通常能排空到终态，不会留给租约恢复。本次没有一个在途 Run 赶上优雅停止（每次 stop 前 worker 已被 kill），所以「排空了多少」没有观察值。空闲时 web 与 observer 的优雅停止约 1s。
- 演练用 PATH shim 给 `install.sh` 的 compose 调用加上同一 checkout 的 `compose.lab.yaml`（健康端口，供 lab Prometheus 抓取），并加一个只让 Observer 接入 `kind` 网络的 override。产品 compose 与 install.sh 未改。
- 为让每个 Run 各开一个事故，`target_identities.json` 临时改为 6 条 `OpsPilot429Drill` 条目，结束后恢复原字节（sha256 `1714ab7b…`）。除 `checkout-prod` 外的演练目标不在 fixture 工具档登记表里，因此这些 Run 都以 `INCOMPLETE_INVESTIGATION` 交接为 `waiting_human`（终态，execution completed），与 #414 的现象相同。
- 回滚段 4 分钟停机是操作者有意为之，用来测会话缺口与自监控告警，不代表回滚本身的耗时（stop + downgrade 约 2s，OLD `upgrade` 约 40s）。
- 自监控告警只取了 Prometheus `ALERTS`，没有导出 Alertmanager `/api/v2/alerts`。路由到 `opspilot-self` 已由 #414 证明。
- 合成告警由脚本 POST 到工作台 intake，不是 Alertmanager 投递的。
- swap 偏紧：开窗 1.34G，最低 0.41G，结束 0.50G。期间无容器 OOM；回滚段 OLD 运行时启动用了 36s（升级段 6s）。
- 回滚段与升级段的两次升级都没有改规则的迁移，所以「迁移结束授权会话」那条路径本次没有经过。

## trace 与费用

### trace（2026-11-10 补记）

- 本次 8 个 Run 没有 trace：演练在 2026-11-01，那时 #428（outbox 投递到 LangSmith）还没合并（11-02 由 PR #441 合并），OLD `e0c11f1e`、NEW `a285cfc5`、INCOMPAT 三个构建都没有产品模式投递（只有 lab 直发的 `OPSPILOT_TRACE=lab`），compose 没有设 `OPSPILOT_TRACE`，即按默认 `off` 运行，所以不存在可附的 LangSmith 链接。0024 的触发器照样写了 outbox 行，但从未投递：回滚时 downgrade 删掉了 `pending=16` 行（见上表）。
- #441 合并后不补带 trace 的演练，理由：
  1. 回滚段在设计上无法完整 trace：旧版 head 是 0023，没有 outbox 表与投递代码；回滚时 downgrade 按 Q18 删掉 outbox 表，未投递的 span 照合同丢弃。补跑只能多出升级段和不兼容段在新版一侧的部分 span，回滚段一侧仍然是空的。
  2. 「Run 跨 worker 重启续跑时 trace 还能投到」已由 #428 真实证据证明：故障②在投递中途 SIGKILL worker，Run `7da57ca3` 在租约到期后由新 worker 续跑到 `waiting_human`，outbox 行重领重投，LangSmith 上该 span 只有一条（[243-trace-outbox](../243-trace-outbox/README.md)）。按事故与版本在 LangSmith 筛选也在那里通过。
  3. 本步的判定项（在途形状、只增不减、不兼容拒绝）都由 PG 快照与 Run 事件确定性判定，trace 不参与判定。
- 72h soak 同样不开 trace（soak 记录 S-R18：控制侧不放 LangSmith key）。

### 费用（2026-11-10 补记）

主数取 token×单价，见 [cost-primary.json](cost-primary.json)：从保留的 `opspilot-393` PG 卷只读查询这 8 个 Run 的 `opspilot_steps.usage`，22 次已结算调用（缓存命中 44544、未命中 9097、输出 15823 token），单价取 DeepSeek 官方价格页（`deepseek-flash`，2026-11-07 读取，假定 11-01 相同，未核对）。窗口是北京时间周日 23:35 到周一 01:25，全部闲时：**CNY 0.0733**。另有 3 次调用在 worker 被 SIGKILL 时还在进行（形状①两次、不兼容段一次，预留停在 `reserved`），没有用量记录，供应商是否计费未知；按本次最贵一步的 3 倍估上限 CNY 0.0331，合计上限 **CNY 0.1064**。

不用余额差：DeepSeek 余额是账户级，同账户其他 key 也在消耗（用户 2026-11-06），差额代表不了本次演练；不用控制台：只按天、按账户汇总（用户 2026-11-09）。口径与 [72h soak 费用](../m3-soak-2026-11-07/README.md#费用) 相同。2 个准备观察会话的 Run（`b2966b23`、`230edc28`）计入。
