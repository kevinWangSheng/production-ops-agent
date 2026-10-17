# F11 U2 发布观察校准证据

- 状态：实验完成、证据待冻结与审查（2026-10-17 批次 `u2-20261017`：基线、probe、五类正式轮、受缺陷影响的三类重跑、boundary、Observer 接线均已执行；policy 未写入，待用户批准）
- 更新日期：2026-10-17
- 依据：[F11 任务记录](2026-10-12-f11-post-release.md)（U2／P1-02）；[PR-2 合同](2026-10-13-f11-pr2-contract.md) §1 校准 policy、固定语义 1／4、P2-04、「校准证据不足」、r1–r7；[预审与决定表](2026-10-14-f11-u2-calibration-contract.md)；C3 §12「实验环境」；ADR-0006
- 工作区：工具 PR #233 已合并（`e1b8867`），分支已清理。实验工具可在主仓库 `main` 运行（占用与原始 ledger 都在主仓库 `tmp/`）；提交证据与 policy 时另开 `chore/`／`feature/` 分支 worktree
- 实验环境：kind-opspilot-m1／otel-demo；2026-10-17 04:05Z–22:46Z 由本任务占用（与 eval 会话 -00／-76／-87 协调，-87 同意延至 10-18 01:00Z；占用记录写的结束时间仍是 16:00Z，延长靠会话协调，记录到期也不会被抢占），已释放

## 目标

在协调后的 kind 实验环境，对 `otel-demo/Deployment checkout` 的真实配置滚动发布取得证据，冻结首个正式发布观察校准 policy（目标身份、profile revision／完整哈希、`min_tracking_seconds` 及冻结参数），经用户批准后写入 `opspilot/observation/policies/release-calibrations.json`，首次允许发布观察授权。不翻 F11 `passes`（U2 不覆盖调查与 old/new 对比验收）。

## 合同（r1）

- U2-01（用户）：覆盖承诺为发布后 0／5／15 分钟起病的回归，另测临近健康终结的边界；不宣称监测满一小时。
- U2-02（用户）：checkout 真实配置滚动发布后按计划注入现有 `paymentFailure`；只证明发布后异常检测与时间覆盖，不证明版本内生回归。
- U2-03（用户；**2026-10-14 r2：放宽为五类各 1 次**，见 [M1 退出决策 D2](2026-10-14-m1-exit-decision.md) 与 SPEC 2026-10-14 段，以下「×3」均按 1 次执行）：五类各 3 次——健康、即时回归、延迟 5 分、延迟 15 分、健康发布＋并发无关故障。通过：健康零异常误报且能 healthy 终结；回归全部在终态前且 deadline 内检出；预热后非预期 unknown ≤5%（全程比例另报）；健康终结时剩余 ≥5 次、≥300 秒。小样本不构成统计可靠性保证。失败轮保留，区分注入失败、环境干扰与产品漏检，不删失败、不事后放宽判据；改参数须新 profile revision 并重跑受影响场景；证据不足则清单保持为空。
- U2-04（用户）：旁路只读采集为主证据——复用 `build_release_query_plan`，串行 mapping/context→UID→after，保存原始响应，离线 `evaluate_release_sample` 折叠；独立审查者对照注入日志、实际响应与 Kubernetes 对象核对真值。产品 Observer 经临时测试清单跑一遍只作接线验证，不算正式校准。
- L1（lead）：发布用无业务作用的实验标识环境变量触发 checkout 滚动更新，保存前后模板与 digest；无关故障候选 `ad` 的可恢复故障，须先证明 checkout 必要信号与流量不受影响；不重建 chart。
- L2（lead）：同 U2-04 采集路径；不把诊断额外查询计入产品覆盖。
- L3（lead）：补有界实验工具（发布快照与事件、时间线 watch、定时注入与恢复、无关故障钩子、旁路采集与离线折叠、实验环境占用记录）。
- L4（lead）：预约串行批次，一次一个故障，恢复且健康窗洗净后再下一轮；每批 3 轮预留 3–4 小时，另 30 分钟基线；凭据只 source 不打印，旁路与 Observer 各用自己的账号。
- 证据：`docs/evidence/f11-u2-calibration/`（`run.md`、冻结 `summary.json`、逐轮摘要、原始响应哈希清单）；原始 ledger 不入库。policy `evidence_path` 指向冻结 `summary.json`、`evidence_sha256` 为其确切字节哈希；用户批准摘要哈希、目标、profile 哈希、M 与覆盖限制后填 `approval_ref／approved_at`。写入正式清单是用户门 PR。

## 进展与证据

- 2026-10-14 预审完成并裁决（r1）。
- 2026-10-14 实验工具 `scripts/f11_u2_harness.py`（[#233](https://github.com/kevinWangSheng/production-ops-agent/pull/233)，`e1b8867`；Codex gpt-6.1-sol 实现）：占用记录、配置发布、时间线、定时注入与恢复、按产品路径登记 pending 发布（`register`）、旁路采集、离线折叠、`abort`、按批次日志核对的汇总。复用产品 `build_release_query_plan`、`evaluate_release_sample`、`advance_release_window`、`instant_vector`、`record_release`，窗口算法未重写，外围调度／采纳规则逐处标注 PR-2 合同行号。独立审查（Codex gpt-6-astra）3 项 P2 已修复复验；机器人 3 项 P2 已修复（占用锁改为经 git common dir 定位的主仓库 `tmp/lab-ledgers/` 跨 worktree 共享；事件流按时间合并；汇总按批次开始日志核对轮次，缺失即拒绝）。离线测试 66 passed，`register` PG 测试 1 passed（lead 在沙箱外执行），`make check` 5463 passed。
- lead 可逆默认：登记传 `canonical_content(profile)`，与产品 web 一致（`opspilot/web/service.py:684`），原文另存并记录哈希；无关故障 flag 为 `adFailure`（集群 `flagd-config` 中核实；checkout 下游不含 ad，仍须实测确认不影响 checkout 信号）。
- 收尾四数（工具阶段）：新增合同问题 0；因合同变更重写测试 0；环境重跑 0；新增用户裁决 0。
- 2026-10-17 probe（`u2-probe-1`）发现 Prometheus 把求值时间舍入到毫秒，窗尾微秒 ≥500 时样本时间戳晚于 evaluated_at、整样本 `QUERY_FAILED`→unknown；用户裁决方案 A：查询 `time` 参数向下截断到毫秒（PR-2 合同 r8 修订 B01），分支 `feature/F11-release-query-ms-floor`，修复后重跑 probe 再进入正式轮（#261）。
- 2026-10-17 正式轮 `u2-f-immediate-1`／`u2-f-delayed5-1`／`u2-f-delayed15-1` 起病时 after 切片 coverage 被新出现的 ERROR 序列拉低、判 `INSUFFICIENT_SAMPLES`→unknown；用户裁决按 PR-2:556–559 意图修复（r8 第二条：实例内 max、跨实例 min），分支 `feature/F11-release-coverage-per-instance`；三轮保留为产品缺陷轮、不作校准证据，修复后重跑 immediate／delayed-5m／delayed-15m／boundary，healthy／unrelated 经核实 after 实例无 ERROR 序列、无起病 unknown 保留。

- 2026-10-17 实验（证据 [run.md](../evidence/f11-u2-calibration/run.md)）：基线 scrape 60s、单查询中位 1.5s、单样本 61–91s。有效轮 healthy-1、unrelated-1、immediate-2、delayed5-2、delayed15-2、boundary-1；缺陷轮 probe-1、immediate-1、delayed5-1、delayed15-1 保留、不作证据；probe-2 与 Observer 接线轮以 `abort` 记入。M=1200 与 M=1500 均满足 U2-03 全部判据（预热后非预期 unknown 0/35、0/39；健康终结剩余 ≥28 次、≥1878s）；M≤900 在 delayed-15m／boundary 漏检，M=1800 在 unrelated 备用预算 94s 不足。adFailure 实测不影响 checkout，但为弱故障（1/25、3/126）。Observer 接线：web 角色授权 applied，`opspilot_observer_u2` 14 个样本后 healthy_confirmed。实验库为 Observer 迁移 0016→0017（仅 GRANT）。
- 收尾四数（实验阶段）：新增合同问题 2（r8 两条）；因合同变更重写测试 3（B01 验收、两处实现测试断言）；环境重跑 0（缺陷重跑 3 轮另计）；新增用户裁决 3（方案 A、按意图修覆盖、窗口延长）。

## 待决与交接（2026-10-17）

- **用户批准项**（policy 写入 `release-calibrations.json` 为用户门 PR，未做）：冻结 `summary.json` 的 sha256；目标五项身份（见 run.md）；profile `otel-demo-checkout@b72bbe2e30be`／`b72bbe2e30bea4775790519787da4ce107dbc1500ce87b87a2ee2af3f57cbfa4`；M（lead 推荐 1500：delayed-15m 余量约 6 分钟，1200 仅约 70s）及冻结参数（W300／F90／超时 20／I60／H600／deadline 3600／40 次）；覆盖限制（run.md「覆盖与限制」）。
- **实验库**：PG `127.0.0.1:55561` 仍运行，数据在主仓库 `tmp/f11-u2/pg`（勿删）；登录角色 `opspilot_observer_u2`、`opspilot_web_u2`。原始 ledger 在主仓库 `tmp/lab-ledgers/u2-*`（不入库，勿删）。
- **已知坑**：harness `collect` 后台进程忽略 SIGINT（非交互 shell），提前停止只能 SIGTERM，之后须人工核实 PID 已退出再移走 `.active-*.json`（probe-2 已如此处理并存档）；Codex 沙箱内起不了 PG；审查提示避免「篡改／绕过」类措辞。
- **相关待排**：[#230](https://github.com/kevinWangSheng/production-ops-agent/issues/230)（重放逐字段完整性），与 U2 无依赖。
