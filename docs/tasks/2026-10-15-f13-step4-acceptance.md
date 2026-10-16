# F13 第 4 步：验收投影与真实验收（PR-C）

- 状态：进行中
- 更新日期：2026-10-15
- 依据：[F13 第 4 步合同预审 r1](2026-10-15-f13-step4-contract.md)（R8、R9、U2、U6、U8 与「验收运行计划」）；PR-B 记录 [2026-10-15-f13-step4.md](2026-10-15-f13-step4.md)（#238 已合并，`knowledge_checks` 在 PR-B 实现，本 PR 复用）。
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-f13-c`，分支 `feature/F13-step4-acceptance`，基于 `origin/main` `d19e78a`。本机 PG 端口 55593（`tmp/pg`）。

## 目标与范围

合同建议拆分中的 PR-C：新增验收投影 `KnowledgeUseOutcome`（R9）、未参与实现的作者写的验收测试、3 个有界真实 Run 的证据，全部通过后翻转 F13 `passes`（U6 条件 A）。投影零模型、无自有 SQL，只复制产品已提交的记录。

## 公开接口（实现与独立验收测试共同依据）

模块 `opspilot/acceptance_knowledge.py`，并从 `opspilot.acceptance` 再导出 `KnowledgeHintOutcome`、`KnowledgeUseOutcome`、`KnowledgeUseRecords`、`knowledge_use_outcome`、`knowledge_use_records`。

1. **读取**：`knowledge_use_records(incidents, incident_id) -> KnowledgeUseRecords`。`incidents` 只需有 `rebuild(incident_id) -> dict`（即 `IncidentStore.rebuild`，事故页 `Workbench.snapshot` 用的同一个读）；调用一次，原样保存为 `KnowledgeUseRecords(incident_id, rebuilt)`。本模块不写 SQL。投影的对象是该事故的**当前 Run**（`rebuilt["run"]`），与事故页 Run 段落一致。
2. **投影**：`knowledge_use_outcome(scenario: IncidentScenario, records: KnowledgeUseRecords) -> KnowledgeUseOutcome`，纯函数。
3. **`KnowledgeUseOutcome` 字段**：
   - `scenario_id`、`subject_id`（= `scenario.subject_id`）、`run_id: str | None`、`run_state: str | None`。
   - `status`：`ok` / `unavailable`（复制输入快照 `scope_facts.knowledge.status`）；`absent`（输入快照没有 `knowledge`：v1–v4 输入或检索关闭）；`unknown`（见第 5 条）。
   - `unknown_reasons: tuple[str, ...]`。
   - `policy_version`、`retrieved_at`、`max_age_seconds`、`omitted_for_size`：从输入快照逐字复制；`absent`/`unknown` 时为 `None`。
   - `entries: tuple[KnowledgeHintOutcome, ...]`：输入快照里的每条知识，按快照顺序。
   - `report_available: bool`：事故有已发布报告（`rebuilt["conclusion"]` 非空）且 `parse_report` 成功。
   - `run_evidence_ids: tuple[str, ...]`：已发布结论载荷 `conclusion.evidence_ids`（与 `outcome_from_durable` 读的同一字段）；没有已发布结论时为空。
   - `unprovided_tags: tuple[str, ...]`：报告 claim 文本中出现、但不对应任何已提供 `entry_id@r<revision>` 的 `[knowledge:...]` 标签，去重、按首次出现顺序（R8：引用未提供条目视为未处理，在此可见）。
   - `model_requests: tuple = ()`。
4. **`KnowledgeHintOutcome` 字段**：快照 entry 的 `entry_id, revision, name, tags(tuple), content_sha256, source_incident_id, source_postmortem_id, source_version, approved_by, approved_at, observation_ended_at, age_seconds, freshness, truncated` 逐字复制（不含 `content` 正文）；另有
   - `verdict`、`claim_kinds(tuple)`：`opspilot.knowledge.hints.knowledge_checks(fact, parsed_report)` 对这条的结果原样复制——与事故页 `snapshot()["knowledge"]["checks"]` 同源同值。报告不可用时传 `None`（即 `untagged`）。
   - `tagged_claims: tuple[Mapping, ...]`：报告中文本含本条标签的 claim，按报告顺序，每项 `{kind, text, evidence_ids(tuple)}`。
   - `handled: bool`（R8 收紧）：`verdict` 为 `consistent` 或 `contradicted`，且决定该 verdict 的每条带标签 `fact`/`rejected_hypothesis` 的 `evidence_ids` 非空并全部属于 `run_evidence_ids`；`unverified`、`untagged`、`conflicting` 一律 `False`。
5. **主体不一致与无效记录**（同 D11 写法：整份 `unknown`，不部分投影，`entries=()`、`run_evidence_ids=()`、`unprovided_tags=()`、`report_available=False`）：
   - `SUBJECT_MISMATCH`：`records.incident_id`、`rebuilt["incident_id"]`、`rebuilt["run"]["incident_id"]` 任一与 `scenario.subject_id` 不等（按字符串比较）。
   - `INVALID_KNOWLEDGE_FACT`：快照 `knowledge` 存在但 `opspilot.knowledge.hints.valid_knowledge` 为假。
6. 投影不调用模型、不发遥测、不读环境变量；不改变 `knowledge_checks` 或报告校验的运行时行为（R8：运行时不因标签拒绝报告）。

## 验收判定（真实 Run 用，见「验收运行计划」）

- Run 1 知识一致：`status == "ok"`，entries 含 K1 当前 revision 且 `freshness == "fresh"`；该条 `verdict` 照实记录，期望 `consistent` 且 `handled`。
- Run 2 撤销后变化：entries 不含被撤销的 `(entry_id, revision)`；如有替换，含新 revision。
- Run 3 陈旧且矛盾：目标知识 `freshness == "stale"`；该条 `verdict == "contradicted"` 且 `handled`；`claim_kinds` 不含 `fact`；`unprovided_tags == ()`；事故页 `#knowledge-hints` 显示陈旧与「被否定」。未核对（`unverified`/`untagged`）不算通过。

## 执行进展与证据

- 实现：Codex `gpt-6.1-sol` 按上节接口实现 `opspilot/acceptance_knowledge.py`（加实现者自测）。保守取舍：`unknown` 时 `run_id`/`run_state` 也置空；主体不一致与知识无效并存时报 `SUBJECT_MISMATCH`；`unprovided_tags` 记完整标签原文；报告无法解析时仍保留已发布结论的证据 ID，知识结果为 `untagged`。
- 独立验收测试：未参与实现的 Claude 子 Agent 只按本记录与合同编写 `tests/test_f13_step4_acceptance.py`（纯内存）与 `tests/integration/test_f13_step4_acceptance_postgres.py`（真实 PG、真实工作台、桩模型输出带标签报告；页面与投影逐字段一致；撤销后后继 Run 不含被撤销 revision），实现未改其断言。本机：纯内存 24 passed，PG（55593）3 passed。
- 独立审查：Codex `gpt-6-astra` 首审投影本身无 P1/P2；判定脚本 2 个 P2（`changed` 可省略被撤销 revision 而空过；页面子串匹配会把别的条目 r20 当成 r2）。已改为必填参数、按链接取 entry_id 并整行解析字段逐项比较，resume 复验确认解决、无新增。
- 检查：`make check`（共享锁）6134 passed、1521 skipped、2 xfailed。
- 真实验收：需要 kind lab 时段，见下节。

## 验收运行计划

按合同「验收运行计划」真实层执行，判定脚本 [`knowledge_use.py`](../evidence/f13-step4-acceptance/knowledge_use.py)（投影 + 事故页 `#knowledge-hints` 逐条比对 + 上节判定，`passed` 为真才算通过），人工动作脚本 [`workbench.py`](../evidence/f13-step4-acceptance/workbench.py)（`ask` 网页提问、`revoke` 知识页撤销，都走工作台 HTTP 与 Basic Auth）。summary.json 与 trace 沿用 [m1-clean-rerun](../evidence/m1-clean-rerun/run.md) 的 `collect.py`（`--scenario S1`，恢复字段不作判定）。

- **起点**：#247 干净重跑库 `production-ops-agent/tmp/m1-clean/pg`（已停）的副本。库里 K1 = `4eb40edf-a9ca-44a3-90cd-5f62b3daf8ab` r1（active，目标为事故 `002c3fba` 的目标，`observation_ended_at` 2026-10-15T18:24:31Z）。副本放本 worktree `tmp/lab-pg`，端口 55593（先停本线测试库），schema 应已是 head（`opspilot.schema check` 核对；本 PR 无迁移）。web/worker/Observer 启动脚本照 `tmp/m1-clean/run-*.sh` 改端口与目录（私有 env 文件只引用，不复制进仓库）。代码为本分支 HEAD。
- **前提**：`kind_lab.py health` 全部 ok、所有 flag off；`vm.swapusage` 检查，观察窗内不跑 `make check`/全量 pytest/新 Codex。
- **顺序 1 → 3 → 2**（见待决 1）：
  1. **知识一致**（故障窗 1）：`kind_lab.py fault inject --experiment-id <新 id>`（flagd `paymentFailure`）→ 告警建事故 I2 → Run 完成后 `knowledge_use.py --scenario consistent --entry K1 --revision 1`；verdict 照实记录。随后 `fault restore`，等 checkout 5 分钟窗口回到健康（约 6–8 分钟）。
  2. **陈旧且矛盾**（无故障）：以 `OPSPILOT_KNOWLEDGE_MAX_AGE_SECONDS=3600` 重启 web（K1 年龄已远超 1 小时）；`workbench.py ask --like-incident 002c3fba… --question "Are checkout payments failing right now? Check the PlaceOrder error ratio over the last 10 minutes."` → 事故 I3 → Run 完成后 `knowledge_use.py --scenario stale-contradicted --entry K1`。
  3. **撤销后变化**（故障窗 2）：恢复默认阈值重启 web；`workbench.py revoke --entry K1 --reason "F13 step 4 acceptance: knowledge changed"` → 再注入 → 告警建事故 I4 → `knowledge_use.py --scenario changed --entry K1 --absent-revision 1`；恢复故障。
- **预计时长**：准备 10 分钟 + Run 1 约 12 分钟（注入到告警约 7 分钟，调查 1–2 分钟）+ 恢复等待 8 分钟 + Run 3 约 5 分钟 + Run 2 约 12 分钟 + 收尾 10 分钟，合计约 1 小时，**2 个故障窗**；模型调用 3 个调查 Run（无复盘生成），按以往约 0.1–0.3 CNY。
- **证据**：`docs/evidence/f13-step4-acceptance/` 下每个 Run 的 `knowledge-use-*.json`、`summary.json`、trace 链接、余额差额；原始 ledger 不入库。某步失败照实记录，不为通过反复重跑。
- **已知风险**：#247 阶段 B 的报告没有出现 `[knowledge:…]` 标签（冻结摘要检索为空），真实模型可能不按约定打标签；Run 3 若为 `untagged`/`unverified` 即判失败（R8、U2「照实记录，不为通过而放宽」），`passes` 不翻。

## 待决

1. **真实 Run 顺序 1 → 3 → 2**：合同写「按顺序执行」，并要求 Run 3 前「在 D1 库中保留一条 active 知识，或用 I2 批准产生的新知识」。起点库只有 K1 一条知识，先撤销再做 Run 3 就没有可用知识；改走 I2 批准则要等 I2 恢复确认与复盘生成（多约 20 分钟），且复盘提议可能替换 K1，结果不可控。推荐把 Run 3 提到撤销之前，三个 Run 的断言不变。

## 下一步与交接

需要 kind lab 时段（约 1 小时、2 个故障窗），由协调者排期后按上节执行；通过后翻 F13 `passes` 并补证据。
