# F11 U2 发布观察校准证据

- 状态：进行中（实验工具离线验证完成，真实实验未执行）
- 更新日期：2026-10-14
- 依据：[F11 任务记录](2026-10-12-f11-post-release.md)（U2／P1-02）；[PR-2 合同](2026-10-13-f11-pr2-contract.md) §1 校准 policy、固定语义 1／4、P2-04、「校准证据不足」、r1–r7；[预审与决定表](2026-10-14-f11-u2-calibration-contract.md)；C3 §12「实验环境」；ADR-0006
- 工作区：`chore/f11-u2-calibration`（`../poa-f11-u2`）
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
- 2026-10-14 实验工具 `scripts/f11_u2_harness.py`（Codex gpt-6.1-sol）：占用记录、配置发布、时间线、定时注入与恢复、产品路径登记 pending 发布、旁路采集、离线折叠、汇总；复用产品 `build_release_query_plan`、`evaluate_release_sample`、`advance_release_window`、`instant_vector` 与 `record_release`，窗口算法未重写。独立审查（Codex gpt-6-astra）3 项 P2（注入超时后恢复记录可能过早删除、Pod 消失时间下界、采集代码版本未冻结）已修复并复验；离线测试 50 passed，`register` PG 测试 1 passed（lead 在沙箱外执行）。lead 可逆默认：登记传 `canonical_content(profile)`，与产品 web 一致（`opspilot/web/service.py:684`）；无关故障 flag 为 `adFailure`（集群 `flagd-config` 中核实）。
- 实验库：本机新建专用 PG（55561，迁移至 0016，数据在 `tmp/f11-u2/pg`，不入库）。实验环境：三个 eval／评测会话确认近期不占用。
- 2026-10-14 L3 工具与离线测试完成：入口 `.venv/bin/python -m scripts.f11_u2_harness`，命令见[开发指南](../development.md#f11-u2-有界实验工具r1l3)。复用产品 query plan、sample evaluator、窗口推进及只读 Prometheus source；未重写窗口算法，外围调度／采纳规则逐处标注 PR-2 合同行号。可逆实现选择：用明确标记 UNAPPROVED 的候选 policy 壳满足既有纯函数 DTO，仅读 DB 校验身份；故障时长从首次确认 flagd variant 起算，不将配置回执当业务起病。占用为共用 ledger 目录的本机记录，跨工作区须协调使用同一目录。
- 离线证据：定向 **34 passed**；最终 `UV_CACHE_DIR=$PWD/tmp/uv-cache make check` 成功（ruff 通过、926 文件格式通过、mypy 117 文件通过；pytest **5431 passed / 1427 skipped / 2 xfailed**，118.54 秒；日志 `tmp/f11-u2-make-check-final.log`）。全新上下文 Agent 只读独立审查及增量复验均无未处置阻断项（定向 34 passed）。未启 PG、未访问 kind、未发布、未读取凭据、未生成真实证据或提交。ad flag 名、当前目标五项身份、真实传播和查询成本留待后续实测。
- 本实施阶段四数：新增合同问题 **0**；因合同变更重写测试 **0**；真实环境重跑 **0**；新增用户裁决 **0**（对齐既有 r1）。

## 下一步

基线测量与开发探测 → 正式五类×3 批次 → 证据独立审查 → 用户批准 → policy 写入 PR。
