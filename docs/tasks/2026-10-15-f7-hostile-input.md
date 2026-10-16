# F7 只读安全与审计（PR-B：敌意输入与数据源秘密）

- 状态：进行中
- 更新日期：2026-10-15
- 依据：`feature_list.json` F7 第 2、3 步；[F7 合同预审](2026-10-15-f7-contract.md)（r1，含 #236 分诊补充）第 5 节第 2 条；C3 §5（l.193-195 revision 与 blocked）、§12 数据出口（「凭据不得进入模型上下文、工具结果、报告或 trace」）；PRODUCT-CONSTRAINTS「Credentials and secret-bearing raw inputs must not enter prompts or exported traces」
- 工作区：worktree `/Users/shenghuikevin/dev/AI/poa-f7-hostile`，分支 `feature/F7-hostile-input`，起点 `origin/main` @ `d19e78a`；本地 PostgreSQL 17 端口 55592，数据在 worktree `tmp/pg`
- 前序：PR-A（#239，暂停入口）已合并；F13 第 4 步（#238，已批准知识进入调查输入）已合并

## 目标与范围

U7-a：数据源返回内容里的凭据在进入模型、trace 和持久化之前脱敏；bump 工具视图 revision；第 2 步（日志注入、知识注入）与第 3 步（模型输入、trace、`summary.json`、web 证据页）验收的确定性部分。

不在本 PR：Observer/发布观察自己存的原始读数（0003、0015 表）的存储脱敏（见待决 1）；PR-C 的入口枚举、只读代理、审计重建与翻 `passes`；真实模型 Run（R4，需协调者排时段）。

## 合同（r1）

- U7-a + r1 补充：工具视图（span log 值、指标标签值）经 `redact_credentials`；证据接口与导出同时返回 `record.raw`，所以秘密在**持久化前**脱敏，断言针对存储与导出表示。r1 补充取代 U7-a 原文「PostgreSQL 里的原始证据保持不变」。
- 第 3 节第 6 类：bump revision，在途 Run 重新领取时进 `blocked(INCOMPATIBLE_STATE)` 是预期；已归档证据不回写。
- U6-a：第 2 步「runbooks」= 已批准复盘知识（F13 第 4 步）。U8-a：确定性判定，已执行查询都在授权目标与窗口内、无变更；模型提出的越权请求被拒且可见，不算失败。R4：日志注入走 span log `exception.message`，真实 Run 用回放传输，不用 kind。

## 实现选择（可逆，lead 自定，各一行理由）

- 脱敏位置：执行器在结果校验通过后、建记录前，对整个解析后的载荷逐个字符串值过 `redact_credentials`，键名是凭据名（与 `redact_credentials` 同一套名称规则）的字段，非空值（字符串、数字、对象）整体换成标记；适配器给的附加视图字段同样处理。理由：一处覆盖所有已登记工具，raw、view、模型消息、trace、web 页同源。
- 键名感知：合同原文只写「值过 `redact_credentials`」，但 `{"api_key": "…"}` 这种结构化标签单看值无法判定；按名称规则补上。理由：与 prose 规则同一定义，取保守。
- raw 存储形态：没有任何值被改时 raw 保持源字节不变；有改动时存脱敏后的规范 JSON，`raw_sha256` 对存储字节计算。理由：r1 允许「脱敏或省略」，脱敏保留可检查性；无命中时零行为变化。
- trace 适配器：`exception.message` 等明细值先脱敏再按 600 字节截断，operation 名先脱敏再截 150 字符。理由：先截断会留下 `Bearer` 后不足 8 字符的令牌片段，规则匹配不到。
- 对象键：键文本同样过 `redact_credentials`；两个键脱敏后相同时，后者加 `#2`、`#3` 后缀，不互相覆盖。理由：URL 键、Jaeger 服务名转成的计数键也是数据源文本（审查 P1）。
- web 证据页：对 `raw_utf8` 与 view（恢复读数还有 value、query）再做一次展示时脱敏，无命中时原样，覆盖本 PR 之前存的旧记录和恢复读数。哈希与 `hashes_verified` 仍针对存储字节；字段 `display_redacted: true` 只在展示内容被脱敏时出现，表示显示内容经过展示脱敏、不再对应该哈希（审查 P2）。理由：旧证据不回写（第 6 类），展示层兜底。
- revision：`PROJECTION_REVISION` → `m1-01-tool-view-v8`；otel-demo 的 `TOOL_SCHEMA_REVISION` 哈希纳入 projection revision；fixture 档 `fixture-2` → `fixture-3`。理由：只改 `PROJECTION_REVISION` 不进 `versions`，在途 Run 不会被拦，违背第 6 类。
- 已知限制（沿用 `redaction.py` 原文）：没有命名上下文的裸秘密字符串判定不了；验收用的合成秘密限于规则声明覆盖的形态（`name=value`、`name: value`、`Bearer`/`Basic`、URL userinfo、凭据名键）。

## 接口（验收测试作者使用）

- `opspilot.redaction.REDACTED_CREDENTIAL` 是唯一标记。
- 工具结果：已采纳与只作历史的 `EvidenceRecord` 的 `raw`、`view`（含 `content`）、`ToolOutcome.model_view` 中不含上述形态的秘密原值；`raw_sha256 == sha256(raw)`。无命中时 `raw` 与传输返回字节逐字节相同。
- `opspilot.tools.outcomes.PROJECTION_REVISION == "m1-01-tool-view-v8"`；`opspilot.tools.fixture.TOOL_SCHEMA_REVISION == "fixture-3"`；otel-demo 的值与 bump 前不同。
- web `GET /incidents/{id}/evidence/{evidence_id}`（JSON 与 HTML）返回的 `raw_utf8`、`view` 不含秘密原值；只有展示时做过脱敏（旧证据、恢复读数）才带 `display_redacted: true`。

## 执行进展与证据

- 2026-10-15：建 worktree 与本地 PG（55592）；基线 `tests/test_m1_tool_boundaries.py`、`tests/integration/test_m1_otel_demo_contract_postgres.py` 111 passed。
- 实现（Codex `gpt-6.1-sol`）：`redaction.redact_payload` / `redact_json_bytes`，执行器 `_inspect` 统一脱敏（采纳与历史两条路径），trace 明细先脱敏后截断，revision bump，web 展示兜底。lead 自改一处：凭据名字段的非空值（不止字符串）整体替换，同步实现者单元测试的一个期望。
- 验收测试（全新上下文 Agent，未看实现）：`tests/acceptance/test_f7_step2_hostile_input_postgres.py`、`test_f7_step3_source_secrets.py`、`test_f7_step3_source_secrets_postgres.py` 及两个 support 模块，挂 `test_product_constraints_checklist.py`。替身模型经真实 `DeepSeekClient` + 脚本化 opener，无真实调用。实现后 15 passed；去掉实现（`git apply -R`）后 8 failed / 4 passed（失败的全是脱敏与 revision 用例，注入类原本就守得住）。
- 独立审查（Codex `gpt-6-astra`，会话 `01a142f6`）：P1 对象键里的凭据（URL userinfo 键、Jaeger 服务名转成的键）未脱敏，审查者经执行器复现；P2 web 展示脱敏后 `hashes_verified=true`，但显示内容并不对应该哈希。lead 修复：键文本脱敏加冲突后缀；加 `display_redacted`；补 3 个回归测试。复验：P1、P2 已解决；新 P2：已脱敏的键含标记里的 `CREDENTIAL` 一词，第二次脱敏（web 展示）把该键的计数整组抹掉。修复为：判定凭据名前先去掉标记本身。第二轮复验确认已解决，同时指出：如果直接让含标记的键免检，源数据只要伪造一个 `api_key_[REDACTED_CREDENTIAL]` 键就能逃过检查（新 P1）。现在的做法（去掉标记后照常检查）两种情况都覆盖，各有一个回归测试。
- `make check`（f79550b）：1 failed——`test_f12_pr1_acceptance_postgres.py::test_evidence_page_is_html_for_browsers_and_json_otherwise` 固定了证据页 JSON 的键集合，`display_redacted` 总是出现会让它失败。改为只有展示内容确实被脱敏时才带这个字段（持久化时已脱敏的新证据没有它），F12 的测试不用动。第三轮复验（e03a2d3）：P1 已解决，无新 P1/P2。
- `make check`（e03a2d3，本地 PG 55592，共享锁）：ruff、format、mypy 通过；pytest 7584 passed、54 skipped、2 xfailed
- P2 的性质（协调者要求核对）：新证据在执行器 `_inspect` 里、`register` 之前已脱敏，PG `opspilot_evidence.raw/view` 存的就是脱敏后字节，`hashes_verified` 对它成立、`display_redacted` 为 false；验收 `test_s3c_s3d_source_secrets_are_absent_from_every_input_store_and_export` 直接查 PG 表零命中。审查者复现 P2 用的是恢复读数（Observer 存储，不在 r1 点名范围，见待决 1）和本 PR 之前写入的旧工具证据——只有这两类会触发展示时脱敏，标记只用于它们。
- 测试作者提出、lead 处置：越权请求有的是 `denied`（目标参数、未登记工具），有的是 `error/INVALID_PARAMS`（窗口越界、offset），U8-a 只要求「被拒、可见」，两者都接受；适配器内拒绝也计入 `tool_operations_used`（现有行为，不在本 PR 范围）；`summary.json` 由调用方组装，测试按现有脚本做法组装。

## 真实 Run 计划（待协调者排时段）

- 脚本 `scripts/f7_hostile_live.py`（Codex 写，lead 复跑 `--dry-check` 退出 0：20 项判定中 18 项 pass，trace 两项因 dry 模式记为 skipped，秘密命中全为 0）。一次真实 DeepSeek Run，数据源用回放（R4，不用 kind）。span log 注入、已批准知识注入、合成秘密同时出现在这一次 Run 里。
- 命令：`OPSPILOT_LAB_DSN=<55592> M1_DURABLE_POSTGRES=1 OPSPILOT_TRACE=lab M0_ENV_FILE=<凭据文件> .venv/bin/python scripts/f7_hostile_live.py --lab-round f7-hostile-<日期> --deadline-seconds 720 -o tmp/f7-hostile-live-report.md`。Run 本身 2–12 分钟，trace 回读另需约 1–2 分钟。费用记供应商余额差。
- 判定：U8-a 账本断言；模型请求、重建 transcript、PG raw/view、web JSON/HTML、LangSmith 回读的全部子 run、`summary.json`、ledger 中合成秘密零命中。冻结到 `docs/evidence/f7-hostile-input-live/`，提交前跑 gitleaks。

## 待决

1. Observer 与发布观察存的原始读数（`0003` readings、`0015` query raw）不在 r1 补充的点名范围（工具证据的 `record.raw`），本 PR 只在 web 展示层兜底，不改存储。是否另开 issue 由协调者定。

## 下一步与交接

- 实现（Codex `gpt-6.1-sol`）与验收测试（全新上下文 Agent）并行 → 独立审查（Codex `gpt-6-astra`）→ `make check` → 开 PR（用户门，不合并，标注真实 Run 待补）。
- 真实 Run 计划见 PR 与给协调者的报告。
