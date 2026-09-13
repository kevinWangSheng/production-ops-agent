# PR16 本地双轴 Code Review

用户于2026-09-10明确指定 eng:code-review 替代不可用的远端 Code Review；仅本次PR，不修改共享工作流。两个轴以独立全新上下文并行审查，不合并或跨轴排序。Security Review另列，不能由本报告冒充远端Security执行。

范围：e9d22e9183601ae322f006f4a42d9dd2bbcbae84...af24646eb02da3de2839005a7a3ea361fd7ac914（完整PR）；命令git diff e9d22e9183601ae322f006f4a42d9dd2bbcbae84...HEAD，HEAD前后固定。无issue-tracker配置，依据现有SPEC/C3/M0计划/冻结合同/任务记录；不安装新平台或重做实验。

## Standards

Standards 独立审查（静态）

固定范围：`git diff e9d22e9183601ae322f006f4a42d9dd2bbcbae84...HEAD`；审查前后 HEAD 均为 `af24646eb02da3de2839005a7a3ea361fd7ac914`，工作区干净。

硬违规：未确认。依据 AGENTS.md「项目目标与权限」「验证与汇报」、SPEC.md 的 Explicit exclusions / Evidence and context requirements / Runtime and human control requirements / Operating constraints / Verification and delivery，以及 docs/development.md。未发现足以报告的新增产品执行权、隐式开放实施门槛或把本地验证称为生产证明的问题。此结论是静态审查，不代表运行验证或远端机器人执行结果。

判断性建议（均非硬违规、非已复现功能故障）：

1. possible Duplicated Code — `scripts/m0_pg_live_probe.py:85–94,101–110`：两处完整重复 `while True` → `time.time() >= deadline` → `fcntl.flock(... LOCK_EX | LOCK_NB)` → `time.sleep(0.05)` 的限时账本锁循环。建议后续维护时提取一个持锁上下文管理器，让申请和结算共享等待/超时/释放规则；保留两个业务操作各自的逻辑。
2. possible Data Clumps — `scripts/m0_environment/initial_evidence.py:399–421,429–431,456–465`：同一验证结果被拆成八元素位置元组，后续访问需要 `for evidence_id, _, _, _, _, view, _, _ in loaded`，复制阶段再次完整解包。建议保留具名验证记录（或小 dataclass），按字段访问，减少添加 provenance 字段时同步修改多处位置的负担。

仅审查活跃源码的上述形状；按项目历史保留规则，不将 frozen snapshots、legacy 合同或历史证据复制列为重复代码。跳过工具已强制的格式/lint 检查；未运行模型、网络、数据库、容器，未打开凭据或私有供应商协议；未修改仓库。合计：0 个硬违规，2 个非阻塞维护建议。

## Spec

# Independent SPEC axis review

Repo: /Users/shenghuikevin/dev/AI/production-ops-agent-m0-01
Base: e9d22e9183601ae322f006f4a42d9dd2bbcbae84
HEAD: af24646eb02da3de2839005a7a3ea361fd7ac914 (confirmed unchanged, worktree clean)
Scope: entire PR, M0 validation only. Read-only inspection and offline synthetic checks. No private protocol, secrets, paid model, network, PG or container operations.

## Report

(a) 未完成要求：报告质量仍未收束，v4真实模型复验尚缺；SPEC:8、任务记录:3–5和冻结包:79明确承认，未虚报产品passes或打开M1。这是保留的实验不足，不单列新增代码缺陷。

(b) 范围：未确认额外产品能力或平台扩建；历史schema、源码快照与失败证据保留符合约定。

(c) P2：严格bridge把已被运行器判为未完成的协议失败重新认作completed。冻结包:89要求“completed assessment须有completed可信执行”，SPEC:70要求区分未完成与完成但不确定。scripts/m0/holmes_bridge.py:742仅按可解析report生成execution，未核result-business.status/report_validation_error。真实wrapper在holmes_baseline.py:1154–1157对末步携带tool calls明确标incomplete，却保留JSON正文；bridge会丢弃该拒绝。离线合法fixture仅增加这一可信失败状态，仍得到trusted/outcome/assessment全部completed、handoff=false、strict_errors=[]。须保留并校验运行器终态/协议拒绝，不能让报告解析替代可信执行状态；旧缺字段保真unknown。

## Evidence

- /tmp/m0-local-spec-probe.py: standalone deterministic mutation probe; fixture-only temporary files, no backend calls.
- /tmp/m0-local-spec-probe.txt: baseline [] and invalid completion accepted [] output.
- Reproduction: PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. .venv/bin/python /tmp/m0-local-spec-probe.py
- Independent existing tests: PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider tests/test_m0_outcomes_v4.py tests/test_m0_holmes_bridge_v4.py tests/test_m0_initial_evidence.py tests/test_m0_runtime_strict.py tests/test_m0_holmes_round02.py
- Result: 211 passed in 5.97s.
- PG behavior inspected statically and against committed safe evidence only; not independently rerun. No claim of fresh real-model or real-PG validation.

## 修复复验

原P2保留；已在经独立复验的补丁中修复。Standards增量0新增问题；Spec 10组状态/CLI、8组候选来源和实际Holmes假传输全部通过，独立244项回归通过。root隔离副本完整复跑601 passed/44 PG默认skip（20.15s）；没有真实模型、trace或PG操作。具体见同目录round-02-local-spec-revalidation.md及round-02-local-standards-fix-review.md。

## 审查提交

```text
af24646 docs: record resumed review request and repeated quota block
f642020 docs: record M0 handoff and unavailable remote review
56b17f7 docs: preserve full-input contract verification snapshot
a100c90 fix: bind complete delivered user input and assessment state
65d3624 docs: record fresh-run verification and preserved lab shutdown
f1df631 fix: validate fresh-run versions and control transitions
5b82c0d docs: preserve clock and private-output verification snapshot
0e44dc7 fix: bind imported collection clocks to original records
f0e7f03 fix: create investigation audit outputs with private permissions
78f7af0 docs: record report-independent handoff verification
90e251c fix: validate delivery bindings before reportless handoff
f923897 docs: record input provenance review and source snapshot
d66db41 fix: require strict investigation input provenance
96e89e3 docs: retain report failure handoff evidence and snapshots
eca7a16 fix: preserve ordinary report failures through the M0 outcome seam
e5357a8 docs: retain initial scope and CLI handoff verification
aff9664 fix: enforce initial evidence scope before disclosure and retain handoffs
ced7fd4 docs: preserve initial evidence validation and delivery boundaries
6770ecb fix: bind initial evidence and executed versions in M0 reports
1fba53e fix: invalidate current reports when human control advances
6eab0a4 fix: require consumed send grants for M0 response commits
aa485f9 docs: preserve strict M0 verification and historical replay evidence
2d28d11 fix: enforce complete report provenance at the strict M0 seam
da02563 docs: retain M0 review regression and closeout evidence
4f9410b fix: scope M0 step limits to each run
eb6e9d0 fix: require committed delivery for completed M0 reports
1462e2d fix: retain new-run transitions in M0 control snapshots
58e10b1 feat: validate M0 durable steps and freeze first-flow contracts
da4d464 fix: make projected evidence coverage explicit
0f20ed2 fix: bound Holmes reports and preserve delivered evidence
```

## 应用与边界

修复补丁SHA256：1b0553501438c50722c6c8d88daaeaa08414d19428f63660ebd9067fe58aa319。应用前核对原HEAD/干净状态，应用后逐项核对3文件SHA与独立审查一致；旧schema、原模型结果、数据库、ledger和worktree不改变。无关的两条维护建议仅记录，不强制重构。当前代码版本由本记录所属提交标识，原离线source manifests保留各自历史版本，不冒充覆盖本次bridge。

root第一次全量检查600 passed/1 failed/44 skipped：隔离archive没有相对.venv/bin/python，test_missing_scanner_fails_closed_with_fixed_output启动失败；复用原解释器后全量601 passed/44 skipped。前后日志均保留，无代码或扫描规则变更。

本轮本地Code Review按用户授权完成，不代表远端机器人执行。用户随后表示bot额度已恢复并要求本轮后改回GitHub bot；最新修复提交将等待Code Review及Security Review，不继续启动本地双轴流程。PR未合并，M1因真实报告质量FAIL保持关闭。

汇总：Standards 0硬违规、2非阻塞维护建议；Spec 1个P2已修复复验，0未处置阻塞项。
