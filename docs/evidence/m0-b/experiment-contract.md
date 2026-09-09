# B 合成账本实验合同（执行前）

日期 2026-09-08；依据 SPEC、C3 §7/13、M0 §1/3/7、共享接口 v1。只验证账本机制，非 F2 实施或真实费用授权。

现场 PostgreSQL 17.9 Homebrew 已存在，另有 PID 4391 服务，保持不动；Docker/Colima 未运行，不启动。16GiB RAM，45GiB 磁盘可用，内存压力显著。使用专属 tmp/m0-b/postgres 持久目录、Unix socket 目录、loopback 55431、合成专用库 m0_budget。原生 macOS 无 cgroup，CPU1/512MiB 仅容器候选，不能宣称硬限制；改为 shared_buffers=32MB、work_mem=1MB、max_connections=12、max_parallel_workers=0、max_worker_processes=0、autovacuum=off 的短时串行实验，测 RSS。不得触碰其他服务或唯一数据。

版本：PostgreSQL 17.9；Python 3.12（锁）；psycopg[binary] 3.3.3 待协调者锁。官方时钟依据：https://www.postgresql.org/docs/17/functions-datetime.html，clock_timestamp 在锁等待后取得实际时间。

启动命令由 scripts/m0/postgres_lab.py 的显式 start/stop/restart 实现，仅固定专属目录；initdb 无用户数据输入、trust 仅本机合成实验。启动后创建专属 m0_budget。测试通过 M0_B_POSTGRES=1 显式选择，默认跳过实际集成；断言跨进程竞争最多额度允许的新记录、同请求至多一次 created、不同 Run 共享额度、未知保留、重复输入/冲突、超额实际全记且阻断、锁等待跨 deadline 拒绝、数据库停启后不恢复额度、连接错误不泄漏。

命令：make setup；定向 pytest tests/test_m0_budget.py tests/integration/test_m0_budget_postgres.py；启动前后资源/端口检查；停止后确认专属服务关闭，原服务存活，数据保留。实际 restart 测试须额外 M0_B_RESTART=1。失败保留日志，修复后重测；服务不可用时先修复本任务环境，不改他人服务。最终留结果/版本/文件哈希及资源摘要于本目录；原始数据库留 ignored tmp，删除 worktree 前需保留它。本轮模型/trace 调用均为 0。
