# PR review gate 独立实现审查

日期：2026-09-09。审查者为未参与设计/实现的独立 Agent；仅修改本报告，未发布状态、修改保护、触发机器人或合并 PR。

## 结论

**P1 已在本地修复并独立复验；当前没有未处置的实现阻断发现。** 发布器现更新同一 check-run，下面保留初次审查的状态额度问题及复现历史。真实主线 Checks API 发布和保护阻断尚未验证，不能将本地结论标为部署完成。

## P1 — 无变化轮询也持续追加状态，约两天耗尽 SHA/context 上限

位置：`scripts/review_gate.py:218-219`、`:235-236`，`.github/workflows/review-gate.yml` 的每 5 分钟 schedule。

每次 evaluate 无条件先写 pending，再写最终状态。官方 [Create a commit status](https://docs.github.com/en/rest/commits/statuses#create-a-commit-status) 明确每个 repository/sha/context 最多创建 1000 条状态，超过返回 validation error。没有其他事件时，500 次正常 reconcile 即达到上限，名义频率约 41 小时 40 分钟；评论和其他 PR 事件还会使全部 open PR 被重新发布。

本地合成反例复用了真实 `evaluate` 和测试的有效证据，仅替换 API/snapshot 和有 1000 次上限的 status transport：500 次 READY 后移除机器人汇总，再次 evaluate 在初始 pending 写入处失败；证据判定已为 SUMMARY_MISSING_OR_AMBIGUOUS，但最后已发布状态仍为 success/READY。观察输出：

```json
{"successful_cycles":500,"writes":1000,"new_evidence":"SUMMARY_MISSING_OR_AMBIGUOUS","last_published_status":["success","READY"],"next_run":"API_UNAVAILABLE"}
```

若额度耗尽时最后状态为 failure/pending，则同一 SHA 后续审查和讨论全部完成也无法恢复。这会影响等待人工审核数天的普通 PR，无需恶意输入。

建议：采用能够更新同一对象的 check-run，或设计经审查的持久状态/证据去重及明确的额度处理；仅降低轮询频率不能消除硬上限。修改后覆盖超过 500 次相同证据 reconcile、证据变化和失败恢复，并重新检查 fail-closed 与发布竞争边界。

## 已执行核查

- 完整阅读 AGENTS/SPEC 及 ROADMAP、任务、开发指南和三个实现文件；此变更是开发工程门禁，不是 SPEC 排除的产品发布权限。
- `.venv/bin/python -m pytest tests/test_review_gate.py -q`：37 passed，0.02s。
- `python3 -I scripts/review_gate.py --pr 12`：真实只读 REST/GraphQL 查询返回 HEAD `70850a2357ac3236edc30d77711549d873495b8d`、`UNRESOLVED_THREADS`。没有使用 --publish。
- 只读查询 #12 bot 汇总：实际机器人 ID 199175422，实际 top-level metadata/表格结构与 parser 预期一致；该汇总绑定 `3309daf5b47ed058e50b3eb9605dab491315643c`，不能充当当前 HEAD 通过证据。
- 静态核查工作流只 checkout main、不执行 PR 文件/依赖、不插值 PR 文本入 shell，权限为 contents/read、pull-requests/read、statuses/write；发布入口检查 repository/workflow_ref。它依赖仓库写权限持有者可信，不证明不同 Actions workflow 之间身份隔离。
- 全分页线程、outdated 未解决线程、身份冒充、未知/重复汇总、HEAD/快照变化拒绝有合成测试；最后一次读与 status 写仍非事务，native conversation resolution 仍是线程重开的服务器控制。

## 证据范围与剩余验证

本报告的 1000 次上限复现是合成 transport，官方文档证明服务硬上限；未实际向 GitHub 写 1000 个状态。37 tests 是本地合成检查；#12 是当前真实 API 的拒绝路径。未执行主线受信 workflow、成功发布、必需检查绑定或真实合并阻断，不得据本报告宣称门禁已启用。

自动更新/手动复审后厂商是否更新汇总仍须真实验证；严格 parser 在厂商格式变化时会拒绝，需要明确恢复流程。bootstrap 须由用户审核合并工作流后验证真实发布，再追加 required check，不可伪造 bootstrap 成功。待最终任务文档补齐已确认部署顺序、可信边界和故障局限。

## 受检版本

HEAD：`ad99e6abd30afdbc1e57a007afaab69dd3e80203`，三个实现文件均为未提交新增。

- scripts/review_gate.py SHA256：`bcfc01bfb1321ce9d028480d192ebb614130ef0a0b12a10ba21f60561c108b1f`
- tests/test_review_gate.py SHA256：`fd819dbcf435f4c535eaf4aa02ba9e03e76864076e55a843cd7e091018105a8f`
- .github/workflows/review-gate.yml SHA256：`b9171a918e5936dc232ed5c10e3c3ead77735f2347e87283b6cebf11b9dbd80a`


## Checks API 修复复验（2026-09-09）

P1 关闭于本地实现层：检查发布端改为分页读取同 SHA、同 name、Actions app ID 15368 的唯一 check-run；首次 POST 创建，后续 PATCH 同一 id。工作流权限改为 `checks: write`。同 app/name 多个对象明确拒绝，旧 commit-status 追加路径已移除。

独立复验命令 `.venv/bin/python -m pytest tests/test_review_gate.py -q`：**40 passed，0.02s**。包含真实 #11 汇总的合成 PR 重放；不是 #11 原事故的就绪判断。

另以有状态的本地 API transport 运行真实 evaluate/status：1001 次 READY 后删除 summary，观察 failure，再恢复 summary，观察 success。共 1003 轮、1 次 POST、2005 次 PATCH，始终只有 1 个 check-run 对象，证明原历史增长反例已消除且证据变化仍能改变结论。此 transport 未模拟 GitHub 内部生命周期规则，不替代真实 API 验证。

只读核对 [Checks API 文档](https://docs.github.com/en/rest/checks/runs#update-a-check-run)：使用的 POST/PATCH、name/head_sha/external_id/status/conclusion/output 字段与权限一致；in_progress 更新没有发送 conclusion，completed 更新发送 success/failure。真实读取 #12 当前 HEAD 的 checks 返回分页对象中的 check_runs 数组，两个现有 Actions runs 的 app ID 均为 15368。首次辅助命令组合 `--slurp --jq` 被 gh 拒绝，随后改为 `--slurp | python3` 成功；实现本身未使用该不兼容组合。

首次部署的真实验证仍必须覆盖：创建 check-run；completed/success -> in_progress 时服务器确实撤销可合并状态；随后 failure 与恢复 success；跨 workflow run PATCH 同 id；fork PR 的 SHA 与 required check 关联；app 15368 绑定和 native conversation resolution。官方字段协议与合成 PATCH 测试不能证明这些服务器行为已经发生。现有仓库写权限可信边界、非事务最后读写窗口、故障不能持续实时撤绿的已声明边界保持不变。

复验版本 SHA256：

- scripts/review_gate.py：`00698e88c6ed200fa89c46ee79738e749ce1195cf3546fa3ecc31b812545fbe5`
- tests/test_review_gate.py：`f0a2955a9fa33cc3c77a10d2e61c1c546cd7c94487e66e633542024eeaee5f36`
- .github/workflows/review-gate.yml：`e5416fdabdff0376e04bd43ce9ea49486a5bea2a5a49089acfab81248d7c1754`
