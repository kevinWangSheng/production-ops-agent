# F7 #254 Observer 原始读数持久化前脱敏

- 状态：实施中
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

实现（2026-10-22，实现者 Agent）：只改 `opspilot/observer/prometheus.py`。新增 `_stored(body)`（非 UTF-8 → `b""`，否则 `redact_json_bytes`）与 `_kept_prefix(body)`（`_stored` 后截到 2048）。`instant()`、`instant_vector()` 的 200 体先 `_stored` 再交 `_single_value`/`_vector_series` 并作为 `body` 返回；原始超限、不完整或脱敏后超 30720 → `failed/TOO_LARGE`、`body_complete=False`、`body` 为脱敏体前 2048 字节；非 200 体、超时与协议中断的部分体都走 `_kept_prefix`。sampler、release_sampling、存储、重放不改：它们本来就对 `result.body` 取哈希、编码与重判，因此自动基于脱敏字节。`docs/development.md` Observer 节加一句说明。

可逆默认（实现者自定，各一行理由）：
- 先脱敏后截断（错误/部分/超限体都是对已读到的全部字节脱敏再截 2048）：跨 2048 边界的令牌不留碎片，也保证脱敏增长不让错误体超过原 2048 上限。代价：读到的字节末尾截在多字节字符中间时整体按非 UTF-8 存空（合同 R4 字面执行，只影响诊断信息）。
- 仅差凭据标签且值不同的两条序列 → 现有 `CONFLICTING_SERIES` 失败；值相同则按现有逻辑去重为一条（与合同 U2-a「失败闭合」一致，未改解析逻辑）。

验证（定向，均在本 worktree）：
- `tests/test_m1_observer.py` 58 passed（原样，未改断言：无命中字节不变的回归证明）；`tests/test_redaction_payload.py` 33 passed；`tests/test_f6_recovery_outcome.py` 34 passed；`tests/acceptance/test_f7_step3_source_secrets.py` + `_postgres.py` 无 PG 时 8 passed 2 skipped。
- 自证单测 `tests/test_observer_redaction_impl.py`（真实 socket）6 passed：凭据标签脱敏且值不变、干净体逐字节不变、401/中断体脱敏、UTF-16 与非 UTF-8 错误体存空、仅差凭据的冲突序列失败、脱敏后超限 → TOO_LARGE 不抛错。
- 临时 PG（127.0.0.1:55651，`tmp/pg55651`，用后已停）：`test_m1_02_replay_postgres.py` 7 passed、`test_m1_02_recovery_outcome_postgres.py` 5 passed、`test_f11_replay_partial_evidence_postgres.py` 8 passed、`test_f11_release_store_postgres.py` 75 passed（首次 1 failed 因 PATH 无 `pg_dump`，环境前提；加 PG17 bin 后全过）、`test_f7_step3_source_secrets_postgres.py` 2 passed。
- `ruff check`、`ruff format --check`（改动的两个 .py）通过；`mypy opspilot/observer/prometheus.py`（strict）无问题。未跑 `make check`/全量 pytest（kind 观察窗，按要求）；另误把 `tests/acceptance/` 全目录连同上述三文件跑过一次，2567 passed 133 skipped。
- 未做：独立验收/合同测试（另一作者并行编写）、Codex 独立审查、kind 真实 Observer 会话（U5-a 可选）。

## 下一步与交接

- 实现与独立测试并行；完成后 Codex 全新会话审查。
