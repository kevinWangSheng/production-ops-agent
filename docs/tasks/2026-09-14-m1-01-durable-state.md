# M1-01 持久化与恢复

- 目标：完成 PG 业务状态/断点、人工追问纠正取消、重启与不兼容处理。
- 依据：`SPEC.md` 实施门槛/Operating constraints/Verification and delivery；`PRODUCT-CONSTRAINTS.md`；`docs/design/technical-proposal-2026-09-07.md` 第 4、5、6 节；`docs/evidence/m0-real-investigation/m0-exit-matrix.md` 的 M1-01 拆分。
- 范围：`opspilot/persistence.py` 与确定性测试；不修改 `scripts/`、`feature_list.json`、SPEC 门槛或并行分支。
- 当前进展：已建立 feature 分支并实现 PG 业务状态、租约/断点、原子预算、控制代际、迟到结果历史化与版本不兼容阻塞接口。
- 完成条件：四项 M1-01 持久化与恢复条件均由 PostgreSQL 集成测试覆盖；`make check` 通过；独立审查与 CI/code review 待完成。
- 实验/费用：本任务不调用模型或外部服务；仅使用本地 PostgreSQL lab，未知费用为 0。
- 未完成：测试、独立审查、提交/推送/PR、CI 与 code review 处置。

## 验证记录

- `make check`：1036 passed，56 skipped；Ruff 与格式检查通过。
- `M1_DURABLE_POSTGRES=1 .venv/bin/python -m pytest -q tests/integration/test_m1_durable_state_postgres.py`：2 passed。
- 本地 PostgreSQL lab 已启动于 `scripts/m0/postgres_lab.py start`；无模型/trace/外部付费调用。
- 独立审查：进行中；尚未提交、推送或创建 PR。

## PR 交付状态

- PR #19：`https://github.com/kevinWangSheng/production-ops-agent/pull/19`
- 提交 `68c423a` 的 CI：`checks` 与 `m0-postgres` 均通过。
- 提交 `68c423a` 的普通 Codex code review 已完成，无具体发现；security review 因额度不足不可用，按项目规则不作为门槛。
- 未合并，等待用户审核。
