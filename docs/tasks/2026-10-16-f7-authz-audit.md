# F7 只读安全与审计（PR-C：只读鉴权代理、入口枚举、审计重建、passes）

- 状态：五步验收有证据，F7 `passes` 已翻（待用户合并）
- 更新日期：2026-10-16
- 依据：`feature_list.json` F7 第 1、5 步（并汇总 2-4 步证据后翻 `passes`）；[F7 合同预审 r1](2026-10-15-f7-contract.md) U1-a、U3-a、U4-a、R2、R3、R5、R6；C3 §8（l.306-308 鉴权代理/独立实例）、§11 审计；[OTLP 写入探测](../evidence/f7-otlp-probe/run.md)（两个产品账号都能写 → 走 U3-a）
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-f7-c`，分支 `feature/F7-authz-audit`，起点 `origin/main`（含 PR-A #239、PR-B #253）；本地 PostgreSQL 17 端口 55595（`tmp/pg`）

## 目标与范围

合同第 5 节 PR-C：lab Prometheus 前加只读鉴权代理（U3-a）；第 1 步入口枚举测试与 kind 探测证据（S1b、S1d、S1f）；U1-a 静态测试并关闭 #92；R2 审计重建投影加第 5 步验收（R3 数据）；五步证据齐后单独一个提交翻 F7 `passes`。

不在本 PR：#197（U2-a，C3 §3 已知差距）、按目标累计上限（U4-a）、Kubernetes 工具（U1-a）。

## 合同预审（PR-C 实施前，lead 对照 `origin/main` 代码、`scripts/kind_lab/` 与 OTLP 探测证据）

无新增需用户裁决项：可能的合同层问题都已被 r1 或既有决定覆盖——
- C3 §8 l.308「目标隔离用真实鉴权代理或独立实例」：M1-01 已定为「一个目标一个专属实例」（[2026-09-26-m1-01-otel-tools.md](2026-09-26-m1-01-otel-tools.md) 第 2 条），所以代理只承担只读（路径/方法白名单），不做按 label 的目标隔离。
- 第 5 步数据跨两个事故：F12 live 库是 M1 干净重跑库的副本加 F12 场景，干净重跑事故覆盖恢复与复盘审核，F12 事故 `e1ac6e74` 覆盖人工纠正；R3 已裁决「D1 只能作补充」，即接受这种组合。

可逆默认（Agent 自定，一行理由）：

| 编号 | 决定 | 理由 |
|---|---|---|
| A1 | 代理用固定版本的 nginx alpine，集群内 Deployment + Secret 配置 | 不加 Python 依赖；精确 location 白名单最容易审计 |
| A2 | 代理 Service 占 NodePort 30090（宿主 19090），Prometheus Service 改 ClusterIP；产品 URL 与环境变量不变 | kind `extraPortMappings` 只能在建集群时定，复用 30090 免重建；宿主上不再有直连 Prometheus 的路径，比 U3-a 原文「换 URL」更严 |
| A3 | Prometheus `web.yml` 只留 `collector`、`proxy`；代理认证 `investigator`、`observer`、`lab`；auth 文件缺 `proxy` 时只补它 | U3-a 原文；不让已发的 env 文件失效 |
| A4 | 只放 GET/HEAD 的 `/api/v1/query`、`query_range`、`series`、`labels`、`label/<name>/values`、`status/buildinfo`；其余 403、未认证 401；上游用规范化路径并替换 Authorization | 产品只用 GET `query`/`query_range`（`observer/prometheus.py:194,300`、`tools/otel_demo.py:1064`）；精确匹配防路径变形 |
| A5 | 第 1 步「已启用入口」= web 全部路由、worker/observer/schema/replay 的 `__main__`、工具注册表；静态断言无 Kubernetes/GitHub/Argo 客户端或凭据变量（U1-a） | 合同 S1b、S1e |
| A6 | 审计投影接口见下节；只读，旧数据缺来源记入 `missing` | R2、R6 |
| A7 | 第 5 步真实数据：F12 live 库（原目录 `poa-f12-close/tmp/live/pg`，在副本上启动、只读事务） | R3；不改原库 |
| A8 | 第 4 步 kind 真实 Run（S4c）尚无证据，列入本 PR 的 lab 计划；`passes` 等它 | `passes` 需五步全部有证据 |

## 接口（供验收测试作者）

`opspilot/acceptance_audit.py`：

- `AUDIT_CATEGORIES = ("input", "query", "evidence", "state_change", "human_correction", "recovery_observation", "postmortem_review")`
- `AuditEntry(recorded_at: datetime, sequence: int, category: str, source: str, record_id: str, run_id: UUID | None, actor: str | None, detail: Mapping[str, object])`，frozen；`source` 是表名；`detail` 只含 id、枚举和计数，不含自由文本。
- `AuditTimeline(incident_id: UUID, entries: tuple[AuditEntry, ...], counts: Mapping[str, int], missing: tuple[str, ...])`；`entries` 按 `(recorded_at, 来源顺序, record_id)` 排序，`sequence` 为排序后的位置（0 起）；`missing` 是计数为 0 的类别。
- `audit_timeline(conn, incident_id: UUID) -> AuditTimeline`，`conn` 与现有 PG 投影同类型；未知事故抛 `LookupError`；只发 SELECT，可在 `READ ONLY` 事务内运行。
- 类别来源：input = `opspilot_inputs`、`opspilot_input_rounds`、`opspilot_alert_deliveries`；query = `opspilot_tool_charges`（`detail` 含 tool、target_id、结果码、是否出站）；evidence = `opspilot_evidence`；state_change = `opspilot_subject_events`、`opspilot_runs` 的创建与结束、影响该事故目标或全局的 `opspilot_suspension_audit`；human_correction = `opspilot_controls`（`detail.action` 为控制动作名，`actor` 为操作员）；recovery_observation = `opspilot_observation_sessions`、`_samples`、`_endings`、`opspilot_self_heal_authorizations`；postmortem_review = `opspilot_postmortems`、`opspilot_postmortem_versions`、`opspilot_f13_audit`（迁移 0008 的复盘、版本与审核审计，D1 = A 澄清）、`opspilot_knowledge_entries`。时间一律取 PostgreSQL 写入的时间列。

lab 代理：经 `127.0.0.1:19090` 访问，行为见 A4；`scripts/f7_authz_probe.py` 输出每个请求的方法、路径、账号名、状态码、期望、是否符合。

## 执行进展与证据

- 2026-10-16：PR-B #253 合并后从 `origin/main` `4aebf75` 建 worktree 与 PG 55595；基线 F7 第 2-4 步 PG 验收 10 passed。
- 实现（Codex `gpt-6.1-sol`）：代理用 `nginx:1.28.0-alpine`；`proxy` 账号只补不重生成；代理配置与 htpasswd 经私有临时文件进 Secret，配置哈希变化滚动 Pod；投影内部自带 `dict_row` 游标，不依赖调用方连接。保守选择：没有自身数据库时间的行用标明来源的关联记录时间（如步骤的写入时间），找不到锚的计入冻结摘要 `undated`，不静默丢弃。
- lead 机械修正：`tests/test_m1_prometheus_credentials.py` 两条旧断言（web.yml 列出全部账号、4 个密码）按 A3 改为「只列 collector 与 proxy」「5 个密码」。
- 验收测试：全新上下文 Claude 子 Agent 只看合同、本记录接口与公开接口写 `tests/acceptance/test_f7_step1_entry_points.py`、`test_f7_step5_audit_postgres.py`（含 U4-a 单次上限）；首跑第 5 步 11 failed（投影假定 dict 行），实现修复后全部通过，未改断言。
- 验收断言放宽与恢复（照实记录）：测试作者首轮遇到投影要求 dict 行（实现缺陷，已在实现侧修，测试保持普通连接）后，看到实现输出把两处断言放宽——复盘来源允许集合加入 `opspilot_postmortem_versions`、`opspilot_f13_audit`、`opspilot_knowledge_revisions`；恢复观察相对登记处置的次序改为只要求采样与结束严格晚于登记、会话行只需晚于此前的纠正。放宽版本随 `c1b9e1d` 提交。协调者要求恢复，作者已恢复原写法；恢复后 2 failed / 22 passed，两处分歧列入下方「待决」决定表，实现与测试都不再改，等裁决。
- 测试作者列出的其余不清处由 lead 按可逆细节定：`tool_charges`、`runs`、`input_rounds` 无自身时间列，取关联记录的数据库写入时间并在 `detail.time_basis` 标明（`steps.observed_at` 默认 `clock_timestamp()`，0001 l.82）；UI 接收的输入由 `input_rounds` 体现；query 的 `detail` 键名以实现为准（测试只断言 `tool`、`target_id`）；第 5 步不挂 `PRODUCT_CONSTRAINT_SCENARIO_INDEX`（需改 `opspilot/acceptance.py`，超出本 PR）。
- 独立审查（Codex `gpt-6-astra`，会话 `01a1451c`）：P2×2——取消等非 completed 终态的 Run 无结束条目；探测矩阵漏 `lab` 账号与 HEAD/OPTIONS/路径变形。修复后 resume 复验两项已解决，另报新 P2：路径变形把 5xx 当拒绝。lead 自修为只认 4xx（301/0/5xx 均判失败）并补参数化用例，自核通过。审查明确：nginx alpine 是否认 `$2y$` bcrypt、真实路径行为、已有 lab 升级顺序与 collector 写入只能在 lab 验证。
- 第 5 步真实数据（R3）：用户 2026-10-16 批准复制 F12 live 库；协调者复制到 `tmp/f12-live-pg`，lead 在 55596 只读启动，重建 4 个事故，跑完停库。`002c3fba`、`e1ac6e74` 七类齐全，证据 [f7-audit-rebuild](../evidence/f7-audit-rebuild/run.md)（D1、D2 待决）。
- 用户裁决（2026-10-16，协调者转达）：D1 = A（接口改为：postmortem_review 来源是 0008 的 `opspilot_postmortems`、`opspilot_postmortem_versions`、`opspilot_f13_audit` 与 `opspilot_knowledge_entries`，`source` 报真实表名）；D2 = A（同一事务写入的行不定义先后，会话行只要求不早于此前的纠正，采样与结束严格晚于登记）。两项都不改实现。原测试作者按裁决收紧：允许集合加入两张 0008 表（未加 `opspilot_knowledge_revisions`），加回审核人与生成者可归属断言；次序断言按 D2 改写，保留采样与结束严格晚于登记、二者必须出现。第 5 步 PG 验收 24 passed（PG 55595）。
- 用户批准复制 F12 live 库（2026-10-16）。
- lab 时段（协调者批准，2026-10-16 09:34–10:04 PDT，持共享重负载锁，注入前 swap 11.9/13.3 GB、checkout rate 0.0125/s、p95 193 ms）：
  - 部署代理：本分支 `kind_lab.py up` 成功，`health` ok，span 样本持续写入。共享 lab 目录以符号链接接入 worktree，auth 文件只补 `proxy`。
  - 第 1 步探测：运行 1 `ok: false`（HEAD 读路径期望 200 实得 405，是探测期望错误：代理放行 HEAD，Prometheus API 不处理 HEAD）；改期望为 405 与单测替身后重跑一次，运行 2 `ok: true`。两份输出都入库，见 [f7-authz-probe](../evidence/f7-authz-probe/run.md)。
  - 第 4 步真实 Run：全局与按目标暂停各一次，窗口内新增查询 0、在途轮写回 `late_result`、解除后不自动恢复，`ok: true`；费用 0.48 CNY；见 [f7-step4-live](../evidence/f7-step4-live/run.md)。故障已恢复、flag 全部 off，web/worker 与 PG 55597、55596 已停，锁已释放。
  - 兼容：本 PR 合并前，main 版 `kind_lab.py` 在未设 lab 账号环境变量时会因 auth 文件有 5 个账号而退出，main 版 `up` 也会拒绝；F11 U2 harness 与产品 env 经代理可读。详见探测记录。
- PR 机器人（`@codex review`，一次）两项 P2 采纳：事故创建前已生效的暂停补一条基线审计行（`detail.baseline`）；`Runner._blocked` 的 `run_handoff`/`execution=blocked` 记为 Run 结束。实现者修，lead 自核；F12 live 库无暂停审计与 blocked 交接行（只读查得 0、0），第 5 步真实数据摘要不受影响。
- 机器人对 `b283350` 自动复审又报一项 P2（知识条目审计行未按本事故的修订归属，会把其他事故的审核动作算进来），采纳：按条目、revision 与来源复盘归属，无 revision 的知识审计行保守排除；补两事故共享条目的单测。lead 处置时误把该线程与前两条一起 resolve，修复后补回复。重跑 F12 live 副本的只读重建，四个事故计数与条目 sha256 与已冻结摘要一致。
- 机器人对后续推送自动复审再报四项同类 P2（观察会话、复盘版本、自愈授权、复用知识条目：可变的最终状态配不可变的创建时间，造成回溯错位），一次性采纳：创建条目去掉可变状态，转移由各自带时间的审计行表达；自愈授权另加按 `updated_at` 的最新快照条目；复用知识条目按本事故修订的 `approved_at` 计时。第 5 步真实数据按 `7a497a6` 重冻结（仅 recovery_observation 计数各增 1-2）。按停机规则，此后机器人新发现只按类分诊，不再为其推代码。
- `make check`：98d7c62 上 8700 passed；合并 main（#257、#259）与机器人修复后 3975bb1 上 8720 passed、55 skipped、2 xfailed（PG 55595，共享锁）。CI 曾因投影里的 `SELECT *` 被 `test_sql_column_order_independence` 拒绝而失败，改为显式列后通过（本机此前的 make check 早于该改动）。

## lab 计划

已于 2026-10-16 执行，结果见「执行进展与证据」。

## 待决

### 第 5 步验收断言与接口分歧（用户 2026-10-16 裁决：D1 = A，D2 = A）

| 编号 | 分歧 | 选项与代价 | 推荐 |
|---|---|---|---|
| D1（裁决 A） | 合同（R2：「组合三个现有投影加 suspension_audit 和 tool_charges」）与本记录接口都没点名 `opspilot_f13_audit`；接口写 postmortem_review 来源为「`opspilot_postmortems` 各版本与审核、`opspilot_knowledge_entries`」并规定 `source` 是表名，而版本与审核动作实际存在 `opspilot_postmortem_versions` 与 `opspilot_f13_audit`（迁移 0008）。实现报真实表名，测试按字面两表断言 → 来源 `opspilot_f13_audit`（action=generate）的条目让 `test_s5b_entries_are_ordered...` 失败。合同没禁止这些表，属于措辞缺口 | A. 允许 0008 的版本表与审核审计表作为来源：改本记录接口，测试作者按新接口改允许集合并加回审核人/生成者可归属断言。代价：一次接口修订；收益：审核谁做的、何时生成都可回溯。<br>B. 实现不读这两张表，只报 `opspilot_postmortems` 与知识条目：符合字面，但复盘「审核」本身（审核人、批准动作）从时间线消失，第 5 步「postmortem review」只剩结果没有动作；真实库摘要需重跑 | A |
| D2（裁决 A） | R6 规定只按 PostgreSQL 写入时间排序；本 PR 的验收另要求因果次序（恢复观察不早于登记处置）。观察会话行与登记控制行在同一事务写入，会话行 `clock_timestamp()` 早 0.7–1.3 ms → `test_s5b_causal_order_holds` 失败。两条要求对同一事务内的行冲突 | A. 同一事务内的行不定义因果先后：会话行只要求不早于此前的纠正，采样与结束必须严格晚于登记。代价：测试按此改写（由测试作者）；不动产品与投影。<br>B. 投影对同一事务的行按事务内位置排序：需要读事务标识（`xmin`）并定一个事务内顺序规则，复杂，且 `xmin` 跨回卷不可靠。<br>C. 产品改为先写控制行再写会话行：动产品存储（`ObservationStore.register_remediation`），超出本 PR 范围，需另开 PR，在途库不受影响 | A |

### 其他

无。
