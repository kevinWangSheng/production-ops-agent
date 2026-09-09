# PR #8：修复后扫描器的固定版本证据

2026-09-09 新增的[原始扫描结果](post-fix-scan-2026-09-09.json)仅证明 components 提交 `1282ab4bcc53d1530c59ee95d2b1ae671ffc4fe8` 的扫描器和仓库快照通过实际扫描。它包含修复后的扫描器 SHA256、Git tree、Gitleaks 版本/发行包与 binary 摘要、完整命令及固定输出。实际扫描退出 0，输出 `SECRET_SCAN_PASSED`；版本自检、合成泄漏/干净样本和精确例外的正负自检由同一受检脚本执行。

[历史 verification.json](verification.json) 保持原样，其 `scripts/check_secrets.py` 摘要属于修复前版本。历史结果不是修复后扫描的直接证明；[原独立审查报告](independent-review.md)另行记录当时的复验。本次新结果不覆盖或改写这些历史工件，也不修改扫描器或扩大例外。

受检仓库由本地 Git 对象构建，不复制原工作区：detached HEAD 固定上述提交、没有其他 refs、工作树干净，`--all` 对应该 HEAD 的 42 个可达提交及当前 tracked snapshot。没有读取真实 `.env`，不包含其他 worktree 的未提交、ignored/untracked 文件或并发变化的分支 refs。

本次结果文件在扫描后生成，**不属于上述受检提交，也不声称自身已被该次扫描覆盖**。后续补充文件与最终 PR 提交的全范围扫描以各自最新 CI 为准。这避免了“生成结果后再改变结果所声明的受检输入”的循环。仅为离线扫描证据；没有重跑 PostgreSQL、模型/trace、运行时出口或产品验收。

## 独立复现

在仓库中执行以下命令，`scan_root` 为本次新建的临时 Git 仓库；固定提交须已可从本地对象读取。发行安装按受检版本脚本验证官方包固定 SHA256，不读取私有配置。

```sh
source_repo="$PWD"
scan_root="$(mktemp -d -t m0-post-fix-repro)"
git -C "$scan_root" init -q
git -C "$scan_root" fetch --no-tags "$source_repo" 1282ab4bcc53d1530c59ee95d2b1ae671ffc4fe8
git -C "$scan_root" checkout --detach -q FETCH_HEAD
git -C "$scan_root" status --porcelain
git -C "$scan_root" for-each-ref
git -C "$scan_root" rev-parse HEAD 'HEAD^{tree}'
git -C "$scan_root" rev-list --count HEAD
(cd "$scan_root" && python3 scripts/install_gitleaks.py --directory tmp/gitleaks)
(cd "$scan_root" && python3 scripts/check_secrets.py --root "$scan_root" --binary tmp/gitleaks/gitleaks)
shasum -a 256 "$scan_root/scripts/check_secrets.py"
```

预期 status/refs 为空，commit/tree 与 JSON 相同，commit count 为 42，扫描退出 0、`SECRET_SCAN_PASSED`，扫描器源码摘要与 JSON `sources` 对应项完全相同。Linux 的固定发行包/binary 摘要与本次 Darwin 不同；源码摘要和固定受检 tree 必须相同。保留或按常规安全规则清理仅此次创建的临时仓库，不操作现有任务 worktree。
