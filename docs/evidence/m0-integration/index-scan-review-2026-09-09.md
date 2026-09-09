# 索引扫描修复独立复验（2026-09-09）

全新上下文、未参与实现的 Agent 对 PR #10 新增 P1 修复独立审查并执行本地反例。结论：本次受测范围未发现剩余阻断项；新增安全行为仍须用户审核后明确授权合并。

- 工作区：`production-ops-agent-m0-main-closeout`，分支 `chore/m0-main-closeout`；基线 HEAD `2424c93054c6e2f642eeb1641592a5c1c1972e9b` 加当前未提交修复。
- 受测 `scripts/check_secrets.py` SHA256：`d6e8dd03cf052b1851bb67ba3bbd7d323d63d9d1fe10ecc3da10dbabd89cc8ee`；`tests/test_secret_scan.py`：`0f24fb7de4635063f39df62a81f31010d8ea94d56b02902eee5396cdfd19fe29`。
- 工具：Gitleaks `8.30.1`，二进制 SHA256 `ba52fb1bfabbcde42f032afad3d6e0b19dff8ed105229a16e7caa338bbc0e84f`；路径 `/Users/shenghuikevin/dev/AI/production-ops-agent-m0-evidence-version/tmp/gitleaks/gitleaks`。

实际执行：

- `make check`：环境、锁文件、Ruff 检查通过；190 passed，13 PostgreSQL 集成测试因未授权启动本地数据库而按默认条件 skip。
- `.venv/bin/python tmp/index-scan-independent-review/review.py`：独立构造合成 Git 仓库，12 项通过。干净仓库 PASS；暂存 canary 后清理或删除工作区、干净索引加未暂存 canary、仅历史含 canary 均 `SECRET_DETECTED`；索引与工作区 symlink、未解决冲突及 gitlink 分别明确拒绝。CLI 输出均为固定状态，stderr 为空，不包含合成 canary。
- 同一脚本验证 1284 字节非 UTF-8/NUL/CRLF blob 精确保留；含正常前序文件及后序 `z/.env` 的索引，在 index/worktree 两种快照中均先拒绝，守卫确认没有调用 `cat-file` 或 `Path.read_bytes`。例外值放到例外路径外仍被真实扫描器拒绝。
- `.venv/bin/python scripts/check_secrets.py --binary /Users/shenghuikevin/dev/AI/production-ops-agent-m0-evidence-version/tmp/gitleaks/gitleaks`：当前仓库 `SECRET_SCAN_PASSED`，退出 0。
- `git diff --check` 通过；逐文件对比 `git show HEAD:<path>`，9 份已有集成证据字节不变。

静态复核：完整索引元数据检查先于内容读取，固定对象 ID 读取原始 blob；索引、工作区、refs 历史分开扫描。无新增 allowlist，原有规则/精确路径/精确值的交集例外保持不变，固定错误输出未放宽。实验脚本与 JSON 原始结果保留在 ignored `tmp/index-scan-independent-review/`，仅使用无效本地合成数据。

限制：本轮为工程扫描检查及离线反例，不证明任意秘密模式均可识别；未测试并发工作区变化的原子性。未读取真实私有配置，未调用模型或 trace，未启动 PostgreSQL，未执行 commit、push 或合并。SPEC 实施门槛、M0 退出和产品验收状态不变。
