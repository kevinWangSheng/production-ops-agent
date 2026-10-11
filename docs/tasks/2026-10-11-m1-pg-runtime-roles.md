# M1 调查侧 PG 运行角色拆分（#169）

- 状态：已完成（#198、#200 已合并，2026-10-11；真实 Run 第 4 项随 #199 在 #203 补跑通过）
- 更新日期：2026-10-11
- 依据：[#169](https://github.com/kevinWangSheng/production-ops-agent/issues/169)；C3 §3「运行角色与权限」、§11「部署与 schema 迁移」；M1-03 D8（[任务记录](2026-10-05-m1-03-postmortem.md)）；SPEC 2026-10-11 段
- 工作区：授权 PR `chore/m1-pg-roles`，`../production-ops-agent-pg-roles`；实施 worktree 写在执行节

## 目标与范围

web 与 worker 目前都用迁移 owner 连接 PG（`opspilot/web/__main__.py` `_serve`、`opspilot/worker_main.py` 读同一个 `OPSPILOT_DSN`），owner 不受 GRANT 约束，调查侧任何库层分工都无法证明。本任务让 web、worker 各用非 owner 登录角色、按业务对象最小授权，迁移 owner 只在部署（`make migrate`）时使用。

这是**调查侧增量隔离**，不完成 C3 §3 的 Controller/Worker/Gateway 全隔离：worker 进程仍直接调用持久化写原语、仍在进程内执行 Prometheus/OTel 工具，因此不做网络隔离。完整拆分见 [#197](https://github.com/kevinWangSheng/production-ops-agent/issues/197)。不翻任何 `passes`。

## 已核查的现状

- 运行时启动只做 `verify_head`，不执行 DDL（`opspilot/persistence/base.py` `install`），非 owner 角色启动不受影响。
- Observer 已有独立角色：0003 建 `opspilot_observer NOLOGIN` 组角色并做列级 GRANT，登录角色由部署脚本或测试夹具在迁移外创建；F13 对 Observer 零写已有集成测试（`tests/integration/test_m1_03_knowledge_store_postgres.py`）。
- 迁移中没有 `SECURITY DEFINER`、`LISTEN/NOTIFY`、显式 `CREATE SEQUENCE`；0010 的 `delivery_id` 等 identity 列只需表的 INSERT 权限，不需要序列权限（实施中探针确认，预审原写「需要序列权限」有误）。
- worker 进程内执行工具（`opspilot/tools/otel_demo.py`），必须可达 Prometheus；预审曾推荐「Worker 不可达被调查系统」，与此不符，已改为移出本任务。
- 预审（Codex 只读，2026-10-11）全文在主仓库被忽略的 `tmp/pg-roles/prereview.result.md`，未入库。

## 合同（r1）

用户 2026-10-11 按推荐全部采纳：

- **U1/U5/U10 范围**：两个运行角色 `opspilot_web`、`opspilot_worker`（NOLOGIN 组角色），替换 owner；worker 继续直接调用持久化原语；不做网络隔离、不拆 Controller/Gateway（#197）。文档如实写「调查侧增量隔离」。
- **U2 角色与凭据**：迁移只建 NOLOGIN 组角色与 GRANT；登录角色与密码由部署脚本或测试夹具创建并加入组角色，同 Observer。密码不进迁移、不进仓库。
- **U3 DSN**：新增 `OPSPILOT_WEB_DSN`（web）与 `OPSPILOT_WORKER_DSN`（worker，含 knowledge worker 与清扫线程）；缺失即退出，**不回退** `OPSPILOT_DSN`。`OPSPILOT_DSN` 只供迁移与部署工具使用。
- **U4 新表权限**：不用 `ALTER DEFAULT PRIVILEGES`；每个迁移对运行角色显式最小授权（必要处列级）；加目录测试：任一表对每个运行角色没有明确授权决定（授予的权限集或显式「无」）即失败，防止后续迁移漏授权或过授权。
- **U6 函数/序列**：运行角色直接写表，不引入 `SECURITY DEFINER`；identity 序列按需授权；运行角色不得有 schema `CREATE` 与数据库 `TEMP`。
- **U8 旧库**：既有 lab/归档库保留原 owner，迁移新增组角色与授权；升级按 C3 §11 停 web、worker、Observer 后迁移。
- **U7/U9 验收**：确定性 PG 负向测试至少证明 worker 不能执行审核动作、web 不能生成草稿或领取 worker 租约、Observer 不能写调查与 F13 对象，且合法路径在非 owner 角色下通过；另加一次有界真实 Run，web、worker、Observer 全部用非 owner 角色，证明授权完整。
- 预审其余可逆默认（D1–D9：命名、schema USAGE、advisory lock、`alembic_version` SELECT、列级授权按 SQL 实际列、回滚保留 owner DSN、测试夹具、权限快照、fail-closed）由实现者按预审推荐执行，偏离时在执行节写一行理由。

## 计划

1. 授权 PR：SPEC 段、ROADMAP 行、本记录（用户门）。
2. 实施 PR（`feature/m1-pg-runtime-roles`，用户门）：迁移 0011 组角色与授权、web/worker DSN 切换、lab 与 CI 显式创建登录角色、开发指南、目录测试与负向测试、真实 Run 证据。验收与合同测试由未参与实现的 Codex 按本合同编写；独立审查走 Codex。开工前对授权矩阵做一次实施预审（由 SQL 推出的每进程每表权限）。

## 执行进展与证据

- 2026-10-11：合同预审（Codex 只读）与用户裁决 r1；开 #197 跟踪完整 C3 §3 拆分。
- 2026-10-11：实施（`feature/m1-pg-runtime-roles`，`../production-ops-agent-pg-roles-impl`，PR #200）。并行线：实现（Opus 5.5）、独立合同测试（Codex，只依据 r1 与公开接口）。lead 定接口 r2（P1–P6）；独立测试与实现先后暴露歧义，lead 裁决 r3（Q1 共用 F13 表的审核写入由角色感知触发器 42501 拒绝；Q2 退回修改可写再生成请求列；Q3 Observer 既有授权不变；Q4 ACL 口径）、r4（实现者停下上报的 C1–C3 取 A：审计行角色守卫、生成任务列级授权、web 只能把 Run 租约置空的守卫）、r5（角色是集群级，downgrade 只撤销 0011 授予的权限）。可逆选择：identity 列不需序列权限（探针确认，预审原述有误）；FOR SHARE 需要的锁用列级 UPDATE；两角色均无挂起写权限。测试侧缺陷 6 处由测试作者修，实现未为测试改断言。独立审查（Codex）P2「downgrade 用 DROP OWNED 会删既有对象」已修（`f698d76`），复验可接受；复验指出 r5 原措辞与 r2 冲突，lead 更正 r5 措辞，代码不变。检查：独立测试 46/46 ×3；`make check`（含 PG）4527 passed；CI 三项通过。机器人分诊：P1（stale 转换未校验旧状态）不采纳，0008 版本守卫对所有角色已拒绝；P2 lab 辅助脚本复用登录角色时补查 INHERIT/REPLICATION/BYPASSRLS（`c08287f`）与 P1 0011 复用同名既有角色前须为纯 NOLOGIN 组角色、否则迁移失败（`2e83627`，源于 lead 接口 r2「复用且不改属性」措辞）均采纳；lead 自核修复差分。
- 2026-10-11：真实 Run（新上下文 Agent，kind lab，web/worker/Observer 均非 owner，证据 `docs/evidence/m1-pg-roles-live/`（随实施 PR #200 入库，本 PR 不含），Codex 复核证据可接受）：告警接入、调查发布、恢复确认通过；PG 日志 0 次 42501/permission denied；96 次连接快照无 owner；余额差 0.12 CNY。第 4 项失败：复盘生成因 traces 视图 49,521 字节超过 32 KiB 上限 `INPUT_TOO_LARGE`（产品既有限制，#199），worker 草稿写入与 web 审核动作在真实环境未执行，只有 PG 集成测试证据。

## 下一步与交接

- #198（`2cca32c`）与 #200（`d8213f0`）2026-10-11 由 lead 按用户意图 squash 合并（用户告知已合并时两者仍 OPEN 且 CLEAN）。
- 第 4 项补跑（2026-10-11，#203，[证据](../evidence/f13-view-compaction-live/run.md)）：在本任务真实运行库的副本上，web/worker 以非 owner 登录角色运行，worker 在 v3 输入策略下生成真实模型草稿并进入审核，web 以 Basic Auth 批准并发布知识条目；PG 日志 0 次 42501/permission denied，连接身份无 owner。U7/U9 由两次真实运行合计满足：告警→调查→恢复（[#169 证据](../evidence/m1-pg-roles-live/run.md)）+ 草稿→批准。退回、驳回、替换、撤销只由 PG 集成测试覆盖。
- 清理：三个 worktree 与本地分支已删；被忽略的实验数据（真实 Run 的 PG 数据目录、原始 ledger、驱动脚本与日志、临时 PG）移至主仓库 `tmp/pg-roles-archive/`，未入库。
