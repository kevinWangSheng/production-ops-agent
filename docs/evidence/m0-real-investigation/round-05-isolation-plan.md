# M0-05/B5 只读权限与执行隔离方案（待批准）

状态：**PG 只读拒绝子项已执行；K8s/OS 仍未执行，未采购。** 本文只描述验证合同与候选设施，不把开发者权限转化为产品 Agent 权限。

## PostgreSQL 只读角色拒绝证据

在专属、可恢复的 M0 PG 实例中创建临时角色（不进入产品凭据、无生产权限），记录角色、数据库、schema、版本和会话身份。示例 SQL：

```sql
CREATE ROLE opspilot_probe LOGIN PASSWORD '<ephemeral-out-of-band-secret>';
GRANT CONNECT ON DATABASE m0_budget TO opspilot_probe;
GRANT USAGE ON SCHEMA public TO opspilot_probe;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_probe;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM opspilot_probe;
```

只读探针使用独立连接执行：

```sql
SELECT current_user, current_database();
SELECT count(*) FROM m0_v3_subject;
INSERT INTO m0_v3_subject(id) VALUES ('rbac-probe-denied');
UPDATE m0_v3_subject SET state = 'cancelled' WHERE false;
DELETE FROM m0_v3_subject WHERE false;
```

预期输出：前两条 `SELECT` 成功；`INSERT/UPDATE/DELETE` 均为 `ERROR: permission denied for table m0_v3_subject`，事务回滚且行数/快照不变。**本轮未执行这些 SQL**，因为它涉及临时 IAM/密码与专属实例授权；执行时必须将密码留在连接渠道，不进入模型/trace/仓库。

## Kubernetes RBAC

当前 macOS/Colima 现场没有已批准的 Kubernetes 目标与凭据，标记为**环境缺测**，不以未执行的 `kubectl` 或模型拒绝作为通过。待具备隔离集群后，使用临时 ServiceAccount/Role 仅授予 `get/list` 目标命名空间资源，再用 `create/patch/delete` 负例验证 `forbidden`；凭据不写入仓库或模型。

## 已取得的专属 PG 证据（2026-09-12）

角色 `opspilot_probe_20260912` 已在专属 55431 实例创建并保留。`SELECT current_user/current_database` 和 `SELECT count(*) FROM m0_v3_subject` 成功；有效 UUID 的 INSERT、UPDATE、DELETE 均返回 `ERROR: permission denied for table m0_v3_subject`，DDL 返回 `ERROR: permission denied for schema public`。完整安全摘要见 [`round-05-b5-pg-readonly-output.json`](round-05-b5-pg-readonly-output.json)。

第一次探针故意使用非 UUID 字符串，得到 `invalid input syntax for type uuid`，随后因同一事务失败而产生级联 abort；该失败原文未覆盖有效权限判定，已保留并用独立事务重跑，最终权限拒绝以上述结果为准。密码/凭据没有进入模型、trace 或仓库。

## Holmes 宿主 OS 隔离候选

| 方案 | 预估成本 | 优点 | 限制/风险 |
|---|---:|---|---|
| 独立低权限 macOS/Linux 用户 + 目录 ACL | 低（0–0.5 人日） | 快速、无额外基础设施 | 同一宿主 root/管理员仍可读取；不能证明对抗宿主攻击者隔离。 |
| rootless 容器 runner + 只读 bind mount/网络白名单 | 中（0.5–1 人日；现有 Colima 可复用） | 限制文件/网络面，易复现 | 容器逃逸、宿主挂载和 daemon 权限仍需单独审查；不等于 VM 隔离。 |
| 独立 VM/沙箱用户与最小共享目录 | 中高（1–2 人日；暂无采购） | 边界清晰、适合保留集盲测 | 资源/启动成本更高；仍需验证日志、凭据和答案数据流。 |

推荐顺序：先用独立用户验证基础路径，再用 rootless runner 作为 M1 前设施；需要对抗宿主或保留集盲测时采用独立 VM。以上均为候选，不采购、不改变当前产品范围。

## 结论

DB 只读角色拒绝、K8s RBAC 和 Holmes OS 隔离均不是当前 M0 已通过能力。最小 PG SQL 与预期输出已准备，实际证据待用户批准设施与凭据边界后再执行。
