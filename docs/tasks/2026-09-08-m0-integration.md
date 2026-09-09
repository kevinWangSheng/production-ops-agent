# M0 汇合与秘密扫描

- 当前状态（2026-09-09）：#8/#9 内容及新增索引扫描修复已由 PR #10 合入 main（e5ecfc0），主线 CI 成功。最终交接见[批次索引](2026-09-08-m0-batch.md)，下文为此前阶段的证据与交接记录。
- 工作区：production-ops-agent-m0-integration / chore/m0-integration；依赖快照 chore/m0-components（普通本地 merge A/B/C，不代表任何 PR 已获准合并）。
- 依据：SPEC 门槛与数据出口、C3 §5/7/11–13、M0 §1/2/3/6/7、本批用户授权和[索引](2026-09-08-m0-batch.md)。

## 实验合同与完成条件

协调者只整合公共入口/检查、累计预算与模型替身的汇合测试、Gitleaks及状态，不覆盖执行者模块。A/B/C分别接受fresh独立审查及修复；汇合代码另外交未参与实现的fresh agent审查，不能由协调者认证。

1. 使用 A MockTransport + B 唯一任务 PostgreSQL（从 B worktree 启停，55431），注入合成用量：完成两轮只扣已知用量；断流后保留unknown并在同实验新Run拒绝超额；服务不可用时不得发送；trace替身白名单回读，结果不冒充C的已接受调查结果。每个实验UUID独立只是合成数据，不代表独立真实额度。
2. 固定Gitleaks8.30.1官方发行包与SHA256；扫描Git历史和当前已跟踪文件快照，不读取.env或其他ignored/untracked私有文件。合成泄漏必须导致失败，缺失/版本不匹配/扫描器错误固定失败；stdout只打印固定状态，scanner输出不回显，使用100%redact。扫描不证明运行时出口安全。
3. CI保持无业务Secrets，加入同一扫描器的实际正负路径和固定digest PostgreSQL17.9合成集成检查；本地专属数据库停止后才触发远程重型作业，最多一组重型环境。CI不运行B原生实例restart，重启证据单列为本地运行。
4. make check、真实本地PG汇合测试、秘密扫描与独立复核完成后推送依赖快照及有界PR，等最新CI。PR base为components，只显示本批集成增量；列出下层PR和推荐顺序。用户批准合并下层后才能将集成PR重定向main再验差异/CI，不能自动合并。

失败保留输出/代码hash与处置，证据保存docs/evidence/m0-integration。真实模型/trace为0；.env不读不复制；区域/账号/预算/期限授权及真实入口独立验证尚未就绪，live始终拒绝。M0退出、产品验收及部署均不由本批完成。

## 进展、资源与交接

A/B/C初始实现已普通合入依赖快照；独立审查进行中，尚不能视作全部审查通过。B专属cluster当前由B审查者独占，协调者等待其停机再复用。数据与原worktree全部保留，不清理唯一证据。

## 自测结果

A/B/C各自独立发现已关闭；当前汇合自测135 passed/13默认skip，显式PG集成12 passed/1原生restart skip，扫描器实际正负自检及跟踪源码/历史扫描通过，live退出3。两次测试失败及处置见[结果](../evidence/m0-integration/results.md)，原始版本/hash见[verification](../evidence/m0-integration/verification.json)。B专属PG已stop，下一步fresh独立集成审查；不能由协调者自检宣布该门槛通过。

## 独立审查与修复

全新上下文review_integration独立验证135普通tests、12实际PG集成（1原生restart skip）；I-R1为新证据源码hash误报，精确例外修复1cb08e0后独立重跑当前文件/全历史扫描及7项反例通过，原发现已关闭。[独立报告](../evidence/m0-integration/independent-review.md)。本地实现和独立审查完成，PR/CI尚待提交回读；不自动合并。最终公开PR描述记录最新提交checks，避免把早期自测当最新提交结果。

## 2026-09-08 PR/CI及交接（历史）

[PR #8](https://github.com/kevinWangSheng/production-ops-agent/pull/8) base=chore/m0-components 51c892a。0e2435d 的GitHub run34318679519：checks成功（含Gitleaks），m0-postgres成功（实际12 passed/1原生restart skip）。[五PR快照](../evidence/m0-integration/pr-ci-snapshot.json)记录核查时各head及checks；这是固定快照，后续文档提交最新CI以PR实时状态和最终描述为准。五个PR均OPEN、未合并。

本批本地/独立审查/PR交付已完成；仍未真实实验、M0退出或产品验收。main/旧分支不动；B唯一数据和日志保留、专属端口已无监听；原有PID4391仍存活。无其他任务后台进程。全部worktree保留待用户审核与合并后安全清理；清理B前必须保留独有实验数据证据，不强删。


## 2026-09-09：PR #8 扫描证据版本修复

- 发现：[GitHub P2](https://github.com/kevinWangSheng/production-ops-agent/pull/8#discussion_r3965282855) 指出历史 verification.json 中的扫描器摘要属于允许名单修复前，无法单独证明修复后的扫描器已运行。原始 JSON 与其余历史证据保留不改。
- 工作区：`production-ops-agent-m0-evidence-version` / `chore/m0-evidence-version`；从最新远程 components `1282ab4bcc53d1530c59ee95d2b1ae671ffc4fe8` 新建。原 components 工作区干净但本地分支仍为历史依赖快照，未擅自切换或同步其他工作区。
- 实验合同：只补充固定提交的扫描证据，不改扫描器、安全例外、产品代码或验收。依据 SPEC 的证据/数据出口/验证门槛、C3 §11–13 与 M0 §1/7/8。前提是固定官方 Gitleaks 8.30.1 下载包校验可用；在任务临时目录创建仅包含上述提交及其祖先的干净 Git 仓库，记录准确 tree、源码 SHA256、扫描范围、命令与输出。预期合成正负自检和 tracked snapshot/history 扫描成功；失败保留固定错误码并调查，不覆盖旧结果或扩大例外。
- 边界：扫描对象不含本次生成的结果文件，结果不声明自身或未来提交已扫描，消除循环引用。Git 历史限定固定提交的可达祖先，不将并发变化的共享 refs 冒充固定输入；最终 PR 的整支扫描另由最新 CI 证明。禁止读取真实 .env，无数据库、模型/trace 调用、费用或部署。
- 完成条件：固定版本实际扫描、摘要回读匹配、历史工件逐字不变、独立 Agent 复验、新 PR 与最新 CI；合并等待用户授权。
- 实现者验证：固定提交的实际扫描退出 0 / `SECRET_SCAN_PASSED`，42 个可达提交；源码摘要与提交及新 JSON 完全匹配，原目录 5 份历史工件逐字不变。详见[范围与复现](../evidence/m0-integration/post-fix-scan-2026-09-09.md)和[原始结果](../evidence/m0-integration/post-fix-scan-2026-09-09.json)。未改代码，未重复数据库/协议测试。
- 独立复验：全新上下文 Agent 独立核对版本、原工件及两次实际扫描，未发现阻断项；见[报告](../evidence/m0-integration/review-evidence-2026-09-09.md)。
- 当前交接：[PR #9](https://github.com/kevinWangSheng/production-ops-agent/pull/9) 已提交；修复和独立复验完成，等待用户审核。最新提交 CI 以 PR checks 为准，合并须明确授权。
- GitHub 新增发现 discussion_r3965882716：汇合任务已写 #8 合并，而 ROADMAP/批次索引仍称全部待合并。已同步两处当前状态，明确 #8 已合并、#4–#7 与 #9 仍 OPEN，以及此文档分支不含其他原分支本轮修复；原 2026-09-08 过程标为历史，扫描原始工件不变。
