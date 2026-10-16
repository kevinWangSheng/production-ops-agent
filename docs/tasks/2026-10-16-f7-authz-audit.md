# F7 只读安全与审计（PR-C：只读鉴权代理、入口枚举、审计重建、passes）

- 状态：进行中
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
- 类别来源：input = `opspilot_inputs`、`opspilot_input_rounds`、`opspilot_alert_deliveries`；query = `opspilot_tool_charges`（`detail` 含 tool、target_id、结果码、是否出站）；evidence = `opspilot_evidence`；state_change = `opspilot_subject_events`、`opspilot_runs` 的创建与结束、影响该事故目标或全局的 `opspilot_suspension_audit`；human_correction = `opspilot_controls`（`detail.action` 为控制动作名，`actor` 为操作员）；recovery_observation = `opspilot_observation_sessions`、`_samples`、`_endings`、`opspilot_self_heal_authorizations`；postmortem_review = `opspilot_postmortems` 各版本与审核、`opspilot_knowledge_entries`。时间一律取 PostgreSQL 写入的时间列。

lab 代理：经 `127.0.0.1:19090` 访问，行为见 A4；`scripts/f7_authz_probe.py` 输出每个请求的方法、路径、账号名、状态码、期望、是否符合。

## 执行进展与证据

（待填）

## lab 计划（需要协调者排期）

（待填）

## 待决

无。
