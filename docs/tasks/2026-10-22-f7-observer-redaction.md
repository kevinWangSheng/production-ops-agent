# F7 #254 Observer 原始读数持久化前脱敏

- 状态：已完成（#315 合并，2026-10-22）
- 更新日期：2026-10-22
- 依据：#254；F7 合同 U7 r1 补充；[2026-10-15-f7-hostile-input.md](2026-10-15-f7-hostile-input.md) 待决 1；C3 §12；[合同预审 r1](2026-10-22-f7-observer-redaction-contract.md)
- 工作区：`/Users/shenghuikevin/dev/AI/poa-254`，分支 `feature/F7-observer-redaction`

## 目标与范围

Observer 恢复观察与发布观察从 Prometheus 读到的响应体，在解析与入库前统一按 `redact_json_bytes` 脱敏；判定、bundle、哈希、发布 fingerprint 与重放都基于脱敏后字节。不加迁移，不改 web、F13、grants；旧行保持展示时脱敏。

## 合同（r1）

lead 2026-10-22 按用户常设决定「预审待决项按推荐」裁决；完整表见合同文件。

- U1-a：所有响应体脱敏（含错误体与部分体前缀），非 UTF-8 体存空字节。
- U2-a：先脱敏再解析与判定；敌意输入下（仅差凭据形态标签的序列、非 UTF-8）失败闭合。
- U3-a：不迁移、不 bump revision。
- U4-a：旧数据不回写，展示时脱敏照旧。
- U5-a：不要求 LangSmith trace（Observer 不在调查 loop、不调模型）；证据为真实 PG 集成 + 本机假 Prometheus 真实 socket + 重放一致。kind 空出后可选补一次 Observer 会话，不阻塞。
- R1–R9：脱敏放客户端单点；哈希对存储字节；脱敏后超 30720 → `failed/TOO_LARGE`；非 UTF-8 → 空字节；query 等配置字段不改；web、F13 不改；测试放新文件；分支与提交格式见合同。

## 排期约束

- 与 #298/#299/#303 无文件重叠；不改 `web/app.py`，测试不写进 F11 集成测试文件。
- U2 harness 直接使用该客户端：合并时机避开另一会话在途的 U2 capture/fold 轮次。

## 执行进展与证据

实现（2026-10-22，实现者 Agent）：只改 `opspilot/observer/prometheus.py`。新增 `_stored(body)`（非 UTF-8 → `b""`，否则 `redact_json_bytes`）与 `_kept_prefix(body, complete=)`（截断处理见下方 P1/P2a 处置；本句为首版描述）。`instant()`、`instant_vector()` 的 200 体先 `_stored` 再交 `_single_value`/`_vector_series` 并作为 `body` 返回；原始超限、不完整或脱敏后超 30720 → `failed/TOO_LARGE`、`body_complete=False`、`body` 为脱敏体前 2048 字节；非 200 体、超时与协议中断的部分体都走 `_kept_prefix`。sampler、release_sampling、存储、重放不改：它们本来就对 `result.body` 取哈希、编码与重判，因此自动基于脱敏字节。`docs/development.md` Observer 节加一句说明。

可逆默认（实现者自定，各一行理由）：
- ~~先脱敏后截断……多字节截断整体存空~~：已按下方审查修复 P1/P2a 取代。
- 仅差凭据标签且值不同的两条序列 → 现有 `CONFLICTING_SERIES` 失败；值相同则按现有逻辑去重为一条（与合同 U2-a「失败闭合」一致，未改解析逻辑）。

验证（定向，均在本 worktree）：
- `tests/test_m1_observer.py` 58 passed（原样，未改断言：无命中字节不变的回归证明）；`tests/test_redaction_payload.py` 33 passed；`tests/test_f6_recovery_outcome.py` 34 passed；`tests/acceptance/test_f7_step3_source_secrets.py` + `_postgres.py` 无 PG 时 8 passed 2 skipped。
- 自证单测 `tests/test_observer_redaction_impl.py`（真实 socket）6 passed：凭据标签脱敏且值不变、无凭据且无重复键的体逐字节不变、401/中断体脱敏、UTF-16 与非 UTF-8 错误体存空、仅差凭据的冲突序列失败、脱敏后超限 → TOO_LARGE 不抛错。
- 临时 PG（127.0.0.1:55651，`tmp/pg55651`，用后已停）：`test_m1_02_replay_postgres.py` 7 passed、`test_m1_02_recovery_outcome_postgres.py` 5 passed、`test_f11_replay_partial_evidence_postgres.py` 8 passed、`test_f11_release_store_postgres.py` 75 passed（首次 1 failed 因 PATH 无 `pg_dump`，环境前提；加 PG17 bin 后全过）、`test_f7_step3_source_secrets_postgres.py` 2 passed。
- `ruff check`、`ruff format --check`（改动的两个 .py）通过；`mypy opspilot/observer/prometheus.py`（strict）无问题。未跑 `make check`/全量 pytest（kind 观察窗，按要求）；另误把 `tests/acceptance/` 全目录连同上述三文件跑过一次，2567 passed 133 skipped。
- 未做：独立验收/合同测试（另一作者并行编写）、Codex 独立审查、kind 真实 Observer 会话（U5-a 可选）。

独立审查（Codex gpt-6-luna，对 `abbdd09`）三条发现的处置：
- P1 截断边界泄漏 URL userinfo（已修）：`_kept_prefix(body, complete=)` 对未读完的体，先退回 UTF-8 字符边界，再把末尾仍未闭合的 URL authority（`//` 或 JSON 转义 `\/\/` 之后尚无 `/?#`、空白或引号）从 `//` 起截掉，并把 JSON 转义形态的 `\/\/user:pw@` 换成标记，然后脱敏、在字符边界截到 2048。理由：逐形态探测（每个截断点跑 `redact_credentials`）显示只有 userinfo 依赖截断点之后的 `@`；Bearer/Basic、`token=` 等共享策略识别的查询参数（裸 `key` 按 `redaction.py:74-77` 有意不识别，完整体同样不脱敏，不在 #254 范围）、凭据键名下的 JSON 值在截断前缀里已被现有规则替换，只截 authority 能保留其余诊断，`test_m1_observer.py` 的保留前缀断言原样通过。完整读取的体保持原样。合同文件 l.88「读取时先截断再脱敏」的描述已由此修正（合同原文不改）。
- P2a 截断后 UTF-8 不完整（已修）：截断点回退到字符边界，保留诊断，不存空字节；超限路径同样走 `_kept_prefix`。
- P2b 重复键规范化（只改表述）：不改 `redaction.py`；`docs/development.md` 与本记录改为「无凭据命中且无重复键时逐字节不变，有重复键时按 F7 策略规范化（失败闭合）」，自测固定该行为。
- 审查修复验证：`tests/test_f7_observer_redaction.py` 10 passed、`tests/integration/test_f7_observer_redaction_postgres.py` 3 passed、`tests/test_observer_redaction_impl.py` 19 passed（新增 9 种凭据形态逐截断点、跨 2049/30721 读取上限、多字节截断、重复键）、`tests/test_m1_observer.py` 58 passed、`tests/test_redaction_payload.py` 33 passed、F7 step3 两文件 8+2 passed、m1_02 replay 7、recovery_outcome 5、f11_replay_partial_evidence 8、f11_release_store 75 passed（临时 PG 55651，用后已停）；ruff、format、mypy 对改动文件通过。
- 范围外发现（未改）：共享 `redact_credentials` 的 userinfo 规则只认字面 `//`，非 JSON 的完整体（如错误页文本）里 JSON 转义的 `\/\/user:pw@host` 不会被脱敏；本 PR 只在未读完体的路径补了这一形态，共享策略是否补齐需另开 issue。

## 下一步与交接

- 实现与独立测试并行；完成后 Codex 全新会话审查。
- 复验（同一审查会话续跑，gpt-6-sol）：P1 九种形态逐截断点 0 个 ≥8 字符片段、P2a 全部截断点 UTF-8 合法、增量无越界。审查另报裸 `key=` 未脱敏：lead 判定不属 #254 缺口——U1-a 是「与 `redact_credentials` 同一策略」，该策略有意不识别裸 `key`（完整体亦然），lead 给实现者的要求误写了「key=」；本记录表述已更正。P2b 表述两处已由 lead 更正。
- 范围外（待用户决定是否开 issue）：共享 userinfo 规则只认字面 `//`，完整非 JSON 体里 JSON 转义的 `\/\/user:pw@host` 不脱敏；本 PR 只在未读完体路径补了该形态。

## 收尾

- 实施期间新冒出的合同问题：1（截断边界泄漏 userinfo，审查 P1；按 U1-a 失败闭合细化，lead 裁决）。
- 因合同变更重写的测试：0（独立测试另有 2 处读法、3 处夹具修正，均非合同变更：裸秘密不属凭据形态、会话参数、CI 残留会话认领与大体超时）。
- 环境重跑次数：CI 2 次（夹具问题）；Codex 容量不足多次换模型。
- 用户裁决轮数：0（预审项按常设决定采纳）；合并与 #319 开单 1 轮。
