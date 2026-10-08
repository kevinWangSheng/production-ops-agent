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
