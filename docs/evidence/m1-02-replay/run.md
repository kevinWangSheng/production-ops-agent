# M1-02 第 5 步：工作台展示与离线重放（#87）

- 日期：2026-10-08
- 分支：`feature/F6-replay-display`，基于 main `8aae433`
- 范围：事故页「Recovery verdict」区块（与「Investigation report」分开，附逐采样/逐读数依据与离线重放结果）；`python -m opspilot.observer.replay` 离线重放；`observation.store.fold_history` 公开折叠；`sampler.replay_sample`。
- 未启动 kind 实验环境，未调用模型，未查遥测；本记录全部来自本机测试与临时 PostgreSQL。

## 页面证据（无 PG，内存 IncidentStore + 真实 Observer 采样捆绑）

由 `tests/test_m1_02_recovery_page.py` 的夹具渲染并保存（渲染脚本只调用测试夹具与 GET 页面）：

- [incident-page-recovered.html](incident-page-recovered.html)：6 个健康采样，第 6 个覆盖 600 s 持续窗口 → `recovery confirmed`、`replay consistent`、lifecycle `resolved`；每个采样列出窗口、stored → replayed outcome、存储判定（disposition / reason / basis / transition）；每条读数列出 stored → replayed 的状态/值/点数、逐信号判定、查询、窗口、来源、原始捆绑 sha256 及校验结果。
- [incident-page-degraded.html](incident-page-degraded.html)：3 个 `degraded` 采样，verdict `degraded`，`not confirmed`，reasons 含「required signals outside their healthy bound: errors」。
- [incident-page-tampered-verdict.html](incident-page-tampered-verdict.html)：只改已存 `outcome` 列为 `degraded`（捆绑未动）→ 页面显示 `integrity mismatch STORED_OBSERVATION_INTEGRITY_MISMATCH`，verdict `unknown`，并注明「bundles recompute to healthy; the stored verdict is not repeated」。

页面不新增表单或写动作（测试断言表单数 == 现有控制动作数）。

## 测试

- 单元：`tests/test_m1_02_replay.py` 10 passed（健康会话一致并确认；只改判定列 → 完整性不一致 + unknown；篡改捆绑 → 该采样 failed、会话不一致；重写捆绑并重算哈希 → 按内容重判 stale 与已存不符；degraded / no_data 会话一致、次数耗尽回 open；profile 内容不符 revision → `HEALTH_PROFILE_UNREADABLE`；无 revision 会话 unknown 且非完整性问题；删光读数行 → `NO_READINGS`；F6 形态 `replay(...)` 拒绝 allow_*；JSON 摘要不含原始字节；CLI 退出码 0/1/2）。
- 页面：`tests/test_m1_02_recovery_page.py` 4 passed；既有 `tests/test_m1_02_register_web.py` 9 passed。
- Observer 单元（`replay_sample` 改动）：`tests/test_m1_observer.py` 47 passed。
- PostgreSQL 17.9 临时实例（端口 55641，数据目录在会话临时目录，未碰 55431 lab）：
  - 新增 `tests/integration/test_m1_02_replay_postgres.py` 5 passed：Observer 登录角色 + 桩 Prometheus 跑出恢复 / 撤流量 / 缺必要信号 / 持续异常四类会话，`replay_stored_session` 全部一致且重放期间桩计数不变；CLI 以 Observer DSN 运行退出 0；只改 `outcome` 列 → 不一致 + unknown + 退出 1、`recomputed_verdict=healthy`；改 raw 字节 → `RAW_HASH_MISMATCH:error_ratio`；改 `opspilot_health_profiles.content` → `HEALTH_PROFILE_UNREADABLE`，全部采样 `replayed_outcome=None`。
  - 既有四个 M1-02 套件（store / observer / register_remediation / takeover）101 passed。
- `make check`：3151 passed / 462 skipped / 2 xfailed（Ruff、格式、mypy 通过）。
- 红证明：见 PR 正文 `make red-proof` 输出（页面测试在旧代码上为断言失败，重放测试为新模块 ImportError）。

## 独立审查处置（2026-10-08，Codex 全新上下文，4 条 P2，全部采纳；原文存于主仓库 `tmp/m1-02-review/139-review-final.md`）

每条先以审查复现写成测试（第一提交上红），再修：

1. 删读数 + 把判定改成非 healthy 仍判一致：已采纳采样在可读 profile 下必须对 profile 的每个信号都有读数行（Observer 对全部信号采样；无读数提交只发生在无 revision 或 profile 不可验证两条路径，采样行没有列记录「无读数」，因此读数缺失即 `NO_READINGS` / `READING_MISSING:<signal>` 完整性不一致）；history_only 采样（如中途挂起的部分采样）不受此限。
2. 删中间采样 / 改会话水位：`fold_history` 现回报 `adopted_sequence / adopted_window_end / adopted_count / healthy_since`，与会话行比较，不等即 `WATERMARK_MISMATCH`。
3. 改读数 query / source / 窗口：读数行的 query 须等于冻结 profile 的 `signal.query`、source 等于 `profile.source`、窗口等于采样窗口，捆绑校验通过时其 `query.expr` 与窗口亦须一致，否则 `READING_BASIS_MISMATCH:<signal>:<field>`；信号不在 profile 内 → `UNKNOWN_SIGNAL:<signal>`。
4. 畸形读数（如 `source="BAD SOURCE"`）：读数重建异常改为 `BASIS_UNPARSABLE:<ExcType>` 完整性不一致 + unknown；CLI 对每会话重放再兜底捕获异常输出 `REPLAY_FAILED:<ExcType>` 文档并退出 1；页面已有 `REPLAY_FAILED` 兜底。

### 复验第二轮（2026-10-08，同一审查者，4 条 P2，全部采纳；原文 `tmp/m1-02-review/139-recheck-final.md`）

1. coverage / freshness 查询未绑定冻结 profile：捆绑里三条查询的 `expr` 都须等于 profile 对应字段（`READING_BASIS_MISMATCH:<signal>:bundle_coverage|bundle_freshness`）。
2. 期限沿用已存 `within_deadline`：`fold_history` 按会话冻结 `deadline_at` 与采样窗尾重算（窗尾 ≥ 期限而行说在期限内 → `DEADLINE_MISMATCH`，折叠按不在期限内处理；窗尾在期限内而提交迟到仍是合法 `deadline_expired`）。
3. ending 校验：恰好一条结束记录（多一条即不一致）；重算折叠若自己结束了会话，记录的原因须相同；`max_samples_exhausted` 须由重算已采纳次数 ≥ 冻结 `max_samples` 支撑；无采样的 `deadline_expired`（sweep）须记录在冻结期限之后。
4. 上一轮误报：Observer 在线取不到 / 无法验证 profile 行时 `submit_without_readings` 现在写一条 `health_profile` 哨兵读数（status failed、query = revision、source `observer`、哈希捆绑记 `reading_error` = `HEALTH_PROFILE_UNAVAILABLE`（存储错误）/ `HEALTH_PROFILE_INVALID`（内容不验证或 revision 不符）、error_type、revision），用已有读数表列，无迁移；重放验证该记录（哈希、revision、唯一一行）后才豁免覆盖检查，`recompute_skipped` 报该原因；删掉它仍是 `NO_READINGS`。无 revision 的会话照旧不写读数（会话列即依据）。PG 测试：一次 poll 让 `store.health_profile` 抛 `PersistenceError` → `failed` + 哨兵，恢复后确认恢复，重放一致；删哨兵 → 不一致。
   范围边界已写入 PR 正文与 development.md：拥有写权限者把原始数据、哈希、判定与会话一致改写无法被检出，不是本步目标（ADR-0003）。

第二轮复验数字：`tests/test_m1_02_replay.py` 21 + `tests/test_m1_02_recovery_page.py` 5 + `tests/test_m1_observer.py` 47 + schema/合同 44 = 117 passed；`tests/integration/test_m1_02_replay_postgres.py` 7 passed；四个既有 M1-02 PG 套件 101 passed（55641）；`make check` 3162 passed / 464 skipped / 2 xfailed。

第一轮复验数字：`tests/test_m1_02_replay.py` 16 + `tests/test_m1_02_recovery_page.py` 5 passed；`tests/integration/test_m1_02_replay_postgres.py` 6 passed（新增用例在真实行上：改 query → `READING_BASIS_MISMATCH:error_ratio:query`；`healthy_since` 置 NULL → `WATERMARK_MISMATCH`；删一条读数并把 outcome 改为 no_data → `READING_MISSING:error_ratio`，三者 verdict 均 unknown）；四个既有 M1-02 PG 套件 101 passed（55641）；`make check` 3157 passed / 463 skipped / 2 xfailed。

### 最后一轮（2026-10-08，机器人线程分诊 4 条 + issue #141，lead 采纳，全部处置）

1. 会话参数不能自证：`max_samples / sustained_window_seconds / sample_interval_seconds` 须等于冻结 profile 的 `session` 值，`deadline_at` 须落在会话行 `created_at` 之后、不超过 `deadline_seconds`+60 s 偏差，否则 `SESSION_PARAMETER_MISMATCH:<field>`。测试夹具据此改为按需派生 profile 变体（`profile_with(...)`、PG 测试 `_authorize` 存入与参数匹配的 profile 内容与 revision）。
2. 无 HealthProfile 的会话 verdict 一律 unknown（不回退到已存 degraded；PRODUCT-CONSTRAINTS「无 profile 不能判定恢复」）。
3. 挂起条件按采样行 `global_generation / target_generation` 与会话 `authorized_*_generation` 重算：代际已变即按挂起处理（不计入恢复），与 `scope_suspended` 标志矛盾 → `SCOPE_MISMATCH`。
4. #141：哨兵采样 outcome 须为 `failed` 且 `required_signals_present=false`（`SENTINEL_MISMATCH:outcome|required_signals_present`）；哨兵读数窗口须等于采样窗口与捆绑窗口（`SENTINEL_MISMATCH:window|bundle_window`）。

数字：`tests/test_m1_02_replay.py` 25 + 页面 5 + observer 47 + schema/合同 44 = 121 passed；重放 PG 套件 7 passed；四个既有 M1-02 PG 套件 101 passed（55641）；`make check` 3166 passed / 464 skipped / 2 xfailed。
