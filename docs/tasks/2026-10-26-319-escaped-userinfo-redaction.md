# #319 共享 userinfo 脱敏覆盖 JSON 转义写法的 URL authority

- 状态：就绪待合并（实现、真实 Run、独立验收测试、独立审查完成）
- 更新日期：2026-10-26
- 依据：issue #319 的 Agent Brief（实现合同）；F7 合同第 3 节第 6 类（[f7-contract](2026-10-15-f7-contract.md)）；C3 §5 投影版本条款（#361）；#315 记录 [f7-observer-redaction](2026-10-22-f7-observer-redaction.md)
- 工作区：`feature/F7-319-escaped-userinfo-redaction`，`/Users/shenghuikevin/dev/AI/poa-319-impl`（起点 origin/main c968783）

## 目标与范围

共享规则 `redact_credentials` 的 URL userinfo 规则原先只认字面 `//`。仍是 JSON 转义写法的 authority（非 JSON 体、截断 JSON、字符串值里再套一层 JSON）会漏网。本次在共享规则里补一条独立规则，同步 Observer 截断路径，并升模型视图投影版本。范围外以 brief 为准：百分号编码、双重转义、HTML 实体（#359）；trace 兜底 `_scrub`（#357）；tracing、web、告警持久化、executor、上下文组装、迁移、grants、`feature_list.json`；不删 #315 的 `_ESCAPED_USERINFO`。

## 合同（r1）

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| U1-B | 两个转义斜杠，每个是 `\/` 或 `\u002f`/`\u002F`，可混写，有无 scheme 都算；Observer `_OPEN_AUTHORITY` 同步认 `\u002f`；其他编码另开 issue | 用户 2026-10-26 |
| U2-A | `PROJECTION_REVISION` → `m1-01-tool-view-v11`，fixture → `fixture-5`；8 个独立验收文件的固定值由独立测试作者改 | 用户 2026-10-26；C3 §5（#361） |
| U3-A | 一次有界回放真实 Run（`scripts/f7_hostile_live.py`，不占 kind），三种形态在各落点零命中，`versions` 显示 v11 | 用户 2026-10-26 |

注：issue 正文与 brief 在 GitHub 上把 `\u002f` 显示成了 `/`（转义被解码），本记录与代码按预审原文 `\u002f`/`\u002F` 实现。

## 实现决定（可逆，Agent 自定）

- 新规则 `_ESCAPED_AUTHORITY_USERINFO` 单独成条，与字面规则同在 `redact_credentials` 第一步、字面规则之后执行：字面规则与其输出逐字不变；放在 assignment 之前，`token:` 不再吞掉 host 与路径。
- userinfo 类排除空白、`/`、`@`、`\`、`"`，possessive；host 类排除空白、`/:?#\"`：匹配不跨 JSON 字符串，单遍线性。替换为「原前缀 + 标记 + `@` + host」，与 #315 截断路径同形。
- `_kept_prefix` 里 `_ESCAPED_USERINFO` 的注释改为说明共享规则已覆盖、该步保留；代码不动。
- `scripts/f7_hostile_live.py`：`READ_TOOLS` 从注册工具面派生并接受发现接口读；加 `--pg-port`（原写死 55592，本次用自建 55653）；合成秘密 5→8 个，新增三种形态（span log 里 `\/\/` 散文、span log 里套 JSON 文档、Prometheus label 里 `\u002f\u002f`）；新增判定 `escaped_userinfo_redacted`（三种形态都以 `前缀[标记]@host` 出现在模型可见视图）；summary 增加 `versions`（含视图里实际出现的 `projection_revision`）。
- 已知限制写入 `redaction.py` 注释：userinfo 内含 `\"` 的非法 URL 会漏；不回写旧数据，web 展示层 `display_redacted` 兜底。

## 执行进展与证据

- 红/绿（`.venv/bin/python -m pytest`）：`tests/test_319_escaped_userinfo_impl.py` 63 条，改动前 25 失败（全部是新形态、Observer 截断、版本与兼容展示用例），改动后全过；只回退 Observer 改动时截断用例 4 条失败。`tests/integration/test_319_escaped_userinfo_impl_postgres.py`（发布审计文本）改动前失败、改动后通过。
- 定向回归：F7/redaction/observer/311/问题与告警脱敏相关单测与改了固定值的 5 个实现测试通过；PG（自建 127.0.0.1:55653）上 F7 Observer 脱敏、草稿生成、F11 PR-3 intake 通过。独立验收文件里仍固定 v10/fixture-4/旧哈希的断言失败（3 处单测、1 处 PG），按 U2-A 由独立测试作者修改；其余验收用例通过。
- 真实 Run（回放数据源 + DeepSeek，2026-10-26，当前有效）：[summary.json](../evidence/f7-hostile-input-live/live-runs/8045d49d-ab9c-5a89-89b0-67fd6742889d/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/15f8d52a-f34e-46c8-a562-055c381afec4/r/00000000-0000-0000-3f85-d203f0476ead?poll=true)。`run_status: published`，7 次模型请求，13 次工具操作（含 #307a 发现工具 5 次）；全部判定 pass：范围与无变更、模型请求、重建 transcript、PG raw/view/run、web JSON/HTML、LangSmith 回读、summary、ledger 的合成秘密命中均为 0，`escaped_userinfo_redacted: pass`；`versions.tool_schema_revision = otel-demo-a3a95a0f888e`，`projection_revisions_seen = [m1-01-tool-view-v11]`。费用：两次尝试合计约 0.07 CNY（余额 90.69 → 90.62，精度 0.01 且有延迟）。原始 ledger 在 `tmp/lab-ledgers/`，未入库。
- 第一次尝试（run 2ffb0146，已被替换、summary 已从仓库移除）秘密判定同样全为 0，但 `dispatched_scope`/`no_mutations` 为 fail：模型调用了 #307a 新增的只读发现工具，脚本 `READ_TOOLS` 仍是 #253 时的两件。修复：`READ_TOOLS` 改为从 otel-demo 注册工具面派生，范围判定接受 Prometheus `/api/v1/label/<name>/values` 的 GET 读，回放加发现接口路由；`tests/test_f7_hostile_live_script.py` 新增用例（改动前 3 条失败，改动后 18 条全过）。
- `_redact_quoted` 对无空白 `name:value` 重复文本的平方级耗时（main 上已存在，与本项无关）另开 [#365](https://github.com/kevinWangSheng/production-ops-agent/issues/365)。
- 负对照：只回退 `redaction.py` 后 `--dry-check` 拒绝写证据（`unsafe_artifact_withheld`），即旧规则下合成秘密会出现在落点里。

- 独立验收测试（Codex gpt-6.1-sol，worktree `poa-319-tests`，分支 `chore/319-acceptance-tests`，已合入本分支）：新增 `tests/acceptance/test_319_escaped_userinfo.py`（27）与 `_postgres.py`（4），并按 U2-A 改 10 个验收文件的固定值（brief 写 8 个，实际 10 个）。lead 按预审原文做了四处测试构造修正：GitHub 把 brief 中的 `\u002f`/`\u002F` 渲染成 `/`，测试作者漏了大写形态、截断用例误用字面 `//`；两条「JSON 里套 JSON」多套了一层 `json.dumps`（成了 M3 的双重转义）；一处断言引用不存在的 `RunnerOutcome.evidence_ids`。在 main 上红（单测 18/27 失败、PG 4/4 失败），在本分支全绿；10 个改固定值的验收文件单测 200 passed、PG 71 passed（临时 PG 55652）。
- 独立审查（code-review 两轴，Codex gpt-6-astra，全新上下文、只读）：首轮 Standards 1（验收文件未 ruff format，已修）、Spec 1（`//u@host\/\/v@w` 输出与 main 不同）——不采纳：brief 的「逐字不变」指只含字面 `//` 的输入，该例差异正是新规则要脱敏的转义段；复验审查者同意不采纳，并审 live 脚本 READ_TOOLS 改动（写工具、admin 路径、POST、多余路径段仍判 fail），两轴 0 发现。

## 下一步与交接

- `@codex review` 分诊一次；CI 绿、CLEAN 后合并（用户 2026-10-26 授权本批由 Agent 合并，含用户门 PR）。
- 临时 PG：`tmp/pg55653`（端口 55653），已停止，数据目录保留。
