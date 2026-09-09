# 持久诊断与资源状态独立审查

日期：2026-09-09。审查者为未参与实现、以全新上下文启动的独立 Agent。范围限定于远程 review comments 3968243058、3968243067、3968243074 对应修复；基线 `70850a2` 及本轮未提交的五文件 diff。已阅读当前 AGENTS、SPEC、ROADMAP、C3 §11、相关业务/事务实现、测试和资源计划；未读取真实 `.env`、批准文件或原实验数据库行，未发起外部请求、模型调用或 trace 上传。

结论：本轮范围内未发现阻塞问题。此结论是代码审查、离线替身和专属本地 PostgreSQL 验证，不是 Flash 真实协议、M0 退出或产品验收通过。

- `LiveLedger.save` 在同一事务更新业务/outbox/usage 并写入固定 business code；`trace_status` 在同一事务更新 trace 状态及 code。新增 `m0_live_diagnostics` 以外键关联原记录，安装 SQL 没有回填或更新原实验行。出口异常分支尽力保存固定分类，不保存供应商异常正文；既有业务失败不会新增 trace 上传权限。
- 项目 GET 的合法 JSON 非字典结果在读取字段和任何模型请求之前返回 `LIVE_PROJECT_RESPONSE_INVALID`。新增参数化用例覆盖 null、数组、字符串、整数和布尔值，并断言只占用 project 请求槽。
- 资源计划已区分原 Pro 单次实际证据、当前 Flash 本地默认切换与未运行的真实验证；旧费用期限不能复用，产品 gate 仍关闭。2026-09-08 批次状态已明确标为历史。审查未独立重查外部账号或 PR 状态。

实际验证：

1. `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/test_m0_live.py tests/integration/test_m0_live_postgres.py -q`：67 passed in 1.67s。包含替身出口失败的诊断与返回值一致性，以及新连接读取业务/trace code。
2. 额外本地 PostgreSQL 故障注入：仅 claim 随机新 experiment id；对 `trace_status` 传入违反 NOT NULL 的 code，使第二条语句失败，前后按该 id 读取两表均不变；成功保存基线后，对 `save` 传入无法适配的 code，使第二条语句失败，业务/outbox/usage 与诊断仍全部保留基线。两次均返回固定 `STORAGE_UNAVAILABLE`。证明诊断写入失败不会留下单边状态提交。
3. 额外离线替身故障注入：trace 状态存储持续抛 `BudgetError('STORAGE_UNAVAILABLE')`；结果为 business completed、trace unknown、`LIVE_STORAGE_UNAVAILABLE`，已保存业务保留，HTTP 替身仅收到 project 与两次模型请求，未发出 trace HTTP。
4. `git diff --check`：退出 0。

局限：存储不可用时不能保证诊断已落盘；进程可能只返回固定 storage code，不能将其表述为持久化成功。进入业务段前的 validate/claim 失败仍不属于本轮逐次诊断持久化保证；没有验证进程 crash 全矩阵或自动导出恢复。本审查未检查原真实行 hash；该项由主执行者独立比较和记录。本地测试只创建随机新 id 的合成记录，未启停 PostgreSQL，未改变旧行。
