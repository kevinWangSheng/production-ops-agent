# F11 四步验收测试：合同解释与已知缺口

依据：`docs/tasks/2026-10-24-f11-pr5-contract.md` §4 第 1–4 步与裁决（U-1..U-4 = A，U-5 见下）。测试文件 `test_f11_step{1,2,3,4}_*.py`，共用 `f11_steps_support.py`。

## 已知产品缺口（测试保留为红）

- G1 第 1 步第 3 条「`released_at` 与载荷逐字节相等」：页面（JSON 与 HTML）按数据库会话时区渲染该时刻。会话时区为 UTC 时字节相等；非 UTC 时（本测试库固定为 `America/Los_Angeles`）显示 `...-07:00`，时刻相同、字节不同。对应测试：`test_item3_released_at_is_shown_byte_for_byte_as_delivered`。其余字段（digest、目标五字段、`source_release_id`）字节相等，已通过。

## 解释

- E1（U-5，已裁决，只作背景）：实验环境没有 CI 门禁，CI 层用代码面证明（`opspilot/` 不导入 K8s／forge／CI 客户端；不出现 kubectl 和 CI/forge API 端点；Observer 进程只有 Prometheus 源做网络 IO，运行时出站连接只到配置的 Prometheus），加上三个产品入口不读取 K8s／CI／GitHub 凭据（静态扫描各入口进程加载的产品模块中的环境读取与凭据路径）。
- E2 「不调用 kubectl／子进程」：`opspilot/schema.py`（迁移工具，不是三个产品进程之一）用 `subprocess` 调 `pg_dump`。按「子进程只用于该工具、且只调 pg_dump」放行，其余模块不得启动进程。
- E3 「构造延迟回归采样」：充足的健康采样通过 Observer 存储 API（`claim_due_samples`／`submit_release_sample`）提交，窗口由测试指定；退化采样走 `ObserverLoop.poll_once` 加假 Prometheus。原因：健康确认需要 M=1500 s 的连续覆盖，只用实时 tick 做不到。授权时刻回填（`authorized_at`）沿用 PR-2a／PR-4 测试的超级用户构造。
- E4 第 2 步第 3 条「无关并发故障」：用另一目标（frontend）上的开放告警事故，以及另一个同时退化的发布来表示。checkout 整服务 context 退化不算无关故障：PR-2 合同规定「只有整体 context 退化 → unknown」，第 4 条已覆盖。
- E5 第 2 步第 4 条：「映射不足」用新 ReplicaSet 的 Pod 运行旧镜像来构造；「流量不足」用 after 请求率 0.001 < 0.008 构造。两种情况都在到期后清扫为 unknown／`deadline_expired`。
- E6 第 4 步第 5 条「解除后不自动恢复」：解除后旧会话可能被收为 revoked。测试只要求不产生新采样、新会话、新授权或 resume 审计，没有 authorized 会话。
- E7 第 4 步第 2 条「缺 Origin 或跨站 → 拒绝」：合同没有给出状态码，测试接受 400 或 403，并要求不写任何行。
- E8 第 4 步第 4 条「42501」：对 web 角色有列授权、由守卫触发器拒绝的列，同样要求 SQLSTATE 42501（grants.py 的 TRIGGER_GUARDED 约定）。
- E9 每个模块使用独立的临时数据库。每个目标使用独立的 `cluster_uid`，因为同一注册表里 (cluster, namespace, workload) 相同会触发 PR-3 r2 的 `TARGET_AMBIGUOUS`。
