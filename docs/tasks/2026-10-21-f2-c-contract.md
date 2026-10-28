# F2 PR-C 合同预审：告警关联 U1、迁移 0018、锁键、页面

- 预审者：Claude（Opus 5.5，代 Codex，未参与实现），只读，基于 main `91a011c`，2026-10-21。未启动 PG、kind 或服务，未调用模型；「已核对」均为读代码所得，没有在库上跑过。
- 依据：F2 合同 r1（U1-A、U2-A、R4–R6，`docs/tasks/2026-10-17-f2-contract.md`）、F2 任务记录、F11 PR-4 预审（`tmp/m2-orch/f11-pr4-out.md` 中的 P4-04、R2、R4）、C3 §4:105、§6:227。
- 修订（2026-10-24，#343 U1-A）：§6 第 5 条由「发布或手工开的同目标事故不参与告警附加」改为「手工开的不参与，发布开的按认领参与」。

## 0. 现状要点（已核对）

- 入口在 `Workbench.intake_alert`（`opspilot/web/service.py:401-484`）。目标在事务外解析：`_alert_opening` 调 `resolve_target`（:486-560）。解析得不到目标时转交接（target 为 None）。
- 存储在 `_AlertOps.record_alert`（`opspilot/persistence/alerts.py:26-108`）：
  - advisory 锁以 `delivery_key` 为键（:54-56）。
  - 只按 `delivery_key` 查 identity（:57-60），所以新的 `startsAt` 必然走 `_open` 开新事故（:61-65）。
- `opspilot_alert_identities` 的约束（`0010_alert_intake.py:65-80`）：
  - `incident_id NOT NULL UNIQUE`，即「一个事故对应一个 identity」。
  - `CHECK ((handoff_reason IS NULL) = (run_id IS NOT NULL))`。
  - `target_id` 存的是 resource_uid 文本，不是 targets 表的 UUID。
  - 结论：一个「附加」上来的 episode 没法再插一行 identity。要插就得改 UNIQUE 或 CHECK。
- 依赖「一个事故一个 identity」的读方：
  - 列表页 `LEFT JOIN opspilot_alert_identities`（`opspilot/web/store.py:811,819`）：一个事故多行 identity 会让列表重复。
  - 验收投影 `fetchone()`（`opspilot/acceptance_alert.py:144-147`）。
  - 续开重建取 `records.identity` 与 `deliveries[0]`（`web/service.py:1196-1235`）。
  - `observation/store.py:1682`。
- 投递结果有 CHECK（`0010:48-55,88`）。词表与代码一致由 `tests/test_m1_04_alert_intake_impl.py:328-340` 断言，测试导入的是 0010 模块。
- `append_input`（`opspilot/persistence/controls.py:627-679`）：自开事务，锁事故行，按 `input_id` 幂等（内容不同时报 `IDENTITY_CONFLICT`），写 `kind='event'`，调 `mark_stale_in`。目前产品代码里没有调用方，只有 investigation/store 里有一个同名但不同的方法。
- 模型只能看到输入 `content` 里的 `text`、`channel`、`question` 三个字段，经过脱敏与截断（`opspilot/investigation/context.py:1099,1116-1138`）。所以追加事件不需要新增输入快照版本，也不改 `prompt_revision`。
- `_accept_in` 先锁 scope（FOR SHARE），再锁目标挂起行（FOR SHARE），最后插入事故（`opspilot/persistence/incidents.py:48-110`）。它的 INSERT 没有 `related_incident_id` 列。
- grants：
  - web 对 incidents 有列级 `INSERT(current_run_id,incident_id,intake_key,lifecycle,state,target_id)`（`opspilot/persistence/grants.py:101-105`）。
  - Observer 对 incidents 是列级 SELECT（`opspilot/acceptance_recovery.py:156-162`）。
  - worker 对 incidents 有 `SELECT, UPDATE(conclusion)`。
- 生命周期取值为 `open / observing_recovery / resolved / closed`（`0002_state_checks.py:56-61`）；领域规则见 `domain/subjects.py:157-166`。
- 实验环境 Alertmanager 配置（`scripts/kind_lab/alertmanager-values.yaml`）：`send_resolved: true`，`group_interval 1m`，`repeat_interval 1h`；规则为 `for: 2m`、rate 5m（`scripts/kind_lab/values.yaml:182-199`）。
- 页面：`_alert.html:54` 的投递表没有 `starts_at` 列，附加 episode 显示不出区别。`_alert_view` 的 `latest` 取该事故的最后一条投递（`web/service.py:1722-1735`）。

### 既有测试中「新 startsAt 开新事故」的断言（实现者问过的未确认项，已查）

| 位置 | 断言 | 处理 |
|---|---|---|
| `tests/acceptance/test_m1_04_alert_intake.py:300-305` | 同 fingerprint、新秒 → `created`，incident 与 run 都不同 | **按合同变更改写**（U1-A）：旧事故开放时改为 `attached`，incident 相同，`run_id` 为空，事故的 Run 数不变。这是 M1-04 验收测试，由独立测试作者改，不由实现者改。 |
| 同文件 `:391-412` `test_c_e8…` | 对另一个秒的 resolved 判 `resolved_recorded` | 推荐语义下不变（那个秒从未 firing 过），保留。 |
| `tests/test_m1_04_alert_intake_impl.py:328-340` | 0010 的 OUTCOMES 恰为 6 个 | 0010 本身不变，测试保留；另加 0018 词表测试。 |
| `tests/integration/test_f12_acceptance_postgres.py:872-895` | 相同 startsAt 再 firing → `replayed` | 不受影响。 |
| 其余 alert 测试（window、context、f7 audit、runtime roles） | 每次用随机 fingerprint，或相同 startsAt 重放 | 未见受影响的断言。 |

## 1. 九类必答

1. **时间**
   - 已定：不设关联时间窗，以「旧事故未 closed」为边界（合同第三节第 1 类；U1-A）。
   - 已定：身份时间沿用 M1-04 E2，即 UTC 秒。所有时间用 DB 时钟。「最新」按 `opspilot_incidents.created_at`（clock_timestamp，`0001:37`）。库里没有 closed_at 列，「最新的已关闭事故」也按 created_at。
2. **计数与口径**
   - 已定：每次附加不开 Run（U1-A）。
   - 待决 C2：附加 episode 的重复通知与 resolved 如何记账。
   - 可逆默认 R-C3：哪些投递产生事件。
3. **状态转移**
   - 已定：未 closed 就附加，包括 `observing_recovery` 与 `resolved`；附加不改 lifecycle、mode、`control_generation`，也不改当前 Run（U1-A；F11 U8「不覆盖人工控制」）。
   - 已定：closed 时新建事故并写 `related_incident_id`，旧事故不重开（C3 §4:105）。
   - 待决 C3：候选集与选择规则。
   - 注意：附加到 `resolved` 的事故后，事故仍为 resolved 且没有新 Run，所以页面与列表必须让人看得到（R-C6）。
4. **主体与权限**
   - 已定：只有 web（`opspilot_web`）写入。
   - 推荐（R-C5）：
     - web 的 incidents INSERT 增加 `related_incident_id`。
     - 任何角色都没有它的 UPDATE，写入后不可改。
     - worker 与 Observer 不变；Observer 的列级 SELECT 不含新列。
     - 追加事件所需的 inputs INSERT 与 `mark_stale_in` 路径，web 已有（control 路径在用）。
5. **数据合同、迁移与部署顺序**
   - 待决 C1：附加 episode 的存储方式。
   - 迁移 0018 的内容见第 4 节。
   - 部署顺序：先迁移，再发 web。旧 web 在新 schema 上兼容（新列可空，CHECK 是超集）。worker 无需同步升级：输入版本与 `prompt_revision` 都不变。
6. **旧数据与已归档证据**
   - 已定：不回填关联；`m1-04-intake-live`、`f12-live` 按当时的行为解读（合同第三节第 6 类）。
   - 旧库里同一 fingerprint 加同一目标可能已有多个开放事故（旧行为是每个 startsAt 开一个），由 C3 的「取最新」规则消化，不改旧行。
   - downgrade 规则见 R-C8。
7. **并发与水位**
   - 已定：事件不推进已分析水位，在下一个安全边界纳入（`controls.py:627-677`；`context.py:1160-1186`）。
   - 锁键见 R-C1，锁顺序见 R-C2。
8. **外部调用**
   - 入口不发任何外部调用。附加不开 Run，所以不增加模型或查询预算。真实运行的取证见 C4。
9. **验收投影与 `passes`**
   - 已定：PR-C 不翻 F2 `passes`，由 PR-E 翻，用户门。
   - `AlertIntakeOutcome` 的增量见 R-C7；真实证据形态见 C4。

## 2. 决定表：需用户裁决

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| C1 | 附加 episode（新 `startsAt`）在库里怎么存 | **A**：不给附加 episode 写 identity 行。附加的事实就是一条 `opspilot_alert_deliveries` 行（outcome=`attached`，`incident_id` 为被附加的事故），加一条 `kind='event'` 输入。之后再收到同一 `delivery_key`，先查 identity，查不到再按 `delivery_key` 查最早的 `attached` 投递来定位事故（`(delivery_key, delivery_id)` 索引已有，`0010:98`）。代价：附加身份没有独立的表，靠投递审计行承担身份职责。好处：identities 与事故继续 1:1，列表 join、验收 `fetchone`、续开重建都不用改。**B**：新表 `opspilot_alert_attachments(delivery_key PK, fingerprint, starts_at, incident_id, input_id, created_at)`。代价：多一张表、多一组 grants 与重放核对，语义更显式。**C**：放宽 identities 的 UNIQUE 与 CHECK，附加也插 identity。代价：`web/store.py:811` 列表会重复，验收投影与重建都得改。 | **A** | 迁移 0018、`alerts.py`、读方 | |
| C2 | 附加 episode 的重复通知与 resolved 记成什么 | **A**：重复的 firing 记 `attached`（`incident_id` 相同，不再追加事件，靠 `input_id` 幂等）；resolved 记 `resolved_attached`，挂到被附加的事故上。只新增一个枚举值 `attached`，与合同字面一致。代价：首投与重投的 outcome 相同，要靠事件计数区分。**B**：再加 `attached_replayed`，与 `replayed` 对称。代价：CHECK 多一个值，合同字面要扩。 | **A**，并在 `AlertResult` 里 `run_id` 一律为空 | CHECK、验收投影、页面 | |
| C3 | 共享的「开放事故」判据（顺带定下 F11 P4-04） | 候选集由各来源各自给出：**告警**取 `opspilot_alert_identities` 中 `fingerprint` 相同、`target_id`（resource_uid）等于本次解析结果、且 `handoff_reason IS NULL` 的 identity 所开的事故；**发布**（F11 PR-4）取 `incidents.target_id` 相同的任意事故，target 为 NULL 的交接事故不参与。选择规则共享为纯函数：候选中有 `lifecycle<>'closed'` 的 → 附加到 created_at 最新的一个（相同时取 incident_id 较大者）；全部 closed → 新建事故，`related_incident_id` 指向 created_at 最新的已关闭事故；没有候选 → 新建、不关联。本次解析为交接（无目标）→ 照旧开交接事故，不关联。备选：多个开放时取最早的；或改为新建并关联。代价：旧库的多开放事故里，较早的那些再也收不到附加。 | 按左栏，即 F11 P4-04 的 a 方案，加上「告警候选限同 fingerprint」 | 状态转移、F2 步骤 1、F11 PR-4 | |
| C4 | PR-C 的真实运行证据形态 | **A**：在 kind 里用真实 Alertmanager 依次做：注入故障 → firing（`created` 加 Run）→ 撤故障等 resolved → 再注入 → 新 startsAt 的 firing（`attached`，同一事故，Run 数仍为 1）→ 人工 `follow_up` 开新 Run，这个 Run 的 LangSmith trace 里 `investigation_inputs` 含附加事件的 `text` → 关闭事故，第三次注入 → `created` 且 `related_incident_id` 指向前者。附 `summary.json` 与投递、输入行的只读导出。约 3 个告警周期，每个周期至少 `for 2m` 加 rate 5m 的回落，合计约 30–40 分钟，另有 1–2 个真实模型 Run。**B**：只做到 attached 为止，closed→related 只做 PG 确定性测试。代价：省一个周期，但关联路径没有真实证据。 | **A**。与 F2 合同第四节的取证窗口合并，按 A→D→C 串行 | kind 排期、PR-E 可引用 | |

## 3. 决定表：可逆默认（Agent 自定，每项一行理由）

| 编号 | 问题 | 选项与代价 | 推荐 | 影响面 | 裁决 |
|---|---|---|---|---|---|
| R-C1 | advisory 锁键（细化合同 R5） | R5 写的是 fingerprint 加 target；也可以只用 fingerprint | 只用 `"alertmanager:fp:" + fingerprint`。理由：target 在事务外解析，注册表热更新时同一个 `delivery_key` 可能解析出不同目标、拿到不同的锁，这样就失去了旧键对同身份首投的串行保证，只剩主键兜底，会报唯一冲突。只按 fingerprint 加锁，锁的粒度比旧键和 R5 都粗，两种串行都能保证；同一 fingerprint 的争用可以忽略。 | `alerts.py:54-56` | |
| R-C2 | 事务内的锁顺序 | — | firing 路径：advisory 锁 → `_lock_scope`（scope FOR SHARE）→ 不加锁地读候选 → 对选中的开放事故 `FOR UPDATE`，然后复核 `lifecycle<>'closed'`；复核失败就在同一事务里重新选择（此时 scope 已经锁住，转去新建不会出现 incidents→scope 的逆序）。理由：与 PR-2 §9 的 scope→incidents 顺序一致。F11 PR-4 的消费者也应先锁 scope 再锁候选事故，这一点请 PR-4 实现时对齐（F11 R2 原文没有写 scope 锁）。 | 并发、F11 R2 | |
| R-C3 | 哪些投递产生事件 | 每条投递都产生，或只有首投产生 | 只有附加 episode 的首个 firing 投递追加 1 条事件。注解变化、重复通知、resolved 只写投递行。理由：repeat_interval 与注解修订不应向模型灌重复输入；开放事故的 resolved 不属于人工决定。 | 输入量 | |
| R-C4 | 事件内容（落实合同 R6） | — | `input_id = uuid5(_INTAKE_NAMESPACE, "attach:" + delivery_key + ":" + incident_id)`，actor 为投递的 `principal.actor_id`。`content = {channel:"alertmanager", text:<固定英文模板，只含 fingerprint、规范化 startsAt、status>, fingerprint, starts_at, status, annotation_revision, delivery_key}`。注解不进事件，仍按 K2 只进开单 Run 的 alert context。理由：模型只看 text 与 channel；白名单字段都来自已校验的身份（fingerprint 已拒歧义字符且长度 ≤ 256，`alertmanager.py:126-134`）。 | 输入合同 | |
| R-C5 | `related_incident_id` 的列定义与权限 | — | `uuid NULL REFERENCES opspilot_incidents(incident_id)`，`CHECK (related_incident_id <> incident_id)`；只授 web INSERT，任何角色都没有 UPDATE；`RUNTIME_GRANTS` 同步。不加「被关联的事故必须已 closed」的触发器，由应用层判据保证。`_accept_in` 与交接事故的 INSERT 各加一个可选参数 `related_incident_id`，F11 PR-4 复用。 | 迁移、grants | |
| R-C6 | 页面 | — | ① `_alert.html` 投递表加 `starts_at` 列，outcome 为 `attached` 的行标「re-fired episode」。② 事故页在有 `related_incident_id` 时显示 `id="related-incident"` 链接。③ 列表页对「resolved 且有晚于 resolved 的 attached 投递」的事故加标记。列表必须保持一事故一行，靠 C1-A 保证。④ `latest` 注解区标明它属于哪个 startsAt。理由：附加不开 Run，人只能从页面上看到。 | `_alert.html`、`incident.html`、`index.html`、`web/store.py` | |
| R-C7 | 验收投影 | — | `AlertIntakeOutcome` 增加 `related_incident_id: str \| None = None`（读 incidents 行）。deliveries 已含 outcome 与 starts_at，不再加字段。事件由测试经 `read_inputs` 断言。理由：给验收作者一个公开接口，只做加法、有默认值。 | `acceptance_alert.py` | |
| R-C8 | downgrade | — | 有 `outcome='attached'` 的行，或有非空 `related_incident_id` 时拒绝降级（同 0010:106-121 的做法）；否则恢复旧 CHECK 并删列。理由：降级会丢关联。 | 迁移 | |
| R-C9 | 优先级（合同第七节留给 B1/C 中先开工者的问题） | 取最高、最新或首次 | Run 的有效优先级取开单 identity 的 severity。附加不建 identity，所以「每个 Run 一行」天然成立。另外 fingerprint 是整个 label set 的哈希、包含 severity，同 fingerprint 的附加不可能改变 severity（这是 Prometheus 行为，仓库内未核对）。发布事故的优先级由 B1 预审定。 | B1 | |
| R-C10 | 附加时的返回值 | — | `AlertResult.outcome='attached'`，`incident_id` 为被附加的事故，`run_id=None`，不发 `_announce_intake`（附加 key 没有 ledger 行）。HTTP 仍返回 200。 | web | |

## 4. 公开接口草案（推荐，未批准）

```text
# opspilot/persistence/correlation.py（新，纯函数，PR-C 与 F11 PR-4 共用）
CorrelationCandidate(incident_id: UUID, lifecycle: str, created_at: datetime)
Correlation(attach_to: UUID | None, related_to: UUID | None)   # 两者不会同时非空
choose_correlation(candidates: Sequence[CorrelationCandidate]) -> Correlation

# opspilot/persistence/controls.py
append_input_in(conn, incident_id, input_id, content, *, actor) -> int   # 原 append_input 的事务体；append_input 改为包装它
# opspilot/persistence/incidents.py
_accept_in(..., related_incident_id: UUID | None = None)
# opspilot/persistence/alerts.py
record_alert(delivery, *, opening, attach_event: Callable[[UUID], Mapping] | None = None) -> {
    outcome ∈ OUTCOMES ∪ {"attached"}, incident_id, run_id|None, handoff_reason, annotation_revision,
    related_incident_id|None }
# opspilot/acceptance_alert.py
AlertIntakeOutcome.related_incident_id: str | None = None
```

- 错误码：沿用 `IDENTITY_CONFLICT`（派生 ID 相同但内容不同）、`UNKNOWN_TARGET` 与存储故障，返回 `failed`/503 让 Alertmanager 重试。不新增错误码。
- 迁移 `0018_alert_correlation`（`down_revision = 0017_web_suspension_grants`）：
  1. `opspilot_alert_deliveries` 换掉 outcome 的 CHECK，词表为 0010 的 6 个值加 `attached`。约束名默认是 `opspilot_alert_deliveries_outcome_check`，属 PG 默认命名，未在库上核对，迁移里应按 `pg_constraint` 查出再删。
  2. `opspilot_incidents ADD COLUMN related_incident_id`，定义按 R-C5。
  3. `GRANT INSERT (related_incident_id) ON opspilot_incidents TO opspilot_web`，冻结在迁移文件内（同 0017 的写法）。
  4. 不加优先级列（留给 B1 的 0019），不加新表（C1-A）。
- 部署：先迁移，再发 web；worker 不必同步。

## 5. 允许改动的文件

- 实现：
  - `opspilot/persistence/alerts.py`、`opspilot/persistence/correlation.py`（新建）
  - `opspilot/persistence/controls.py`（只抽出 `append_input_in`）、`opspilot/persistence/incidents.py`（只加参数）
  - `opspilot/persistence/grants.py`
  - `opspilot/migrations/versions/0018_alert_correlation.py`
  - `opspilot/web/service.py`（intake、`_alert_view`）、`opspilot/web/store.py`（列表标记、related 读取）
  - `opspilot/web/templates/_alert.html`、`incident.html`、`index.html`
  - `opspilot/acceptance_alert.py`（只按 R-C7 加字段）
  - 新的实现测试
  - 任务记录、`docs/evidence/f2-alert-correlation-live/`
- 由独立验收作者改：`tests/acceptance/test_m1_04_alert_intake.py:300-305`，以及新的 F2 步骤 1 告警关联验收测试。
- 不改：`domain/subjects.py`（规则已在）、调查 loop、`context.py` 与输入版本、worker、Observer、`feature_list.json`、F11 的发布路径（属于 PR-4）。

## 6. 验收测试作者应覆盖的可观察行为（经 `/intake/alertmanager`、PG 与 `alert_intake_outcome`）

1. 同 fingerprint、同目标、新 startsAt，旧事故为 `open`：
   - 结果 `attached`，`incident_id` 与旧事故相同，`run_id` 为空。
   - 事故的 Run 集合、`control_generation`、mode、lifecycle、conclusion 都不变。
   - 恰好多出 1 条 `kind='event'` 输入，模型可见投影只含 channel 与 text。
2. 同 1，旧事故分别为 `observing_recovery`、`resolved`、人工接管（human_owned）、已有结论：都只附加，各项状态不变。
3. 旧事故为 `closed`：
   - 结果 `created`，得到新事故与新 Run，`related_incident_id` 等于旧事故。
   - 旧事故仍为 closed。
4. 多个候选：两个开放事故时附加到 created_at 最新的那个（测试先用旧行为或直接写库造数据）；全部 closed 时关联到最新的那个。
5. 不该附加的情况：
   - 不同 fingerprint、同目标：各自成事故。
   - 同 fingerprint 但解析出不同目标：新事故。
   - 本次解析为交接：交接事故，无关联。
   - 手工开的同目标事故：不参与告警附加。发布开的同目标事故按认领参与：未被其他 fingerprint 附加过的才是候选（#343 U1-A，r1 改写，见 [#343 合同](2026-10-24-343-alert-joins-release-incident-contract.md)）。
6. 幂等：
   - 附加 episode 的重复 firing（含注解变化）：结果 `attached`，事件仍是 1 条。
   - 附加 episode 的 resolved：`resolved_attached`，挂到被附加的事故，不产生事件，状态不变。
   - 503 之后的重试结果相同。
7. 并发：同 fingerprint、两个不同 startsAt 的首投并发 → 恰好 1 个事故，一个 `created` 一个 `attached`；同一 startsAt 的并发保持 `created` 加 `replayed`（现有 `:415-422` 不变）。
8. 竞态：附加与人工 close 并发时，结果只有两种：附加先提交，或 close 先提交后新建关联事故。不会出现附加到已 closed 事故的情况。
9. 挂起（全局或目标）期间：照常附加与新建；挂起解除前没有查询或模型调用。
10. 权限负向：
    - worker 与 Observer 不能写 `related_incident_id`。
    - web 不能 UPDATE 它。
    - 0018 之后 `RUNTIME_GRANTS` 与实际 ACL 相等（`test_pg_runtime_roles_contract.py:197`）。
11. 页面：
    - 投递表显示 starts_at 与 `attached`。
    - 新事故页有指向旧事故的链接。
    - 列表一事故一行；resolved 事故收到附加后带标记。
12. 续开 Run（`follow_up` 或超时续开）后，锚点与 alert context 仍来自开单的那次投递，不被附加 episode 改写；附加事件出现在新 Run 的输入里。
13. 0018 降级：有 attached 行或 related 值时拒绝。

## 7. 是否需要 kind 真实运行

- 需要（F2 合同第六节规定 C 需真实 Alertmanager 再 firing 的 trace），形态按 C4 推荐。
- 断言：
  - 投递行由真实 Alertmanager 产生（actor 为 webhook 主体），三次 firing 的 startsAt 两两不同、fingerprint 相同。
  - outcome 依次为 `created`、`attached`、`created`（第三次带 related）。
  - 附加时 Run 数不变。
  - `follow_up` Run 的 trace 中含附加事件的 text，附 LangSmith 链接与冻结的 `summary.json`。
- 观察窗内不跑 `make check`，注入前查 swap。

## 8. 未确认

- 实验环境里撤掉故障后，告警能否在一个周期内真正 resolved 并在再注入时得到新 startsAt：rate 5m 的回落时间与 `for 2m` 相互作用，没有实测；若告警抖动，可能在预期之外多出 episode（这些也会按规则附加，不影响正确性，只影响取证时长）。
- 「fingerprint 包含 severity，所以同 fingerprint 不变级」依据的是 Prometheus 与 Alertmanager 的标签指纹行为，没在仓库内核对。
- outcome CHECK 的实际约束名没有在库上查。
- F11 PR-4 的消费者若不先锁 scope、就在已持有 incidents 行锁时调用 `_accept_in`，可能与挂起路径逆序；本预审只给出共享约定（R-C2），没有核对 PR-4 的实现。
- 页面列表的「resolved 后收到附加」标记用一条 SQL 还是逐事故读，以及它的性能，未评估；当前列表上限为 `LIMIT %s`。
