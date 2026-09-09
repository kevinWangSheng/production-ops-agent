# B 本地机制结果

2026-09-08；受检代码为本目录 source-hashes.json 的 dirty 快照，共同基线 e5a971c。Python 3.12.13、PostgreSQL 17.9 Homebrew、psycopg/binary 3.3.3；m0 依赖由协调者锁定。

实际命令与结果：

- `make setup`：成功同步锁；第一次仅基线32包，随后锁新增 psycopg 两包。
- `M0_B_POSTGRES=1 M0_B_RESTART=1 .venv/bin/python -m pytest tests/test_m0_budget.py tests/integration/test_m0_budget_postgres.py -q`：18 passed，含10项实际 PostgreSQL 测试，见 postgres-tests.txt。
- `make check`：Ruff、离线锁检查通过，55 passed、10 skipped；默认跳过真实 PostgreSQL 是显式安全入口，另项实际测试已经执行。见 development-check.txt。
- `scripts/m0/postgres_lab.py start/stop`：实测初始化与启动成功；测试真实停止/重启成功；最终 stop 成功，55431 无监听；既有 PostgreSQL PID4391 仍存活。未重启 Docker/Colima 或他人 PostgreSQL。

核查事实：4个 spawn 子进程争用同一实验，两Run共用100额度，40预留仅两个created；4个同身份调用仅一次created。unknown占用在数据库重启和重建对象/重复初始化后保留。锁等待由 pg_stat_activity 实际观测，在取得锁后 clock_timestamp 检查期限。费用120超出预留50仍全记且阻断；输入、身份、结算冲突拒绝。提交确认丢失注入在真实commit之后抛连接异常，调用者获固定失败、重查仅replay，无第二次发送许可。金额包括超过28位整数精确校验。原始异常没有出现在公开错误或__context__。

结果不覆盖真实模型/trace、真实计费价格、身份/网络权限隔离、F2完整恢复/调度、故障注入矩阵全量、升级、soak或产品验收；本批不打开live或SPEC门槛。提交确认丢失是驱动边界故障注入，数据库停启则为实际进程操作。trust仅本地合成实验；本机其他用户/进程可连接不是安全证明。

资源：原生配置 shared_buffers32MB、work_mem1MB、maintenance_work_mem16MB、max_connections12、无并行worker、autovacuum关闭。一次测试后静态RSS：专属postmaster10032KiB、checkpointer1552、background writer1792、walwriter1744，总15120KiB；CPU采样均0.0%。不是峰值或硬512MiB/CPU1限制，也不是持续容量证明。数据47MiB保留于本worktree tmp/m0-b/postgres；日志 tmp/m0-b/postgres.log。宿主初始16GiB RAM、45GiB空闲磁盘，已有压缩/交换压力，故未启动VM。

失败/处置：初始Docker无法连接且Colima未运行，使用已安装PG17.9独立cluster解决，未修改他人daemon。首次Ruff仅import排序1项，自动修正后检查通过；无被隐藏的测试失败。实现过程中主动修复超大Decimal求和潜在舍入，用整数求和并补回归。生命周期补归属marker、拒绝symlink、端口占用前置拒绝、server data_directory及pid核对；已有本任务cluster在确认路径后补一次marker。陈旧pid或未知cluster拒绝，不自动删除修复。

本地自测完成；独立审查、PR/CI由协调者后续安排，不自称独立通过。后续复用同一cluster需从本worktree运行start；结束stop并保留数据。清理worktree前必须保留该数据库/日志或取得明确删除授权，不能由重建合成实验覆盖原证据。
