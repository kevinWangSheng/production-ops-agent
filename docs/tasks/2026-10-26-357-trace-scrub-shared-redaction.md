# #357 trace 导出兜底改用共享 `redact_credentials`

- 状态：实现与真实 Run 完成，M1 已由 lead 裁决；独立验收测试与独立审查待做
- 更新日期：2026-10-26
- 依据：issue #357 的 Agent Brief 与「Brief 补充（用户 2026-10-26 裁决）」；前置 #319（[任务记录](2026-10-26-319-escaped-userinfo-redaction.md)）；已知误伤 #366
- 工作区：`feature/F7-357-trace-scrub-shared-redaction`，`/Users/shenghuikevin/dev/AI/poa-357-impl`（起点 origin/main 5b158b1）

## 目标与范围

trace 导出前的兜底 `_scrub`（`opspilot/tracing.py`）原先只有自己的两条凭据正则，没有 URL userinfo 规则，导出 trace 的凭据安全完全依赖上游脱敏。本项让兜底对每个导出字符串调用共享 `redaction.redact_credentials`，共享策略扩展时兜底自动跟上。trace 的结构、键名、采样与导出目标都不改。共享策略本身的覆盖范围不在本项（#319、#359），已知误伤也不在本项（#366）。

## 合同（r1）

| 编号 | 裁决要点 | 出处 |
|---|---|---|
| Q15-A | 兜底直接调用共享 `redact_credentials`，兜底里不另写、不复制凭据正则；其他既有清洗保留 | 用户 2026-10-26 |
| 补充 | 验收第 3 条收窄为「共享策略无匹配的 trace 载荷经兜底后逐字节不变」；`*_tokens` 计数、`session_id`、`auth: ok` 等误伤列明不修（#366） | 用户 2026-10-26 |
| M1 | 导出 trace 的凭据标记从 `[REDACTED]` 变为共享的 `[REDACTED_CREDENTIAL]`，这是 Q15-A 的直接后果；`tests/test_tracing.py` 三处标记字面量（:792、:795、:845）随之改写，没有断言变弱，秘密不出现的断言一条未改。:792 改写的原因：键级 `_redact` 写入 JSON 文本的 `[REDACTED]` 会被共享策略再次标记为 `[REDACTED_CREDENTIAL]` | lead 2026-10-26 裁决（选项 A） |

## 实现决定（可逆，Agent 自定）

- `_scrub` 顺序：① 环境变量秘密值整值替换（`_secret_values`，保留）；② `redaction.redact_credentials(text)`；③ `_KEY_SHAPES`（保留）。通过模块属性在调用时取函数，monkeypatch 共享策略即对兜底生效。
- 删除 `_CREDENTIAL_TEXT` 与 `_BEARER_TEXT`。逐例对比：两条旧正则命中的所有形态（`Authorization=`/`: `、`password`、`passwd`、`my-token`、`api-key`、`bearer:`、`Bearer <token>`）共享策略都覆盖；共享策略另外覆盖 userinfo（字面与转义）、`access_token=`（旧 `\b` 漏）、JSON 引号键值。差异：旧规则 `\S+` 吞到空白，共享规则在 `&;,?#` 处停，`secret=x&y=1` 现在保留 `&y=1`。
- 保留 `_KEY_SHAPES`（`sk-…`、`lsv2_…`）：共享策略只认带命名上下文的凭据，对裸的供应商 key 不匹配（实测 `sk-aaaa…` 原样通过），删掉会丢失现有清洗；brief 要求保留「其他既有清洗」。它是供应商 key 形态规则，不是凭据名/赋值规则，与共享策略不重叠。
- 未动 `_CREDENTIAL_KEY` / `_redact`：这是工具参数名列表与 arguments JSON 的键级结构脱敏，不是字符串值兜底，brief 只改字符串值清洗。
- 全部导出字符串都经唯一写点 `_write` → `_scrub`（`test_round3_scrub_is_the_single_write_path` 守护）；状态描述与异常文本本来就不导出，inputs/outputs/metadata 都是经 `_write` 的属性。

## 执行进展与证据

- 红/绿：`tests/test_357_trace_scrub_impl.py` 10 条（接缝：lab tracer + 内存 exporter，文本经 prompt 与 reply 进入，读导出属性）。改动前 7 失败（字面 `//`、`\/\/`、`\u002f\u002f` userinfo，`access_token=`，JSON `"api_key"`，host 保留，monkeypatch 新形态），3 通过（Bearer、逐字节不变、key 形态与环境值，均为保护性用例）；改动后 10 通过。
- 回归：`tests/test_tracing.py`（改 M1 三处标记字面量后）、`test_m0_trace*`、`test_m0_round07_launch`、`test_319_escaped_userinfo_impl`、`test_redact_credentials_quoted`、`test_redaction_payload`、`test_f7_observer_redaction`、`test_observer_redaction_impl` 合计 260 passed。ruff check/format、mypy（`opspilot/tracing.py`）通过。
- 真实 Run（回放数据源 + DeepSeek，不占 kind，自建 PG 127.0.0.1:55654）：[summary.json](../evidence/f7-hostile-input-live/live-runs/b5a490b1-fae3-5f01-9b59-595495c14e17/summary.json)；[LangSmith trace](https://smith.langchain.com/o/c7727675-dcae-4751-8582-7ecaae39a80f/projects/p/4701d37d-af6d-4586-8d89-4ea4f2b49b1f/r/00000000-0000-0000-0a65-bbbe1dda5822?poll=true)。`run_status: published`，7 次模型请求、15 次工具操作，trace 回读 23 个 run 完整（`dropped_spans: 0`）；全部判定 pass，LangSmith 回读、模型请求、PG、web、summary、ledger 的合成秘密命中均为 0。原始 ledger 在 `tmp/lab-ledgers/`，未入库。费用未对账（运行前未取余额，summary `cost` 为 `lead_to_fill`）。
- 第一次尝试（run 16fe6ee0，summary 未入库，移到 `tmp/superseded/`）：秘密命中同样为 0，但导出丢了 3 个 span（`dropped_spans: 3`，回读 22/25），`trace_complete: fail`、`secrets_trace: unverified`。属导出环境问题（本机 swap 约 11.3/12 GB），重跑一次通过。
- 照实说明：本 Run 的合成秘密都在上游（工具视图）已被脱敏，没有一条绕过上游直接进 trace。用共享策略扫 ledger 全部 1082 个字符串，改写数为 0，即兜底在本 Run 中对秘密没有实际作用。本 Run 证明的是「新兜底接入真实导出路径后端到端零命中、trace 完整」；兜底单独拦截各形态由上面的单测证明。
- 重建（lead，2026-10-26）：独立验收测试里的合成 assignment/Bearer 值（高熵）在 PR #367 分支历史中触发 CI gitleaks generic-api-key；CI 扫描所有已取回分支的历史，追加提交无法消除。改为从 main 以单个提交重建为分支 `feature/F7-357-trace-scrub-redaction`（新 PR），合成值换成低熵的 `aaaaaaaa357`/`bbbbbbbb357`（断言不变），关闭 #367 并删除含该提交的两个远程分支。独立审查（Codex gpt-6-astra，两轴 0 发现）与 `@codex review`（无发现）针对的是同一内容。

## 已知误伤（#366，不在本项修正）

共享凭据名规则会把非凭据字段的值替换为标记：`prompt_tokens=12`、`session_id=abc`、`auth: ok` 实测变为 `…=[REDACTED_CREDENTIAL]`。兜底接入后，trace 里含这类 `name=value` 文本的属性值也会被替换；结构化的 token 计数属性是整数，不经字符串兜底，不受影响。另：`_redact` 写入的 `[REDACTED]` 在 JSON 文本里会被共享策略改写为 `[REDACTED_CREDENTIAL]`（幂等改写，非泄漏）。

## 下一步与交接

- 独立验收测试（Codex，另一个 worktree，不碰本分支 `tests/acceptance/`）合入后复跑；独立审查；`@codex review` 由 lead 触发。用户门（凭据），就绪后打 `ready-for-human`。
- 临时 PG：`tmp/pg55654`（端口 55654），已停止，数据目录保留。
