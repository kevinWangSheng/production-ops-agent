# M0 汇合独立审查

日期：2026-09-08。审查者是未参与协调实现或 A/B/C 的全新上下文 Agent。对象：`93f70ab6c73c6f022478e9a2eef2d4e5ae923e89`，相对 components `51c892a912fefe5b4123fbcd63b36866efcaf954`。初始工作树干净；仅本报告由审查者写入，未改实现。

## 结论

**原 I-R1 已在修复提交 `1cb08e0` 独立复验关闭，本批有界本地集成审查通过。** 实际数据库汇合通过；修复后的当前跟踪文件及 Git 历史扫描通过。以下保留首次失败与处置依据。未改变 SPEC、PRD、feature_list.json，未开放产品实施或验收门槛。

## I-R1 / P1：提交证据后秘密扫描失败

实际运行 `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks` 返回 `SECRET_DETECTED`，退出 1。仅提取已 redacted 报告的规则、文件、行和 commit 定位：`generic-api-key` 命中 `docs/evidence/m0-integration/verification.json` 第 56、58 行。两者都是源码 SHA256，属于误报；Git 历史扫描同样命中上述受检提交中的两行。协调者证据记录的扫描通过发生在证据提交之前，不能证明当前 HEAD 通过；照此推送会使新增必需扫描步骤失败。

建议对这两项明确且可审计的误报进行精确处置，并覆盖历史。不要禁用 generic-api-key 或整份证据文件扫描；只修改当前文件无法消除 `--all` 中的历史命中。修复后须重新实际运行当前跟踪文件与历史扫描，并验证真实随机合成 canary 仍拒绝。

## 独立执行证据

- `make check`：doctor、offline lock、Ruff lint/format 均通过；135 passed / 13 skipped，1.15s。数据库默认 opt-in 跳过与显式运行结果分别记录。
- 从 B worktree 运行 `.venv/bin/python -m scripts.m0.postgres_lab start`；集成 worktree 运行 `M0_B_POSTGRES=1 .venv/bin/python -m pytest tests/integration -q`：12 passed / 1 native restart skipped，2.66s；随后从 B worktree stop 成功。只复用 B 专属数据库，数据保留，未操作既有 PostgreSQL PID4391。
- 三项 A+B 汇合包括正常两轮实际累计账本结算及 trace 替身回读、断流 unknown 跨 Run 拒绝超额、连接失败零发送；其余 B 实际数据库合同测试一起通过。原生 restart 未在本审查重跑，B 独立报告另有实际证据。
- live 实际返回 denied / LIVE_NOT_ENABLED / network_calls=0；源码仍无条件拒绝。
- 真实 Gitleaks binary 在独立临时 Git 仓库：干净样本 SECRET_SCAN_PASSED；未提交但已跟踪文件中的随机合成 api_key 为 SECRET_DETECTED；删除 canary 后仅历史残留同样 SECRET_DETECTED。
- 假扫描器反例：错误版本返回 SCANNER_VERSION_INVALID；正确版本但扫描退出 7 返回 SCANNER_FAILED；两者退出 1、stderr 为空，未回显扫描器原始错误。既有测试另覆盖缺失扫描器、误跟踪 .env 读取前拒绝、symlink 拒绝和未跟踪私有内容不复制。

## 静态审查与边界

已读完整 AGENTS/SPEC、ROADMAP、C3 §5/7/11–13、M0 计划、batch/shared-contract/integration 任务、原始集成失败和结果工件；检查增量及 A/B/C 独立发现修复记录。A/B/C 按普通本地 merge 聚合，集成增量 10 文件，未修改实现模块或验收清单。任务记录明确 components 只是依赖快照，PR/合并仍需相应流程，当前不声称远程 CI 已通过。

CI action、uv/Python、Gitleaks 发行包 SHA256 及 PostgreSQL image digest 固定；contents read、checkout 不保留凭据、无业务 Secrets、无 pull_request_target。数据库 service 1 CPU / 512 MiB、job 15 分钟限制；合成 trust 身份不冒充产品权限。发行安装仅从已核验哈希包取 gitleaks 常规文件。网络下载和 Linux service 未由本审查运行，需实际 CI 证据。

没有读取、复制或打印真实 .env，没有真实模型/trace、云采购、生产、PR 合并；只运行授权本地合成检查。替身协议、实际本地 PostgreSQL 与静态 CI 检查不等于真实服务组合兼容、完整恢复、M0 退出、72 小时 soak 或产品验收。


## I-R1 修复后独立复验

受检提交 `1cb08e0`。固定 Gitleaks 配置仅在 generic-api-key 规则下，对明确 manifest 路径后缀与两个已核查非秘密源码摘要作 AND 例外；值使用锚定完整匹配。没有重写历史、禁用整条规则或排除整份证据文件；两种扫描路径表示分别是工作树绝对路径和 Git 相对路径。

独立实际执行：

- `python3 scripts/check_secrets.py --binary tmp/gitleaks/gitleaks`：退出 0，`SECRET_SCAN_PASSED`。内部实际正负自检、当前跟踪文件及全部 Git 历史均通过，历史中的原误报已准确处理。
- `.venv/bin/python -m pytest tests/test_secret_scan.py -q`：4 passed in 0.17s。
- 独立临时目录自拟反例：两个已核查摘要分别放入指定 manifest 均允许；同路径随机 canary、同路径摘要改一个字符、已知摘要置于其他路径、已知摘要置于 `verification.json.bak` 均检出。未把已知公开摘要当作真实凭据。
- 独立临时 Git 仓库：在指定 manifest 提交随机 canary，再提交删除，只留下历史；Git 模式仍检出，证明路径例外没有屏蔽历史中的其他值。

I-R1 关闭，当前没有新增必须修复发现。本次只改扫描器，未重启数据库或重复无关协议集成；前述实际 PostgreSQL 结果仍适用。远程 Linux CI 仍待协调者实际运行，不能用本地通过替代。
