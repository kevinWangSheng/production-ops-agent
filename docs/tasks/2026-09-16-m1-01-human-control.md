# M1-01 子任务：追问/纠正/取消（人工控制）

## 目标与范围
在 DurableStore 控制代际基础上落实 C3 人工控制、全局/目标级暂停，以及 follow-up/correction/cancel 的持久语义；迟到结果仅登记历史，不得覆盖新代际。范围为领域、持久化和服务接口，不含 UI/SSE。

## 依据与前提
依据 `SPEC.md` M1-01 bounded-open 门槛、`PRODUCT-CONSTRAINTS.md` 人工控制优先级、C3 技术方案及现有 DurableStore 合同。分支基于 `origin/chore/durable-store-hardening`；与重启子任务共享 `persistence.py`，本任务未扩展其结构之外的改动。

## 实现与证据
- 领域控制模型位于 `opspilot/domain/control.py`，实现控制代际条件更新、全局与目标暂停、人工优先级和 scope 栅栏。
- 持久化控制入口为 `DurableStore.control()`；`publish()`/步骤提交共用代际与租约栅栏，迟到结果写入 `late_result` 历史。
- 领域确定性断言见 `tests/test_domain_contracts.py`、`tests/test_domain_state_machines.py`；PG 控制/迟到结果合同见 `tests/integration/test_m0_control_contracts_postgres.py`、`tests/integration/test_m1_durable_state_postgres.py`。

## 验证
- `make setup`：成功，创建并同步 `.venv`（Python 3.12.13）。
- `make check`：通过；ruff、format、mypy 均通过；pytest `1051 passed, 84 skipped, 2 xfailed`。
- PG 集成测试因 `M1_DURABLE_POSTGRES=1` 未在本次环境设置而按项目约定跳过；未将跳过结果宣称为 PG 运行证明。

## 未完成项与下一步
需要在具备本地 PostgreSQL 的环境中运行 `M1_DURABLE_POSTGRES=1` 的并发追问/取消和迟到结果测试，并完成独立新上下文审查、提交/推送和 PR 的 CI/code-review 闭环。当前未修改 feature passes 或扩大产品权限。
