# F11 U2 发布观察校准证据

- 状态：进行中（实验工具已合并 #233，真实实验未执行；2026-10-14 交接）
- 更新日期：2026-10-14
- 依据：[F11 任务记录](2026-10-12-f11-post-release.md)（U2／P1-02）；[PR-2 合同](2026-10-13-f11-pr2-contract.md) §1 校准 policy、固定语义 1／4、P2-04、「校准证据不足」、r1–r7；[预审与决定表](2026-10-14-f11-u2-calibration-contract.md)；C3 §12「实验环境」；ADR-0006
- 工作区：工具 PR #233 已合并（`e1b8867`），分支已清理。实验工具可在主仓库 `main` 运行（占用与原始 ledger 都在主仓库 `tmp/`）；提交证据与 policy 时另开 `chore/`／`feature/` 分支 worktree
- 实验环境：kind-opspilot-m1／otel-demo；与 eval 会话已协调（2026-10-14：近期不占用，开跑前互相确认）

## 目标

在协调后的 kind 实验环境，对 `otel-demo/Deployment checkout` 的真实配置滚动发布取得证据，冻结首个正式发布观察校准 policy（目标身份、profile revision／完整哈希、`min_tracking_seconds` 及冻结参数），经用户批准后写入 `opspilot/observation/policies/release-calibrations.json`，首次允许发布观察授权。不翻 F11 `passes`（U2 不覆盖调查与 old/new 对比验收）。

## 合同（r1）

- U2-01（用户）：覆盖承诺为发布后 0／5／15 分钟起病的回归，另测临近健康终结的边界；不宣称监测满一小时。
- U2-02（用户）：checkout 真实配置滚动发布后按计划注入现有 `paymentFailure`；只证明发布后异常检测与时间覆盖，不证明版本内生回归。
- U2-03（用户）：五类各 3 次——健康、即时回归、延迟 5 分、延迟 15 分、健康发布＋并发无关故障。通过：健康零异常误报且能 healthy 终结；回归全部在终态前且 deadline 内检出；预热后非预期 unknown ≤5%（全程比例另报）；健康终结时剩余 ≥5 次、≥300 秒。小样本不构成统计可靠性保证。失败轮保留，区分注入失败、环境干扰与产品漏检，不删失败、不事后放宽判据；改参数须新 profile revision 并重跑受影响场景；证据不足则清单保持为空。
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

## 下一步与交接（2026-10-14）

接手会话按此执行；尚未对 kind 做任何写操作，未生成任何真实证据。

- **实验库**：本机专用 PG `127.0.0.1:55561`，库 `m0_budget`，owner `m0_lab`（trust），已迁移至 `0016`，库为空。数据在主仓库 `tmp/f11-u2/pg`（git 忽略；不随 worktree 删除，勿删）。交接时仍在运行；若已停：`/opt/homebrew/opt/postgresql@17/bin/pg_ctl -D tmp/f11-u2/pg -l tmp/f11-u2/pg.log -o "-p 55561 -k $PWD/tmp/f11-u2/sock -c listen_addresses=127.0.0.1" -w start`。不要复制 `tmp/184-step3/pg` 旧证据库（含角色凭据，权限分类器会拒绝）。
- **目标身份**：身份文件 `tmp/184-step3/targets.json`，注册表键 `m0-otel-20260909`（`cluster_uid` 与集群 `kube-system` UID 一致，lead 已核）。profile 为仓库 `opspilot/observer/profiles/otel-demo-checkout.json`（revision `otel-demo-checkout@b72bbe2e30be`）。
- **凭据**：旁路采集只 `set -a; source tmp/m1-kind-lab/prometheus-lab.env; set +a`；Observer 接线验证用 `prometheus-observer.env`。只 source，不 cat、不打印、不写入证据。flagd 端口转发在开跑阶段自行准备。
- **实验环境协调**：2026-10-14 eval 会话 production-ops-agent-00、-76 与评测会话 -87 均回复近期不占用 kind-opspilot-m1，开跑前会先联系本任务。开跑前用 `occupation claim` 写占用记录（主仓库 `tmp/lab-ledgers/`），并用 SendMessage 通知上述会话具体时段；一次只注入一个故障，恢复且健康窗洗净后再下一轮。
- **开跑顺序**：`kind_lab.py health` 全部 ok、所有 flag 为 off → `occupation claim` → 基线测量约 30 分钟（scrape 周期、flush 延迟、checkout 流量、查询耗时）→ 一轮 `probe` 开发探测（release → watch → register → context → collect → fold），核对端到端链路与时间线 → 确认 `adFailure` 不影响 checkout 必要信号 → 正式五类×3（每批 3 轮约 3–4 小时）→ 临近健康终结的边界轮（U2-01，场景 `boundary`）→ 一次产品 Observer 接线验证（U2-04；先在 55561 实验库建专用登录角色，trust 认证无需口令：`CREATE ROLE opspilot_observer_u2 LOGIN IN ROLE opspilot_observer`、`CREATE ROLE opspilot_web_u2 LOGIN IN ROLE opspilot_web`，Observer 用 `OPSPILOT_OBSERVER_DSN=…user=opspilot_observer_u2`、授权走 web 角色；不得用 owner `m0_lab` 跑接线验证，否则绕过角色授权与守卫，启动方式见[开发指南](../development.md)「Observer」一节；临时测试清单驱动 Observer 走授权→采样→提交，只作接线证据，不算正式校准，测试清单不得进入产品清单）→ `aggregate --batch` 冻结 `docs/evidence/f11-u2-calibration/summary.json` → 确认所有故障已恢复、无运行中操作后 `occupation release` 释放占用，并通知上述会话 → 证据独立审查（Codex gpt-6-astra，对照注入日志、原始响应与 K8s 对象核对真值）→ 用户批准摘要哈希、目标、profile 哈希、M 与覆盖限制 → policy 写入 PR（用户门）。命令细节见[开发指南](../development.md#f11-u2-有界实验工具r1l3)。
- **判据与失败处理**：见上文 r1 U2-03。冻结与送审前须确认五类×3、`boundary` 轮与 Observer 接线验证都已执行并在汇总或 run.md 中可查（`aggregate` 只核对日志轮次齐全，不检查场景覆盖），缺任一项不得提交用户批准；不删失败轮（中止轮用 `abort` 记入），不事后放宽判据；改参数须新 profile revision 并重跑受影响场景；证据不足则清单保持为空。
- **已知坑**：Codex 沙箱内无法启动 PG（共享内存），PG 测试由 lead 在沙箱外跑；给 Codex 审查者的提示避免「篡改／构造事务／绕过」类措辞，会触发内容过滤（改用「字段不一致的修改」「数据一致性复核」）；Codex 只用 workspace-write 沙箱，不用 bypass 参数。
- **相关待排**：[#230](https://github.com/kevinWangSheng/production-ops-agent/issues/230)（重放逐字段完整性），与 U2 无依赖。
