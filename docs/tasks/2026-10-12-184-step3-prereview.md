核对基线：当前分支 `feature/F6-184-self-heal-step3`，HEAD `df3863ef97cd290663b565c917e2626736ec74d7`，`git status --short` 无变更输出；按任务记录合同 r6 与第 2 步决定审查，仅静态读取，实验环境当前状态未确认。

| 编号(D3-xx) | 问题 | 选项与代价 | 推荐（依据，引用 文件:行） | 影响面 | 分类 A=需用户裁决/B=lead 可逆 |
|---|---|---|---|---|---|
| D3-01 | 两个事故从哪里进入？ | ①真实 Alertmanager webhook：多等待告警形成，但覆盖完整入口；②工作台手动提交：更容易控制时间，但只证明 UI 入口后的路径。 | **①两个场景都用真实告警。** 已有 kind webhook 实测成功，配置约在 inject 后 5–7 分钟 firing；不伪造 webhook，不把 resolved 通知当恢复授权。依据：`docs/development.md:163`；`docs/evidence/m1-04-intake-live/run.md:20–27`。这是实验覆盖选择，并非新增产品合同。 | 入口、实验时长、告警关联 | B |
| D3-02 | 如何登记目标身份？ | ①复用经环境核对的身份文件；②按告警标签或模型输出猜身份：违反 C3；③直接把 Deployment UID 用作注册表键：可能与调查工具目标不一致。 | **注册表键使用工具目标 `m0-otel-20260909`，并核对既有登记身份。** 代码把文件键作为 `resource_uid`；条目需要 `integration_id/cluster_uid/namespace/workload/health_profile_id`，另加唯一命中的 `match.labels={cluster:opspilot-m1,namespace:otel-demo,service:checkout}`。不能从标签替代身份。实际 integration/cluster 身份值本次**未确认**。依据：`opspilot/web/store.py:136–169`；`opspilot/schema.py:185–207`；`opspilot/observation/store.py:1843–1859`。 | 身份绑定、fail-closed | B；改变身份信任来源为 A |
| D3-03 | 什么才算真实 Run 已具备自动授权条件？ | ①真实 worker 经产品发布；②owner 写 conclusion、手工完成 Run 或直接创建会话：绕过待验证触发链。 | **①且检查当前 Run。** `conclusion IS NOT NULL`、当前 Run `state='completed'`、Run 与事故 `control_generation` 相等；同时记录报告与发布证据。用户所说“非空”在 r6 中的准确谓词是非 NULL，不另改成字符串非空合同。依据：`docs/tasks/2026-10-12-184-self-healing-recovery.md:23`；`opspilot/observation/store.py:1458`。 | 触发真实性、代际一致 | B；放宽 C1 为 A |
| D3-04 | 模型交接而没有发布结论怎么办？ | ①记失败并停止该次尝试；②有限重跑、保留失败；③无限重跑或造结论：掩盖失败。 | **默认每场景一次真实尝试；交接即该次未达到前提，照实冻结，不计通过。** 因已定位的环境故障重做，使用新实验 ID，保留首次失败与重做理由；不内置“刷到成功”的循环。不把 C9 的自动授权失败重试当作模型重跑政策。依据：任务记录 `:23、35`；`AGENTS.md:43–45`。若要预先允许固定数量重跑，可由 lead 记录实验编排上限，不改产品每 Run 上限。 | 失败口径、费用、计数 | B；交接也算通过为 A |
| D3-05 | S1 在何时 restore？ | ①结论及自动会话授权后：明确证明自动观察链；②结论前：也可能符合 r6，但易让调查只看到健康，并增加先后关系歧义。 | **等 C1 成立，并看见自动授权会话后立即 restore。** 注入到授权期间保持故障；产品侧不提交任何 control。记录发布、授权、restore 三个 UTC 时间及 harness 校验结果。健康窗口从第一个采纳的健康采样窗尾起算，不从 restore 时刻起算。依据：任务记录 `:25`；`opspilot/observation/store.py:591–599`。 | S1 因果边界、时间证据 | B |
| D3-06 | S1 用什么健康参数？ | ①随产品发布的 profile：约需恢复数据洗出时间加 600 秒健康窗；②缩短健康窗/降低流量门：省时间但削弱健康证据。 | **①不缩短。** `evaluation_window=300s`、freshness `90s`、query timeout `20s`；session deadline `3600s`、max samples `40`、interval `60s`、sustained `600s`。自动循环保留默认 `30s`，Observer poll 保留 `5s`。依据：`opspilot/observer/profiles/otel-demo-checkout.json:11–22`；`docs/development.md:102、177`。 | 健康定义、实验时长 | B；削弱健康判据或验收为 A |
| D3-07 | S2 如何确保测到 deadline，而不是次数耗尽？ | ①原样默认 profile：40 次采样可能先耗尽，无法保证期限路径；②实验 profile 只改 deadline 为 `1200s`：约 20 分钟观察；③旧 bounded profile：11 次，证明的是次数耗尽。 | **②只在 S2 的实验 profile 改 `session.deadline_seconds=1200`，其余保持 shipped 值，冻结完整内容及 revision。** 仍满足 `600≤1200`，40 次预算不会按正常 60 秒间隔先耗尽；最终必须检查实际 ending。这是已有 `OPSPILOT_HEALTH_PROFILE` 配置接口的实验变体，改变实验会话期限，但不改 shipped 配置或验收要求。依据：`opspilot/web/__main__.py:151–159`；`opspilot/observer/health_profile.py:303–310`；`opspilot/observation/store.py:604–611`。旧 bounded 文件实际是 `deadline=3600,max_samples=11`：`docs/evidence/m1-02-live/otel-demo-checkout-bounded.json:14–18`。 | 期限路径、配置版本 | B；以次数耗尽替代本步“到期”为 A |
| D3-08 | 指标滞后如何影响通过判定？ | ①只等固定三分钟：简单，但不是实际故障证据；②核对实时信号、覆盖与计数器增长：多一次工程探测，能区分环境停滞。 | **②固定等待仅作预计。** inject 后确认错误信号实际退化且流量、必要遥测可用；S1 restore 后等历史窗口洗出，再积累完整健康窗；S2 保持流量和必要遥测，不能靠撤流量制造 unknown 来代表持续故障。约三分钟滞后是文档经验值，当前延迟**未确认**。依据：`docs/development.md:155、165`；`docs/evidence/m1-02-live-2/run.md:13、20`。 | 环境前提、真假健康、失败归因 | B |
| D3-09 | S2 到期后究竟写什么？ | ①Observer 期限结束；②要求自动授权决定变为 `handed_off`：混淆授权失败与观察未恢复。 | **①事故回 `open`；会话 `state='expired'`、`ended_reason='deadline_expired'`；ending 的 `transition='observation_ended_unconfirmed'`。** F6 投影 `observation_ended=true`、`human_interaction='handoff'`，`handoff_reasons` 包含 `DEADLINE_EXPIRED`，附实际恢复原因。不要预设所有信号原因都出现，也不要要求事故上新增同名 handoff 字段。自动授权决定仍为 `authorized`，它记录授权成功。依据：`opspilot/observation/store.py:413、2253–2289`；`opspilot/observer/replay.py:873–878`；`opspilot/acceptance_recovery.py:980–983`。 | 状态与原因口径 | B，已定合同 |
| D3-10 | S2 何时清理故障，是否重新观察？ | ①期限终态与证据捕获后 restore；②提前 restore：可能转成 S1；③到期后人为补开观察：改变场景。 | **①先捕获期限 ending、投影与工作台，再 restore 清理。** 随后等环境恢复可用；可再次读取同一事故，确认该代际没有第二次自动授权。每次 inject 使用唯一 ID，restore 使用相同 ID，遵守 harness 的现场字节比对，不覆盖其他会话变更。依据：任务记录 `:27`；`scripts/kind_lab.py:895–923`。 | 环境清理、幂等、并发 | B |
| D3-11 | summary 应冻结哪些信息？ | ①沿用 ADR-0006 summary 外壳、增加恢复投影；②直接沿用旧 F6 全量包：没有本次真实调查 trace，且容易重新入库原始证据。 | **①每次真实 Run 一个 summary。** 包括 schema/experiment/run_id/frozen_at、scenario/round、受检 commit 与 dirty 工件哈希、输入摘要、incident/session ID、报告及发布依据、确定性 verdicts、调用/采样/查询计数、余额差与并发费用限制、trace、raw ledger sha256/bytes/path。恢复部分冻结身份、profile 内容/revision、授权来源与审计、关键时间、会话/ending、F6 投影、权限测量、工作台关键展示断言、harness 操作校验。依据：`scripts/lab_evidence.py:250–286`；`docs/evidence/m1-04-alert-window-live/live-runs/bc327b19-0d42-502c-ad60-54cd32b78da0/summary.json:1–31`；`docs/evidence/m1-02-live-2/s1-recovered/summary.json:31–63`。 | 长期证据、可复核性 | B |
| D3-12 | 外部断言源与入库边界是什么？ | ①F6 公开投影加原始字段交叉检查；②脚本自算“恢复”；③仅看页面文案。 | **①用 `recovery_outcome(scenario, RecoveryRecords(...))`，并测量 Observer 自己的权限。** 复用 `incident_records()` 与 `table_privileges()`，投影及离线 replay 必须一致；UI 文案单独检查。入库仅冻结 summary、简短运行记录和可复用工具/实验配置；原始 ledger、完整库快照、原始遥测、原始页面与日志留忽略目录，摘要保留哈希。历史包不改写。依据：`docs/evidence/m1-02-live/lab_run.py:282–309`；`opspilot/acceptance_recovery.py:951–983`；`docs/adr/0006-trace-evidence-and-backlog.md:16–20`。 | 验收公开接口、数据出口 | B；新增非公开数据出口为 A |
| D3-13 | LangSmith 应链接哪个 Run？ | ①每场景链接发布结论的当前调查 Run；②借用旧 trace 或把 Observer 说成有 trace。 | **①两场景各链接自己的真实调查 Run，核对 business run_id、incident_id 和 OTel trace ID。** 如有多个 attempt，列明对应关系，明确哪个 attempt 发布；read-back 必须 found、有有效 URL。Observer 目前 trace 缺口照实披露，恢复证据由 PG/F6/replay 提供。依据：`scripts/lab_evidence.py:133–140、180–226`；`docs/evidence/m1-02-live-2/run.md:6`。PR 至少一次 trace 是最低门槛，不宜让另一场景缺关联。 | Trace 归属、证据完整性 | B |
| D3-14 | 复用 runner 还是新增脚本？测试由谁写？ | ①改通用 fixture runner：扩大工具职责；②组合现有工具，新增薄编排/取证脚本：新增少量工程代码；③旧 F6 prepare/register：绕过自动链。 | **②建议新增 `scripts/m1_184_self_heal_live.py`，复用 kind harness、lab_evidence 和 F6 公共接口，不实现另一套恢复算法。** `m1_live_runner` 和 `m1_takeover_evidence` 实际都构造 fixture 输入；旧 F6 runner 依赖人工 register。依据：`scripts/m1_live_runner.py:294–308`；`scripts/m1_takeover_evidence.py:326–341`；`docs/evidence/m1-02-live/lab_run.py:147、282–285`。AGENTS 没有明文豁免“工程脚本”：本步的**通过断言**应由独立作者按合同/公开接口编写；一般 CLI、序列化辅助测试不要求全部交独立作者。非平凡取证逻辑仍独立审查。依据：`AGENTS.md:50–51`。 | 工程工具、独立验收 | B；修改验收断言要求为 A |
| D3-15 | 部署、旧数据、并发与完成状态如何处理？ | ①专用空库、三种运行角色、单场景串行；②复用历史库：自动循环可能处理存量事故，worker 也可能领取其他 Run。 | **①先协调共享 kind 环境空闲，专用 lab 库迁移至受检 head。** web/worker/Observer 保持既有角色；owner 仅迁移、工程取证，不授予产品集群凭据。S1/S2 串行，等待真实 DB 终态，不用宿主时间提前写结束。已有归档证据和 F6 `passes=true` 不变；任务记录第 2 步“未执行”描述与当前代码已有实现不一致，lead 更新状态记录即可。依据：`docs/development.md:88–92、129、174–181`；任务记录 `:51、62–64`。实际 schema、角色、空闲状态**未确认**。 | 部署前提、存量候选、权限、状态记录 | B；修改权限、PRODUCT-CONSTRAINTS、验收步骤或 feature_list 为 A |

按推荐无需新增 A 类合同改动。表中的 A 类替代方案应单独停止裁决，不能为跑通实验采用。

### 执行清单

以下是交给执行者的命令与判据，**本次均未执行**。建议新脚本尚不存在；下面明确标出的 CLI 是待实现接口，不能当作现有命令。

**1. 公共准备**

先协调另一会话释放实验环境；核对受检版本包含第 1、2 步，然后使用专用 lab 数据库。已有环境无需为了实验重新执行 `kind_lab.py up`。

```sh
git status --short
git rev-parse HEAD
.venv/bin/python scripts/kind_lab.py health

# owner DSN 由私有环境提供，仅用于专用 lab 库迁移
OPSPILOT_DSN="$OPSPILOT_LAB_DSN" make migrate
```

身份文件至少采用以下结构；`integration_id`、`cluster_uid` 从已核对的部署登记取得，不能用示例替代：

```json
{
  "m0-otel-20260909": {
    "integration_id": "<已核对的实验集成身份>",
    "cluster_uid": "<已核对的集群身份>",
    "namespace": "otel-demo",
    "workload": "checkout",
    "health_profile_id": "otel-demo-checkout",
    "match": {
      "version": 1,
      "labels": {
        "cluster": "opspilot-m1",
        "namespace": "otel-demo",
        "service": "checkout"
      }
    }
  }
}
```

分别用受限角色和私有环境启动三个产品进程；不要把模型凭据环境传给 Observer：

```sh
# web 终端：私有 env 已提供 OPSPILOT_WEB_DSN、OPSPILOT_UI_USERS
set -a
. "$PWD/tmp/m1-kind-lab/prometheus-investigator.env"
. "$PWD/tmp/m1-kind-lab/workbench-alertmanager.env"
set +a

export OPSPILOT_TOOL_PROFILE=otel-demo
export OPSPILOT_TARGET_IDENTITIES="$PWD/tmp/184/targets.json"
export OPSPILOT_SELF_HEAL_INTERVAL_SECONDS=30
export OPSPILOT_HEALTH_PROFILE="$PWD/opspilot/observer/profiles/otel-demo-checkout.json"
export OPSPILOT_BIND=127.0.0.1:8080
export OPSPILOT_ALLOWED_ORIGINS=http://127.0.0.1:8080
.venv/bin/python -m opspilot.web serve
```

```sh
# worker 终端：私有文件含 OPSPILOT_WORKER_DSN、DEEPSEEK_API_KEY、
# investigator 凭据及 LANGSMITH_API_KEY；实际值不打印
export OPSPILOT_ENV_FILE="$PWD/tmp/184/worker-private.env"
export OPSPILOT_TOOL_PROFILE=otel-demo
export OPSPILOT_TRACE=lab
export OPSPILOT_LAB_ROUND=184-step3-20261012
export LANGSMITH_PROJECT=opspilot-lab-184-step3-20261012
.venv/bin/python -m opspilot.worker_main
```

```sh
# Observer 终端：仅提供 Observer 自己的连接与账号
export OPSPILOT_OBSERVER_DSN="$OPSPILOT_LAB_OBSERVER_DSN"
export OPSPILOT_OBSERVER_PROMETHEUS_URL=http://127.0.0.1:19090
export OPSPILOT_OBSERVER_ENV_FILE="$PWD/tmp/m1-kind-lab/prometheus-observer.env"
export OPSPILOT_OBSERVER_POLL_SECONDS=5
.venv/bin/python -m opspilot.observer
```

启动真实 Run **之前**，由新编排脚本调用 `lab_evidence.prepare_lab_project(...)` 建 project 并回读 `longlived`；worker 启动本身不替代此步骤。新脚本还需准备 S2 profile：仅将 shipped `session.deadline_seconds` 改为 1200，保存内容、revision 和差异。

**2. S1：授权后由 harness 恢复**

```sh
.venv/bin/python scripts/kind_lab.py fault inject --experiment-id m184-s1-20261012
```

执行者随后：

1. 用 lab 账号探测实际退化、流量和遥测新鲜度；等待真实 firing intake，记录 incident/current Run ID。
2. 等真实发布满足 C1，再等一条 `automatic` 会话；记录授权事务时间与绑定代际。
3. 执行恢复：

```sh
.venv/bin/python scripts/kind_lab.py fault restore --experiment-id m184-s1-20261012
```

4. 等终态；读取工作台页面，调用公开投影，执行离线重放：

```sh
.venv/bin/python -m opspilot.observer.replay --incident "$S1_INCIDENT_ID"
```

建议新脚本提供以下**待实现**取证命令：

```sh
.venv/bin/python scripts/m1_184_self_heal_live.py collect \
  --scenario S1 --incident "$S1_INCIDENT_ID" \
  --experiment-id m184-s1-20261012 \
  --lab-round 184-step3-20261012
```

**S1 确定性通过判据：**

- 真实调查满足 C1；同一事故代际只有一条成功自动授权及一个关联会话。
- `opspilot_controls` 有 `action='auto_authorize_observation'`、`actor='system'`，不推进 control generation；没有人工 control 或 `register_remediation`。
- 会话 `authorization_source='automatic'`、`target.revision=null`、`state='completed'`、`ended_reason='recovery_confirmed'`；事故 `lifecycle='resolved'`。
- F6：`replay.consistent=true`、`recovery_confirmed=true`、`healthy_window_seconds≥600`、`authorization_source='automatic'`；无人工登记动作、无 `human_control` 权限标记。
- 工作台显示 `recovery_status='recovered_no_remediation'` 和准确文本 `"recovered, no remediation recorded"`。
- 本次发布 Run 的 trace 回读成功；summary 冻结上述判据与实际计数。模型交接、遥测缺失或 timeout 均不计通过。

**3. S2：持续故障直到期限结束**

S1 清理后，确认流量、健康信号与告警基线恢复，再停下本实验 web，换 S2 profile 重启；worker、Observer 保持原角色。新 profile 只用于随后新授权的会话。

```sh
export OPSPILOT_HEALTH_PROFILE="$PWD/tmp/184/otel-demo-checkout-deadline-1200.json"
.venv/bin/python -m opspilot.web serve
```

```sh
.venv/bin/python scripts/kind_lab.py fault inject --experiment-id m184-s2-20261012
```

执行者：

1. 确认实际退化和必要信号可用，等待新 firing、新事故及真实 C1。
2. 等自动会话出现，核对冻结参数 `deadline_seconds=1200,max_samples=40,interval=60,sustained=600`。
3. 保持故障与流量，等 **DB deadline 已到且 Observer 提交期限终态**。宿主超时只是编排保护，不是通过证据。
4. 先完成投影、页面、重放和 summary 捕获：

```sh
.venv/bin/python -m opspilot.observer.replay --incident "$S2_INCIDENT_ID"

# 待实现 CLI
.venv/bin/python scripts/m1_184_self_heal_live.py collect \
  --scenario S2 --incident "$S2_INCIDENT_ID" \
  --experiment-id m184-s2-20261012 \
  --lab-round 184-step3-20261012
```

5. 再清理环境：

```sh
.venv/bin/python scripts/kind_lab.py fault restore --experiment-id m184-s2-20261012
.venv/bin/python scripts/kind_lab.py health
```

**S2 确定性通过判据：**

- C1、自动来源、代际与无人控制判据同 S1；故障持续证据覆盖观察期间，必要遥测与流量仍有效。
- 会话 `state='expired'`、`ended_reason='deadline_expired'`；恰好一条对应 ending，`transition='observation_ended_unconfirmed'`、`lifecycle_after='open'`，结束时间不早于冻结 `deadline_at`。
- 事故 `lifecycle='open'`；F6 `replay.consistent=true`、`recovery_confirmed=false`、`observation_ended=true`、`observation_ended_reason='deadline_expired'`、`human_interaction='handoff'`，`handoff_reasons` 包含 `DEADLINE_EXPIRED`。
- `max_samples_exhausted`、授权失败 `handed_off`、遥测缺失导致的 unknown，分别如实记录，均不能替代本场景通过。
- 工作台没有恢复成功声明；清理后同一代际没有补开第二个自动会话。
- 本次发布 Run 的 trace 与 summary 对应一致；原始 ledger 不入库。

两场景的独立验收作者负责上述断言，完成前独立审查核对冻结证据；本步不修改 `feature_list.json`、F6 既有验收步骤或 `passes`。