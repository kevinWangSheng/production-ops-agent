# #319 共享 userinfo 脱敏覆盖 JSON 转义写法的 URL authority

- 状态：进行中（实现与真实 Run 完成；独立验收测试与独立审查待做）
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
- `scripts/f7_hostile_live.py`：加 `--pg-port`（原写死 55592，本次用自建 55653）；合成秘密 5→8 个，新增三种形态（span log 里 `\/\/` 散文、span log 里套 JSON 文档、Prometheus label 里 `\u002f\u002f`）；新增判定 `escaped_userinfo_redacted`（三种形态都以 `前缀[标记]@host` 出现在模型可见视图）；summary 增加 `versions`（含视图里实际出现的 `projection_revision`）。
- 已知限制写入 `redaction.py` 注释：userinfo 内含 `\"` 的非法 URL 会漏；不回写旧数据，web 展示层 `display_redacted` 兜底。

## 执行进展与证据

- 红/绿（`.venv/bin/python -m pytest`）：`tests/test_319_escaped_userinfo_impl.py` 63 条，改动前 25 失败（全部是新形态、Observer 截断、版本与兼容展示用例），改动后全过；只回退 Observer 改动时截断用例 4 条失败。`tests/integration/test_319_escaped_userinfo_impl_postgres.py`（发布审计文本）改动前失败、改动后通过。
- 定向回归：F7/redaction/observer/311/问题与告警脱敏相关单测与改了固定值的 5 个实现测试通过；PG（自建 127.0.0.1:55653）上 F7 Observer 脱敏、草稿生成、F11 PR-3 intake 通过。独立验收文件里仍固定 v10/fixture-4/旧哈希的断言失败（3 处单测、1 处 PG），按 U2-A 由独立测试作者修改；其余验收用例通过。
- 发现但不在本项范围：`_redact_quoted` 对无空白的 `a:b` 重复文本是平方级（512 KiB 约百秒级，main 上即如此，与转义形态无关），已报 lead。
- 真实 Run（回放数据源 + DeepSeek，2026-10-26）：[summary.json](../evidence/f7-hostile-input-live/live-runs/2ffb0146-9fe4-534b-9206-83b1ba88635a/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/15f8d52a-f34e-46c8-a562-055c381afec4/r/00000000-0000-0000-7404-9154224be0cd?poll=true)。`run_status: published`，6 次模型请求，14 次工具操作；模型请求、重建 transcript、PG raw/view/run、web JSON/HTML、LangSmith 回读、summary、ledger 的合成秘密命中均为 0；`escaped_userinfo_redacted: pass`；`versions.tool_schema_revision = otel-demo-a3a95a0f888e`，`projection_revisions_seen = [m1-01-tool-view-v11]`。费用：余额 90.69 → 90.69 CNY（精度 0.01，低于分辨率）。原始 ledger 在 `tmp/lab-ledgers/`，未入库。
- 同一 Run 的 `dispatched_scope`/`no_mutations` 为 fail：模型调用了 #307a 之后新增的只读发现工具 `get_label_values`、`get_metric_names`，脚本的 `READ_TOOLS` 白名单与回放路由仍是 #253 时的两件工具，判定因此失败（目标与窗口均在范围内，回放返回 `SOURCE_ERROR`）。这是脚本判定陈旧，与 #319 无关，照实保留。
- 负对照：只回退 `redaction.py` 后 `--dry-check` 拒绝写证据（`unsafe_artifact_withheld`），即旧规则下合成秘密会出现在落点里。

## 下一步与交接

- 独立验收测试（另一模型、另一 worktree）合入本分支后复跑；独立审查未做。
- PR 属用户门（数据流与凭据），就绪后由用户合并。
- 临时 PG：`tmp/pg55653`（端口 55653），已停止，数据目录保留。
