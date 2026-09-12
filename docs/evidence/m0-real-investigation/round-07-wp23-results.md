# 工作包 3：wp23 控制/观察/故障恢复结果

执行日期：2026-09-12。依据 M0 计划 §3；无新增 trace 上传。专属 PG 55431 已启动、执行、stop，数据目录保留。

## PG 合同

- `test_target_pause_denies_dispatch_and_resume_requires_new_run`：暂停期间 dispatch/tool/claim/new_run 均被 `PAUSED`/控制拒绝；resume 不隐式恢复旧 Run；显式新 Run generation 2 才可继续。
- `test_global_pause_covers_untargeted_subjects_and_is_idempotent_safe`：全局暂停覆盖未指定 target，重复 pause/resume 返回冲突；预算/旧状态不被隐式重置。
- `test_observer_authorization_is_independent_of_investigation`：observer 自有 experiment/window/query limit，不借 investigation fence/预算；暂停也拒绝 observer query。
- `test_observation_stream_rejects_out_of_order_and_unknown_revision`：captured_at 乱序、重复和旧 profile revision 均写审计并返回 unknown/拒绝；后续新样本仍可接受。
- `test_short_db_outage_resumes_from_committed_state_without_double_commit`：PG stop/restart 后 committed response/operation 保留，重复提交幂等，step/operation/attempt/dispatch 各 1。

结果：**5 passed**（`-rA` 输出见 [`round-07-wp23-postgres.txt`](round-07-wp23-postgres.txt)）。

## 真实 DeepSeek + PG 增量 Run

脚本 [`round07_wp23_pause_real_probe.py`](../../../scripts/m0_lab/round07_wp23_pause_real_probe.py) 先在 PG 中 pause target：新 dispatch 被 `PAUSED` 拒绝并审计；resume 后显式 new Run generation 2，发送 1 次真实 DeepSeek HTTP 200，response accepted=true。Run/ledger：[`round-07-wp23-real-run.json`](round-07-wp23-real-run.json)、[`round-07-wp23-real-ledger.json`](round-07-wp23-real-ledger.json)。

- HTTP：1；known cost upper `0.001749 CNY`；trace uploads 0。
- 审计事件：pause accepted、PAUSED denied、resume accepted、new_run accepted、model_response accepted。
- 首次 probe 因 body `max_tokens=1024` 触发既有 envelope guard，0 HTTP；原始失败见 [`round-07-wp23-real-run-initial-failure.txt`](round-07-wp23-real-run-initial-failure.txt)。修正为 profile 允许的 32768 后只执行一次真实请求，未用失败样例补分；成功摘要见 `round-07-wp23-real-run-success.stdout`。

## 状态边界

本轮证明的是实验层控制/观察/PG 恢复合同和一条真实模型接线，不证明完整产品生命周期、生产健康、发布升级或 M1 入口；K8s RBAC、Holmes 宿主 OS 隔离和正式 judge/账单仍按矩阵保留缺口。
