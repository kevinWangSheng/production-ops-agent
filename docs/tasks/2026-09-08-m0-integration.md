# M0 汇合与秘密扫描

- 状态：进行中；2026-09-08。
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
