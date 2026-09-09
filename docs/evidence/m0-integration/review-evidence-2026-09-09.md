# 扫描证据版本修复：独立复验

日期：2026-09-09。审查者为未参与实现、全新上下文的独立 Agent。范围是本次任务记录差异与 `post-fix-scan-2026-09-09.md/json`，依据 AGENTS、SPEC 证据与交付门槛、C3 §11–13 和 M0 实验合同。结论：本次范围未发现阻断项。

## 版本与历史核查

- 固定提交 `1282ab4bcc53d1530c59ee95d2b1ae671ffc4fe8`，tree `cdafe6287b149d9867a221cd05d849922bc268f4`，由 `git rev-parse <revision>^{tree}` 独立核对。
- `git show <revision>:<path>` 的 blob 经 Python hashlib.sha256 计算，与 JSON 的 sources 及当前源码逐字计算结果一致：扫描器摘要为 `1ff3681f5951aecbdea11392c409c4605560ce41c57ffe047bcd31005ba6b7ca`；安装脚本摘要为 `71e45944ca600a2a1a6d2b3e02ca713da26ffbcc93950802bedb7a11edc2b893`。
- `git show 93f70ab:scripts/check_secrets.py | shasum -a 256` 与原 verification.json 的旧摘要相同；`git log -- scripts/check_secrets.py` 显示其后有精确例外修复 1cb08e0。因此新证据正确区分修复前记录与修复后扫描。
- `git ls-tree -r --name-only <revision> docs/evidence/m0-integration` 列出的 5 份原有工件，逐个与对应 Git blob 比较，全部逐字不变。
- 受检 binary 的本地 SHA256 为 `ba52fb1bfabbcde42f032afad3d6e0b19dff8ed105229a16e7caa338bbc0e84f`，与 JSON 一致。安装脚本的固定 Darwin 包摘要也与 JSON 一致；本次复验复用已安装工具，没有重新下载发行包，下载真实性复验不属于本次新增证明。

## 实际扫描

独立临时仓库位于任务 worktree 的 `tmp/independent-postfix-z4r6td9h`。实际执行 `git init -q`、`git fetch --no-tags <任务worktree> <revision>`、`git checkout --detach -q FETCH_HEAD`；`git status --porcelain` 与 `git for-each-ref` 均为空，`git rev-list --count HEAD` 为 42。只从 Git 对象重建，无原工作区私有文件复制。

在该临时仓库运行：

```sh
python3 scripts/check_secrets.py \
  --root /Users/shenghuikevin/dev/AI/production-ops-agent-m0-evidence-version/tmp/independent-postfix-z4r6td9h \
  --binary /Users/shenghuikevin/dev/AI/production-ops-agent-m0-evidence-version/tmp/gitleaks/gitleaks
```

退出 0，stdout 为 `SECRET_SCAN_PASSED`，stderr 为空。同一脚本实际执行版本检查、合成泄漏/干净样本、精确摘要例外与错误路径/其他值反例，再扫描 tracked snapshot 和固定历史。没有扩大允许名单，脚本与受检提交相同。

随后仅将待审的任务记录和两份 post-fix 工件按精确路径复制并 `git add -- <path>` 到上述独立临时仓库，再运行同一命令：退出 0，stdout 为 `SECRET_SCAN_PASSED`，stderr 为空。故本次新增文档未引入扫描误报。主任务索引未由审查者改变。

## 限制与交接

原 post-fix JSON 只指向固定提交，不声称扫描自身或未来变更；此边界清楚且无循环引用。独立第二次扫描包含待审三文件，但不包括其后生成的本报告；最终提交全范围扫描仍须由最新 CI 证明。未运行数据库、服务、真实模型/trace、付费调用或产品验收，未读取真实 .env。没有提交、推送或合并；临时复验仓库保留供协调者检查和安全清理。


## 2026-09-09 追加：PR #9 批次状态发现复验

针对 discussion_r3965882716，独立只读复核 ROADMAP、批次索引及汇合任务的本次差异；未参与这三份文档的修改。结论：新增当前状态与远程一致，旧过程明确标为历史，未发现阻断项。

实际执行 `gh pr list --state all --json number,state,headRefOid,baseRefName,mergeCommit,url` 及各 PR 的 `gh pr view --json headRefOid,statusCheckRollup,body`：#4 为 92eb7d0、#5 为 ab7f0ad、#6 为 566316a、#7 为 5905792，均 OPEN 且 checks SUCCESS；#8 为 MERGED，目标 components、合并提交 1282ab4；#9 为 OPEN，核查时 head ea661e0 的 checks/m0-postgres 均 SUCCESS。本次未提交改动不借用该 CI 结果，后续仍须最新提交检查。

GitHub GraphQL reviewThreads 实时回读确认：#4–#7 的 7 条原讨论均 resolved；#8 与 #9 的各一条讨论仍 unresolved。文档引用的测试数字、修复范围与各 PR 描述一致；此追加只复核状态和对应来源，没有重跑其他分支测试或重新认证其实现。Git merge-base 检查确认四个下层最新 head 均不是本分支 HEAD 的祖先。

逐个解析三份文档中的本地 Markdown 链接，目标文件全部存在。再次将固定 1282ab4 下的 5 份历史扫描目录工件与当前文件逐字比较，全部一致。SPEC、验收和扫描代码无本轮修改；文档继续明确真实入口、M0 退出和产品验收未通过，不将 PR 合并混作实施门槛开放。按任务范围没有重复固定历史扫描、读取真实 .env、启动服务或执行提交/推送；最终暂存快照及 CI 由协调者执行。
